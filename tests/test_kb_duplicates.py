"""Identical files in the knowledge-base folder are indexed once."""
import shutil
from contextlib import closing

import pytest

import app.rag.vectorstore as vectorstore
from app.core.config import get_settings
from app.rag import lookup
from app.services import kb_versions
from app.services import knowledge_base as kb


class FakeStore:
    def __init__(self):
        self.docs = {}

    def add_documents(self, docs, ids=None):
        self.docs.update(zip(ids, docs))

    def delete(self, ids=None):
        for i in ids:
            self.docs.pop(i, None)


@pytest.fixture
def folder(monkeypatch, tmp_path):
    root = tmp_path / "Urban Money"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "urban_money_dir", root)
    monkeypatch.setattr(get_settings(), "kb_index_db", tmp_path / "kb.db")
    monkeypatch.setattr(get_settings(), "kb_cache_dir", tmp_path / "cache")
    monkeypatch.setattr(vectorstore, "is_pinecone", lambda: True)
    store = FakeStore()
    monkeypatch.setattr(vectorstore, "get_vectorstore", lambda: store)
    kb_versions._invalidate()
    return root, store


def _status(rel):
    with closing(kb._db()) as db:
        r = db.execute("SELECT status, error FROM kb_files WHERE path=?", (rel,)).fetchone()
    return tuple(r) if r else None


def _row_paths():
    with closing(kb._db()) as db:
        return lookup.indexed_paths(db)


def test_a_copy_is_not_indexed_and_takes_over_when_the_original_goes(folder):
    root, store = folder
    (root / "Codes.csv").write_text("Bank,Code\nAxis Bank,UM123\n", encoding="utf-8")
    (root / "WhatsApp").mkdir()
    shutil.copy(root / "Codes.csv", root / "WhatsApp" / "Codes.csv")
    r = kb.sync()
    assert r["indexed"] == ["Codes.csv"] and r["duplicates"] == ["WhatsApp/Codes.csv = Codes.csv"]
    assert _status("WhatsApp/Codes.csv") == ("duplicate", "Same content as Codes.csv")
    assert _row_paths() == {"Codes.csv"} and "WhatsApp/Codes.csv" not in kb_versions.active_versions()
    assert all(d.metadata["source"] == "Codes.csv" for d in store.docs.values())

    r = kb.sync()                                             # nothing changed: nothing re-done
    assert r["indexed"] == [] and r["unchanged"] == 1 and len(r["duplicates"]) == 1

    (root / "Codes.csv").unlink()
    r = kb.sync()
    assert r["indexed"] == ["WhatsApp/Codes.csv"] and r["removed"] == ["Codes.csv"]
    assert _status("WhatsApp/Codes.csv")[0] == "indexed" and "WhatsApp/Codes.csv" in kb_versions.active_versions()


def test_a_single_file_sync_also_recognises_a_copy(folder):
    root, _ = folder
    (root / "Codes.csv").write_text("Bank,Code\nAxis Bank,UM123\n", encoding="utf-8")
    kb.sync()
    shutil.copy(root / "Codes.csv", root / "Copy of Codes.csv")
    r = kb.sync(only={"Copy of Codes.csv"}, by="admin@example.com")
    assert r["duplicates"] == ["Copy of Codes.csv = Codes.csv"] and r["indexed"] == []
