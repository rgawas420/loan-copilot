"""Admin knowledge-base document management: upload, process, preview, archive, restore and delete."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import app.rag.vectorstore as vectorstore
from app.core.config import get_settings
from app.main import app
from app.services import accounts, kb_admin
from tests.test_knowledge_base import FakeStore


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
    return root, store


def _admin():
    c = TestClient(app)
    r = c.post("/api/auth/register", json={"email": f"kb-{uuid.uuid4().hex[:8]}@example.com", "name": "KB Admin",
                                           "password": "correct-horse"})
    accounts.update_user(r.json()["user"]["id"], -1, role="admin")
    return c


def _wait():
    for _ in range(200):
        if not kb_admin.busy():
            return
        time.sleep(0.05)
    raise AssertionError("knowledge-base job did not finish")


def _files(c):
    return {f["path"]: f for f in c.get("/api/admin/kb").json()["files"]}


def _texts(store):
    return " ".join(d.page_content for d in store.docs.values())


def test_kb_admin_needs_admin_role(kbenv):
    _admin()                                     # so the user below is not the first account (which is admin)
    c = TestClient(app)
    assert c.get("/api/admin/kb").status_code == 401
    c.post("/api/auth/register", json={"email": f"u-{uuid.uuid4().hex[:8]}@example.com", "name": "U",
                                       "password": "correct-horse"})
    assert c.get("/api/admin/kb").status_code == 403
    assert c.post("/api/admin/kb/upload", files=[("files", ("a.txt", b"x"))]).status_code == 403


def test_upload_processes_into_the_index(kbenv):
    root, store = kbenv
    c = _admin()
    r = c.post("/api/admin/kb/upload", data={"folder": "Payouts/2026"},
               files=[("files", ("payout.txt", b"Piramal LAP payout is 1.5 percent.")),
                      ("files", ("song.mp3", b"\x00\x01")),
                      ("files", ("../../escape.md", b"HDFC home loan RM is Asha"))]).json()
    assert [s["name"] for s in r["staged"]] == ["payout.txt", "escape.md"]       # the path in the name is dropped
    assert r["errors"][0]["file"] == "song.mp3" and "unsupported" in r["errors"][0]["error"]
    _wait()                                      # nothing to conflict with: both are added as new documents
    assert (root / "Payouts" / "2026" / "escape.md").exists()
    files = _files(c)
    assert files["Payouts/2026/payout.txt"]["status"] == "indexed" and files["Payouts/2026/payout.txt"]["chunks"] >= 1
    assert "1.5 percent" in _texts(store) and "Asha" in _texts(store)

    c.post("/api/admin/kb/upload", data={"folder": "Payouts/2026"},
           files=[("files", ("payout.txt", b"Piramal LAP payout is 1.75 percent from October."))])
    _wait()
    item = c.get("/api/admin/kb").json()["pending"][0]
    assert item["status"] == "REVIEW" and "same file name" in item["candidates"][0]["reasons"]
    assert "1.5 percent" in _texts(store) and "1.75 percent" not in _texts(store)   # not active until approved
    c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "version", "doc_id": item["candidates"][0]["doc_id"]})
    _wait()
    assert "1.75 percent" in _texts(store) and "1.5 percent" not in _texts(store)   # old vectors replaced


def test_folder_traversal_is_rejected(kbenv):
    c = _admin()
    r = c.post("/api/admin/kb/upload", data={"folder": "../outside"}, files=[("files", ("a.txt", b"hello there"))])
    assert r.json()["staged"] == [] and "dot" in r.json()["errors"][0]["error"]
    assert c.get("/api/admin/kb/download", params={"path": "../../audit.db"}).status_code == 400
    assert c.get("/api/admin/kb/preview", params={"path": "missing.txt"}).status_code == 404


def test_archive_restore_and_delete(kbenv):
    root, store = kbenv
    (root / "codes.txt").write_text("Axis Bank DSA code 6159MUM")
    (root / "old.txt").write_text("Old payout grid 2024")
    c = _admin()
    c.post("/api/admin/kb/sync", json={"full": False})
    _wait()
    assert {f["status"] for f in _files(c).values()} == {"indexed"}

    assert c.post("/api/admin/kb/archive", json={"paths": ["old.txt"]}).json()["archived"] == ["old.txt"]
    _wait()
    ov = c.get("/api/admin/kb").json()
    assert "old.txt" not in {f["path"] for f in ov["files"]} and [a["path"] for a in ov["archived"]] == ["old.txt"]
    assert "Old payout" not in _texts(store)

    assert c.post("/api/admin/kb/restore", json={"paths": ["old.txt"]}).json()["restored"] == ["old.txt"]
    _wait()
    assert _files(c)["old.txt"]["status"] == "indexed" and "Old payout" in _texts(store)

    r = c.post("/api/admin/kb/delete", json={"paths": ["codes.txt", "nope.txt"]}).json()
    assert r["deleted"] == ["codes.txt"] and r["errors"][0]["file"] == "nope.txt"
    _wait()
    assert not (root / "codes.txt").exists() and "6159MUM" not in _texts(store)
    from app.services import knowledge_base as kb
    assert kb.keyword_search("code 6159MUM") == []


def test_preview_download_and_reprocess(kbenv):
    root, store = kbenv
    (root / "rm.md").write_text("Bandhan Bank BL min vintage 3 years")
    c = _admin()
    assert c.get("/api/admin/kb/preview", params={"path": "rm.md"}).json()["processed"] is False
    c.post("/api/admin/kb/sync", json={})
    _wait()
    p = c.get("/api/admin/kb/preview", params={"path": "rm.md"}).json()
    assert p["processed"] and p["total"] == 1 and "vintage 3 years" in p["chunks"][0]
    d = c.get("/api/admin/kb/download", params={"path": "rm.md"})
    assert d.status_code == 200 and d.content == b"Bandhan Bank BL min vintage 3 years"

    before = set(store.docs)
    assert c.post("/api/admin/kb/process", json={"paths": ["rm.md"]}).json()["queued"] == 1
    _wait()
    assert set(store.docs) == before and _files(c)["rm.md"]["status"] == "indexed"   # same ids, re-written
    assert c.post("/api/admin/kb/process", json={"paths": ["missing.md"]}).status_code == 404

    acts = {a["action"] for a in accounts.recent_activity(50)}
    assert {"kb_sync", "kb_reprocess"} <= acts
