"""Source authority (priority) in retrieval and in the prompt, and the knowledge-base audit log."""
from datetime import date

import pytest

import app.rag.retrieval as retrieval
from app.rag import grounding
from app.services import kb_versions
from app.services import knowledge_base as kb
from tests.test_kb_admin import _admin, _wait
from tests.test_kb_staging import APRIL, AUGUST, _pending, _upload, kbenv  # noqa: F401  (fixture)


def test_priority_defaults_from_the_document_type_and_drops_for_ocr_and_expiry():
    a = kb_versions.authority_of({"category": "Payout"})
    assert (a["source_type"], a["priority"], a["custom"]) == ("PAYOUT_SHEET", 90, False)
    assert kb_versions.authority_of({"category": "General"})["priority"] == 50
    assert kb_versions.authority_of({"category": "Payout", "source_type": "FAQ"})["priority"] == 20
    assert kb_versions.authority_of({"category": "Policy", "priority": 75})["priority"] == 75
    ocr = kb_versions.authority_of({"category": "Policy"}, {"extraction": "ocr"})
    assert ocr["priority"] == 80 and ocr["notes"] == ["OCR, not verified"]
    old = kb_versions.authority_of({"category": "Payout"}, {"effective_until": "2026-07-31"}, today=date(2026, 10, 8))
    assert old["priority"] == 60 and old["notes"] == ["expired 2026-07-31"]


def test_higher_priority_source_ranks_first_and_the_prompt_states_authority(kbenv, monkeypatch):
    root, _ = kbenv
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: [])          # keyword search only
    (root / "HDFC payout answers.txt").write_text("HDFC Bank business loan payout is 1.20 percent.")
    (root / "HDFC Payout Sheet.txt").write_text("HDFC Bank business loan payout is 1.35 percent.")
    kb.sync()
    c = _admin()
    r = c.patch("/api/admin/kb/document", json={"path": "HDFC payout answers.txt", "source_type": "FAQ"})
    assert r.status_code == 200 and r.json()["reindexing"] is False                # used at once, no re-index
    r = retrieval.retrieve("HDFC business loan payout", use_llm=False)
    sources = [d.metadata["source"] for d in r.docs]
    assert sources[0] == "HDFC Payout Sheet.txt"
    prio = {c.doc.metadata["source"]: c.priority for c in r.selected}
    assert prio == {"HDFC Payout Sheet.txt": 90, "HDFC payout answers.txt": 20}

    context, refs = grounding.build_context(r.docs)
    assert "authority: Payout sheet, priority 90" in context and "authority: FAQ, priority 20" in context
    assert "higher priority applies" in grounding.SYSTEM_PROMPT

    assert c.patch("/api/admin/kb/document", json={"path": "HDFC payout answers.txt", "priority": 101}).status_code == 422
    assert c.patch("/api/admin/kb/document", json={"path": "HDFC payout answers.txt", "source_type": "BOSS"}).status_code == 400


def test_audit_log_records_who_did_what_to_which_version(kbenv):
    root, _ = kbenv
    (root / "215 PLUS APRIL 2026 PAYOUT SHEET.txt").write_text(APRIL)
    kb.sync(by="")                                                          # startup sync: system
    c = _admin()
    _upload(c, "AUG 26 PAYOUT SHEET.txt", AUGUST)
    [item] = _pending(c)
    c.post(f"/api/admin/kb/pending/{item['id']}/decide", json={"action": "version", "doc_id": item["candidates"][0]["doc_id"]})
    _wait()
    c.post("/api/admin/kb/versions/activate", json={"path": "AUG 26 PAYOUT SHEET.txt", "version": 1})
    _wait()
    log = c.get("/api/admin/kb/audit").json()
    events = [(e["action"], e["actor"].split("@")[0][:3], e["path"]) for e in reversed(log["events"])]
    assert events[0] == ("process", "sys", "215 PLUS APRIL 2026 PAYOUT SHEET.txt")
    acts = [a for a, _, _ in events]
    assert acts.index("upload") < acts.index("activate") < acts.index("rollback")
    assert all(actor == "kb-" for a, actor, _ in events if a in ("upload", "activate", "rollback"))
    activate = next(e for e in log["events"] if e["action"] == "activate")
    assert activate["version"] == 2 and "v2 (August 2026) active" in activate["detail"] and "v1 (April 2026) archived" in activate["detail"]
    assert activate["label"] == "Activated a new version"
    doc_events = c.get("/api/admin/kb/audit", params={"doc_id": activate["doc_id"]}).json()["events"]
    assert {"activate", "rollback", "process"} <= {e["action"] for e in doc_events}
    assert c.get("/api/admin/kb/audit", params={"action": "upload"}).json()["total"] == 1
