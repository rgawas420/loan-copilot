"""Verifying OCR text: checks on each scanned page (OCR confidence, a second reading, suspicious figures), the
checklist that must be ticked before saving, page-scoped verification of mixed PDFs, tables, conflicts, history.

These tests run the real OCR engine on generated images; only the second reader (a vision model) is faked."""
import io

import pytest

from app.core.config import get_settings
from app.services import kb_verify, ocr_review, vision
from app.services import knowledge_base as kb
from app.services.ingestion import Part
from tests.test_kb_admin import _admin, _texts, _wait
from tests.test_kb_staging import kbenv  # noqa: F401  (fixture)

pytest.importorskip("rapidocr_onnxruntime")
pymupdf = pytest.importorskip("pymupdf")

LINES = ["HDFC Bank LAP payout 1.25%", "Tenure up to 15 years", "Pincode 411001"]


def _poster(lines=LINES) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("arial.ttf", 30)
    except OSError:
        font = ImageFont.load_default(size=30)
    img = Image.new("RGB", (760, 70 + 60 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((30, 30 + 60 * i), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def review_on(monkeypatch):
    """Real OCR and checks; the second reader returns `reading["text"]` (None = no reader configured)."""
    monkeypatch.setattr(get_settings(), "ocr_enabled", True)
    monkeypatch.setattr(get_settings(), "ocr_review", True)
    reading = {"text": None}
    monkeypatch.setattr(vision, "second_reader", lambda: (object(), "fake-reader") if reading["text"] else (None, ""))
    monkeypatch.setattr(vision, "second_reading", lambda data: reading["text"])
    return reading


def _editor(c, **target):
    """Open the verification screen and wait for its checks."""
    c.get("/api/admin/kb/verify", params=target)
    for t in list(ocr_review._threads.values()):
        t.join(120)
    ed = c.get("/api/admin/kb/verify", params=target).json()
    assert ed["review"]["status"] == "READY", ed["review"]
    return ed


# ------------------------------------------------------------------ figure checks
def test_suspicious_figures_are_flagged():
    found = {s["text"] for s in ocr_review.suspicious(
        "RO18.50% | ROI 8.50% | 1O.5% | HDFC0001 | 120% | 8.5.0% | Pincode 411001 | l2.5% | 10lakh")}
    assert found == {"RO18.50%", "1O.5%", "120%", "8.5.0%", "l2.5%"}
    problems = ocr_review.lint("Payout [unreadable]% on LAP")
    assert problems[0]["blocking"] and "unreadable" in problems[0]["detail"]


def test_markdown_tables_in_verified_text_become_table_rows():
    parts = kb_verify.section_parts({"label": "Page 2", "page": 2, "text":
        "Payout grid August 2026\n\n| Product | Payout |\n|---|---|\n| LAP | 1.25% |\n| Home loan | 0.65% |\n\nT&C apply"})
    assert [(p.text, p.rows) for p in parts] == [
        ("Payout grid August 2026", []), ("", ["Product: LAP | Payout: 1.25%", "Product: Home loan | Payout: 0.65%"]),
        ("T&C apply", [])]
    assert all(p.page == 2 and p.verified for p in parts)


# ------------------------------------------------------------------ images
def test_image_checks_must_be_ticked_and_suspicious_figures_accepted(kbenv, review_on):
    root, store = kbenv
    (root / "HDFC LAP poster.png").write_bytes(_poster())
    kb.sync()
    c = _admin()
    ed = _editor(c, path="HDFC LAP poster.png")                     # checked without a second reader
    ocr_text = ed["ocr_text"]
    assert "1.25%" in ocr_text and "411001" in ocr_text and ed["review"]["pages"][0]["second"] is None
    review_on["text"] = ocr_text.replace("1.25%", "1.20%")         # the two readings disagree on one figure
    assert c.post("/api/admin/kb/verify/recheck", json={"path": "HDFC LAP poster.png"}).json()["started"]
    ed = _editor(c, path="HDFC LAP poster.png")
    page = ed["review"]["pages"][0]
    assert page["page"] == 0 and page["width"] == 760 and page["boxes"] and page["second"] == review_on["text"]
    assert all(0 < b["conf"] <= 1 and len(b["box"]) == 4 for b in page["boxes"])
    items = {(it["kind"], it["text"]) for it in ed["review"]["items"]}
    assert {("only_ocr", "1.25%"), ("only_second", "1.20%")} <= items
    assert next(it for it in ed["review"]["items"] if it["kind"] == "only_ocr")["box"] is not None   # shown on the image
    png = c.get("/api/admin/kb/verify/page", params={"path": "HDFC LAP poster.png", "page": 0})
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")

    ids = [it["id"] for it in ed["review"]["items"]]
    body = {"path": "HDFC LAP poster.png", "base_at": None, "accept_warnings": False}
    r = c.post("/api/admin/kb/verify", json={**body, "text": ocr_text, "checked": ids[:-1]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "unchecked" and r.json()["detail"]["items"] == ids[-1:]
    r = c.post("/api/admin/kb/verify", json={**body, "text": ocr_text + "\nROI 1O.5%", "checked": ids})
    assert r.status_code == 422 and r.json()["detail"]["warnings"][0]["text"] == "1O.5%"
    r = c.post("/api/admin/kb/verify", json={**body, "text": ocr_text + "\nROI [unreadable]", "checked": ids,
                                             "accept_warnings": True})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "unreadable"

    r = c.post("/api/admin/kb/verify", json={**body, "text": ocr_text + "\nROI 10.5%", "checked": ids}).json()
    assert r["checked"]["items"] == len(ids) and r["scope"] == "all" and r["changes"]["added"] == ["10.5%"]
    _wait()
    assert "ROI 10.5%" in _texts(store) and all(d.metadata["extraction"] == "verified" for d in store.docs.values())

    stale = c.post("/api/admin/kb/verify", json={**body, "text": ocr_text, "checked": ids})   # opened before the save
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "conflict"
    ed = c.get("/api/admin/kb/verify", params={"path": "HDFC LAP poster.png"}).json()
    assert ed["verified"]["checked"]["checked"] == len(ids) and ed["history"] == []
    c.post("/api/admin/kb/verify", json={**body, "base_at": ed["verified"]["at"], "text": ocr_text, "checked": ids})
    c.post("/api/admin/kb/verify/remove", json={"path": "HDFC LAP poster.png"})
    _wait()
    hist = c.get("/api/admin/kb/verify", params={"path": "HDFC LAP poster.png"}).json()["history"]
    assert [h["action"] for h in hist] == ["removed", "replaced"] and hist[1]["changes"]["added"] == ["10.5%"]
    assert "ROI 10.5%" not in _texts(store)                             # back to the OCR output


def test_unreadable_page_is_a_check_item(kbenv, review_on):
    root, _ = kbenv
    (root / "blank scan.png").write_bytes(_poster([]))
    kb.sync()
    c = _admin()
    ed = _editor(c, path="blank scan.png")
    assert [it["kind"] for it in ed["review"]["items"]] == ["page", "empty_page"] and not ed["ocr_available"]


# ------------------------------------------------------------------ mixed PDFs
def _mixed_pdf() -> bytes:
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((50, 72), "Loan against property policy. Maximum tenure 15 years for salaried customers.")
    p2 = doc.new_page()
    p2.insert_image(pymupdf.Rect(40, 40, 560, 300), stream=_poster())
    return doc.tobytes()


def test_mixed_pdf_verifies_only_its_scanned_pages(kbenv, review_on):
    root, store = kbenv
    (root / "LAP policy.pdf").write_bytes(_mixed_pdf())
    kb.sync()
    c = _admin()
    ed = _editor(c, path="LAP policy.pdf")
    assert ed["scope"] == "pages" and ed["text"].startswith("=== Page 2 ===\n") and "Maximum tenure" not in ed["text"]
    assert [p["page"] for p in ed["review"]["pages"]] == [2]
    ids = [it["id"] for it in ed["review"]["items"]]

    no_head = c.post("/api/admin/kb/verify", json={"path": "LAP policy.pdf", "checked": ids,
                                                   "text": ed["text"].split("\n", 1)[1]})
    assert no_head.status_code == 400 and no_head.json()["detail"]["code"] == "pages"
    text = "=== Page 2 ===\n| Product | Payout |\n|---|---|\n| HDFC LAP | 1.30% |\nPincode 411001"
    assert c.post("/api/admin/kb/verify", json={"path": "LAP policy.pdf", "checked": ids, "text": text}).status_code == 200
    _wait()
    texts = _texts(store)
    assert "Maximum tenure 15 years" in texts                          # the text page keeps its own extraction
    assert "Product: HDFC LAP | Payout: 1.30%" in texts and "1.25%" not in texts
    pages = {d.metadata.get("page") for d in store.docs.values()}
    assert pages == {1, 2}


def test_page_scoped_text_keeps_page_headers():
    text = kb_verify.to_text([Part("Page 2", text="Payout 1.25%", page=2, ocr=True)], headers=True)
    assert text == "=== Page 2 ===\nPayout 1.25%" and kb_verify.parse(text)[0]["page"] == 2
