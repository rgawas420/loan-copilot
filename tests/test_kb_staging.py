"""Staged uploads: new documents, new versions waiting for approval, duplicates, activation, rollback, merging."""
import sqlite3
from contextlib import closing

import pytest

import app.rag.vectorstore as vectorstore
from app.core.config import get_settings
from app.services import kb_staging, kb_versions
from app.services import knowledge_base as kb
from tests.test_kb_admin import _admin, _texts, _wait
from tests.test_knowledge_base import FakeStore, _xlsx


@pytest.fixture
def kbenv(monkeypatch, tmp_path):
    root = tmp_path / "Urban Money"
    root.mkdir()
    store = FakeStore()
    monkeypatch.setattr(get_settings(), "urban_money_dir", root)
    monkeypatch.setattr(get_settings(), "kb_index_db", tmp_path / "kb.db")
    monkeypatch.setattr(get_settings(), "kb_cache_dir", tmp_path / "cache")
    monkeypatch.setattr(vectorstore, "is_pinecone", lambda: True)
    monkeypatch.setattr(vectorstore, "get_vectorstore", lambda: store)
    kb_versions._invalidate()
    return root, store


APRIL = "Payout grid April 2026\nHDFC Bank Business Loan payout 1.20 percent\nICICI Bank LAP payout 0.65 percent"
AUGUST = "Payout grid August 2026\nHDFC Bank Business Loan payout 1.35 percent\nICICI Bank LAP payout 0.70 percent"


def _upload(c, name, data, **form):
    r = c.post("/api/admin/kb/upload", data=form, files=[("files", (name, data if isinstance(data, bytes) else data.encode()))])
    assert r.status_code == 200, r.text
    _wait()
    return r.json()


def _pending(c):
    return [p for p in c.get("/api/admin/kb").json()["pending"] if p["status"] in kb_staging.OPEN]


def _versions(path):
    return [(v["version"], v["status"], v["file"], v["label"]) for v in kb_versions.versions(path)]


def test_new_month_waits_for_approval_then_replaces_the_old_one(kbenv):
    root, store = kbenv
    (root / "215 PLUS APRIL 2026 PAYOUT SHEET.txt").write_text(APRIL)          # copied into the folder: bulk load
    kb.sync()
    c = _admin()
    _upload(c, "AUG 26 PAYOUT SHEET.txt", AUGUST)
    [item] = _pending(c)
    assert item["status"] == "REVIEW" and item["label"] == "August 2026" and item["effective_from"] == "2026-08-01"
    match = item["candidates"][0]
    assert match["path"] == "215 PLUS APRIL 2026 PAYOUT SHEET.txt" and match["label"] == "April 2026"
    assert any(r.startswith("similar name") for r in match["reasons"]) and "same document type (Payout)" in match["reasons"]
    assert "1.20 percent" in _texts(store) and "1.35 percent" not in _texts(store)   # nothing changes before approval
    assert not (root / "AUG 26 PAYOUT SHEET.txt").exists()

    r = c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "version", "doc_id": match["doc_id"]}).json()
    assert r["path"] == "AUG 26 PAYOUT SHEET.txt" and r["version"] == 2
    _wait()
    assert sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == ["AUG 26 PAYOUT SHEET.txt"]
    assert "1.35 percent" in _texts(store) and "1.20 percent" not in _texts(store)
    assert _versions("AUG 26 PAYOUT SHEET.txt") == [
        (2, "ACTIVE", "AUG 26 PAYOUT SHEET.txt", "August 2026"),
        (1, "ARCHIVED", "215 PLUS APRIL 2026 PAYOUT SHEET.txt", "April 2026")]
    old = kb_versions.versions("AUG 26 PAYOUT SHEET.txt")[1]
    assert old["effective_until"] == "2026-07-31" and old["restorable"]
    assert kb_versions.active_versions().keys() == {"AUG 26 PAYOUT SHEET.txt"}
    assert _pending(c) == []

    r = c.post("/api/admin/kb/versions/activate", json={"path": "AUG 26 PAYOUT SHEET.txt", "version": 1}).json()
    assert r["path"] == "215 PLUS APRIL 2026 PAYOUT SHEET.txt"                       # rollback brings the old file back
    _wait()
    assert sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == ["215 PLUS APRIL 2026 PAYOUT SHEET.txt"]
    assert "1.20 percent" in _texts(store) and "1.35 percent" not in _texts(store)
    assert [v[:2] for v in _versions("215 PLUS APRIL 2026 PAYOUT SHEET.txt")] == [(2, "ARCHIVED"), (1, "ACTIVE")]


def test_unrelated_upload_is_added_at_once_and_exact_duplicate_waits(kbenv):
    root, store = kbenv
    c = _admin()
    _upload(c, "HDFC Business Loan Policy.txt", "HDFC Bank business loan: maximum Rs 50 lakh.")
    _upload(c, "ICICI Business Loan Policy.txt", "ICICI Bank business loan: maximum Rs 75 lakh.")   # another bank
    assert _pending(c) == [] and len(kb_versions.active_versions()) == 2
    done = [p for p in c.get("/api/admin/kb").json()["pending"] if p["status"] == "PUBLISHED"]
    assert len(done) == 2

    _upload(c, "HDFC BL policy copy.txt", "HDFC Bank business loan: maximum Rs 50 lakh.")
    [item] = _pending(c)
    assert item["status"] == "DUPLICATE" and item["duplicates"][0]["path"] == "HDFC Business Loan Policy.txt"
    assert not next(ch for ch in item["checks"] if ch["check"] == "Not a duplicate")["ok"]
    c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "reject"})
    assert _pending(c) == [] and not (root / ".pending" / item["id"]).exists()
    r = c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "reject"})
    assert r.status_code == 409


def test_admin_can_name_the_document_or_keep_an_upload_separate(kbenv):
    root, _ = kbenv
    c = _admin()
    _upload(c, "Bajaj SM contacts.txt", "Bajaj Finserv Pune SM Sanket 9811111111")
    _upload(c, "Pune list.txt", "Bajaj Finserv Pune SM Rohit 9822222222", target="Bajaj SM contacts.txt")
    [item] = _pending(c)
    assert item["status"] == "REVIEW" and "chosen at upload" in item["candidates"][0]["reasons"]
    r = c.post(f"/api/admin/kb/pending/{item['id']}/decide",
               json={"action": "separate", "title": "Pune contact list", "effective_from": "2026-10-01"}).json()
    assert r["path"] == "Pune list.txt"
    _wait()
    doc = kb_versions.get_doc("Pune list.txt")
    assert doc["title"] == "Pune contact list" and kb_versions.versions("Pune list.txt")[0]["effective_from"] == "2026-10-01"
    assert len(kb_versions.active_versions()) == 2

    _upload(c, "Pune list.txt", "Bajaj Finserv Pune SM Amit 9833333333")
    [item] = _pending(c)
    r = c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "separate"})
    assert r.status_code == 409 and "already in the knowledge base" in r.json()["detail"]
    bad = c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "version", "doc_id": item["candidates"][0]["doc_id"],
                                                                     "effective_from": "01-10-2026"})
    assert bad.status_code == 400


def test_unreadable_upload_can_only_be_rejected(kbenv):
    c = _admin()
    _upload(c, "poster.png", b"not really an image")                  # OCR is disabled in tests
    [item] = _pending(c)
    assert item["status"] == "FAILED"
    assert c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "separate"}).status_code == 409
    assert c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "reject"}).status_code == 200


def test_database_refuses_two_active_versions(kbenv):
    root, _ = kbenv
    (root / "a.txt").write_text("Axis Bank home loan code 6159MUM")
    kb.sync()
    doc = kb_versions.get_doc("a.txt")
    with closing(kb._db()) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO kb_versions(doc_id, version, sha, status) VALUES (?, 9, 'x', 'ACTIVE')", (doc["doc_id"],))


def test_existing_duplicates_are_found_and_merged_into_versions(kbenv):
    root, store = kbenv
    rows = [["BANK NAME", "HL", "LAP"], ["Axis Bank", "6159MUM", "6159MUM"], ["HDFC Bank", "7788", "7789"],
            ["ICICI Bank", "9911", "9912"], ["Bajaj Finserv", "14788", "14789"]]
    _xlsx(root / "Code Book-3.xlsx", rows)
    _xlsx(root / "Code Book-4.xlsx", rows + [["Kotak", "5501", "5502"]])
    _xlsx(root / "Code Book-5.xlsx", rows + [["Kotak", "5501", "5502"], ["Aavas", "6601", "6602"]])
    (root / "HDFC policy.txt").write_text("HDFC Bank home loan policy: maximum tenure 30 years.")
    kb.sync()
    c = _admin()
    [group] = c.get("/api/admin/kb/duplicates").json()["groups"]
    assert [m["path"] for m in group["members"]] == ["Code Book-3.xlsx", "Code Book-4.xlsx", "Code Book-5.xlsx"]
    assert group["latest"] == "Code Book-5.xlsx"

    c.post("/api/admin/kb/merge", json={"keep": "Code Book-5.xlsx", "others": ["Code Book-4.xlsx", "Code Book-3.xlsx"]})
    _wait()
    assert sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == ["Code Book-5.xlsx", "HDFC policy.txt"]
    assert [v[:3] for v in _versions("Code Book-5.xlsx")] == [
        (3, "ACTIVE", "Code Book-5.xlsx"), (2, "ARCHIVED", "Code Book-4.xlsx"), (1, "ARCHIVED", "Code Book-3.xlsx")]
    assert kb_versions.active_versions().keys() == {"Code Book-5.xlsx", "HDFC policy.txt"}
    assert "Aavas" in _texts(store) and sum("Code Book" in d.metadata["source"] for d in store.docs.values()) >= 1
    assert all(d.metadata["source"] in ("Code Book-5.xlsx", "HDFC policy.txt") for d in store.docs.values())
    assert c.get("/api/admin/kb/duplicates").json()["groups"] == []

    c.post("/api/admin/kb/versions/activate", json={"path": "Code Book-5.xlsx", "version": 1})
    _wait()
    assert (root / "Code Book-3.xlsx").exists() and not (root / "Code Book-5.xlsx").exists()


# ------------------------------------------------------------------ review tools (comparison, tests, history)
def test_comparison_lists_changed_cells_added_and_removed_rows():
    from app.services import kb_review
    from app.services.ingestion import Part
    old = [Part("Sheet: Codes", rows=["BANK NAME: Axis Bank | HL: 6159MUM | LAP: 6159MUM", "BANK NAME: HDFC Bank | HL: 7788 | LAP: 7789",
                                      "BANK NAME: ICICI Bank | HL: 9911 | LAP: 9912"])]
    new = [Part("Sheet: Codes", rows=["BANK NAME: Axis Bank | HL: 6160MUM | LAP: 6159MUM", "BANK NAME: HDFC Bank | HL: 7788 | LAP: 7789",
                                      "BANK NAME: Kotak | HL: 5501 | LAP: 5502"])]
    c = kb_review.compare(old, new)
    [t] = c["tables"]
    assert t["key"] == ["BANK NAME"]
    assert t["changed"] == [{"key": "Axis Bank", "cells": [{"header": "HL", "old": "6159MUM", "new": "6160MUM"}]}]
    assert [r["key"] for r in t["added"]] == ["Kotak"] and [r["key"] for r in t["removed"]] == ["ICICI Bank"]
    assert c["summary"]["rows_changed"] == 1 and not c["summary"]["identical"]

    text = kb_review.compare([Part("Page 1", text=APRIL)], [Part("Page 1", text=AUGUST)])
    assert "1.35 percent" in text["figures"]["added"] and "1.20 percent" in text["figures"]["removed"]
    assert text["text"] and kb_review.compare(old, old)["summary"]["identical"]


def test_failing_knowledge_tests_block_activation_until_forced(kbenv):
    from app.services import rag_debug
    root, store = kbenv
    (root / "215 PLUS APRIL 2026 PAYOUT SHEET.txt").write_text(APRIL)
    kb.sync()
    ok = rag_debug.golden_save({"question": "HDFC business loan payout?", "expected_answer": "1.35%", "expected_document": "PAYOUT SHEET"})
    bad = rag_debug.golden_save({"question": "ICICI LAP payout?", "expected_answer": "0.65%", "expected_document": "payout sheet"})
    other = rag_debug.golden_save({"question": "Axis code?", "expected_answer": "6159MUM", "expected_document": "Code Book"})
    try:
        c = _admin()
        _upload(c, "AUG 26 PAYOUT SHEET.txt", AUGUST)
        [item] = _pending(c)
        doc_id = item["candidates"][0]["doc_id"]
        check = next(ch for ch in item["checks"] if ch["check"] == "Knowledge tests")
        assert not check["ok"] and "1 of 2" in check["detail"]                  # the Code Book test is not linked

        rv = c.get(f"/api/admin/kb/pending/{item['id']}/review", params={"doc_id": doc_id}).json()
        assert rv["current"]["label"] == "April 2026" and rv["new"]["label"] == "August 2026"
        assert {t["question"]: t["status"] for t in rv["tests"]["tests"]} == {"HDFC business loan payout?": "PASS",
                                                                            "ICICI LAP payout?": "FAIL"}
        assert next(t for t in rv["tests"]["tests"] if t["status"] == "FAIL")["in_current"] is True
        assert "1.35 percent" in rv["comparison"]["figures"]["added"]

        r = c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "version", "doc_id": doc_id})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "tests_failed"
        assert [t["question"] for t in r.json()["detail"]["tests"]] == ["ICICI LAP payout?"]
        assert "1.20 percent" in _texts(store)                                     # nothing changed
        r = c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "version", "doc_id": doc_id, "force": True})
        assert r.status_code == 200 and r.json()["tests_failed"] == 1
        _wait()
        assert "1.35 percent" in _texts(store)
        assert "failing knowledge test" in kb_versions.versions("AUG 26 PAYOUT SHEET.txt")[0]["note"]

        hist = c.post("/api/admin/kb/history-search", json={"query": "HDFC business loan payout"}).json()["results"]
        assert hist[0]["status"] == "ARCHIVED" and hist[0]["label"] == "April 2026" and "1.20 percent" in hist[0]["text"]
        normal = c.post("/api/admin/kb/search", json={"query": "HDFC business loan payout"}).json()["results"]
        assert all("1.20 percent" not in h["text"] for h in normal)              # normal search: ACTIVE only
        cmp = c.get("/api/admin/kb/versions/compare", params={"path": "AUG 26 PAYOUT SHEET.txt", "version": 1}).json()
        assert cmp["old"]["label"] == "April 2026" and "1.20 percent" in cmp["comparison"]["figures"]["removed"]
    finally:
        for g in (ok, bad, other):
            rag_debug.golden_delete(g["id"])


# ------------------------------------------------------------------ verified text for image-based documents
POSTER_OCR = "Aditya Birla STSL LAP\nROI 8.5%\nPincode 411001\nPayout 1.75%"         # OCR misread 1.25% as 1.75%


@pytest.fixture
def fake_ocr(monkeypatch):
    from app.services import ocr
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image", lambda data: POSTER_OCR)


def test_admin_corrects_ocr_text_and_it_replaces_the_ocr_output(kbenv, fake_ocr):
    root, store = kbenv
    (root / "Aditya_Birla_STSL_LAP_Reference.jpg").write_bytes(b"fake image bytes")
    kb.sync()
    c = _admin()
    f = {x["path"]: x for x in c.get("/api/admin/kb").json()["files"]}["Aditya_Birla_STSL_LAP_Reference.jpg"]
    assert f["extraction"] == "ocr" and "Payout 1.75%" in _texts(store)

    ed = c.get("/api/admin/kb/verify", params={"path": f["path"]}).json()
    assert ed["image"] and ed["verified"] is None and ed["text"] == POSTER_OCR
    assert {"8.5%", "1.75%", "411001"} <= set(ed["figures"])
    r = c.post("/api/admin/kb/verify", json={"path": f["path"], "text": POSTER_OCR.replace("1.75%", "1.25%")}).json()
    assert r["changes"] == {"removed": ["1.75%"], "added": ["1.25%"]}
    _wait()
    assert "Payout 1.25%" in _texts(store) and "1.75%" not in _texts(store)
    assert all(d.metadata["extraction"] == "verified" for d in store.docs.values())
    f = {x["path"]: x for x in c.get("/api/admin/kb").json()["files"]}[f["path"]]
    assert f["extraction"] == "verified"
    ed = c.get("/api/admin/kb/verify", params={"path": f["path"]}).json()
    assert ed["verified"]["by"].startswith("kb-") and ed["text"].endswith("Payout 1.25%") and ed["ocr_text"] == POSTER_OCR

    assert c.post("/api/admin/kb/verify/remove", json={"path": f["path"]}).json()["removed"]
    _wait()
    assert "Payout 1.75%" in _texts(store)                                     # back to the OCR output


def test_multi_page_text_keeps_its_pages():
    from app.services import kb_verify
    from app.services.ingestion import Part
    text = kb_verify.to_text([Part("Page 1", text="ROI 8.5%", page=1, ocr=True), Part("Page 2", text="Tenure 20 years", page=2, ocr=True)])
    assert text == "=== Page 1 ===\nROI 8.5%\n\n=== Page 2 ===\nTenure 20 years"
    assert kb_verify.parse(text.replace("8.5%", "8.50%")) == [{"label": "Page 1", "page": 1, "text": "ROI 8.50%"},
                                                               {"label": "Page 2", "page": 2, "text": "Tenure 20 years"}]
    assert kb_verify.kind([Part(text="x", ocr=True)]) == "ocr" and kb_verify.kind([Part(text="x")], ".png") == "ocr"


def test_uploaded_image_waits_until_its_text_is_verified(kbenv, fake_ocr):
    root, store = kbenv
    c = _admin()
    _upload(c, "LAP poster.png", b"png bytes")
    [item] = _pending(c)
    assert item["status"] == "REVIEW" and not next(ch for ch in item["checks"] if ch["check"] == "Image-based")["ok"]
    assert c.get(f"/api/admin/kb/pending/{item['id']}/file").content == b"png bytes"
    c.post("/api/admin/kb/verify", json={"pending_id": item["id"], "text": POSTER_OCR.replace("1.75%", "1.25%")})
    _wait()                                         # checked again: verified, nothing to conflict with, so added
    assert _pending(c) == [] and "Payout 1.25%" in _texts(store) and (root / "LAP poster.png").exists()


def test_image_ocr_cannot_read_is_rescued_by_typing_its_text(kbenv):
    root, store = kbenv
    c = _admin()
    _upload(c, "rates.png", b"unreadable")                    # OCR is disabled in tests: nothing can be read
    [item] = _pending(c)
    assert item["status"] == "FAILED"
    ed = c.get("/api/admin/kb/verify", params={"pending_id": item["id"]}).json()
    assert ed["text"] == "" and not ed["ocr_available"]
    assert c.post("/api/admin/kb/verify", json={"pending_id": item["id"], "text": "  "}).status_code == 400
    c.post("/api/admin/kb/verify", json={"pending_id": item["id"], "text": "HDFC LAP ROI 9.10%"})
    _wait()
    assert _pending(c) == [] and "ROI 9.10%" in _texts(store)
