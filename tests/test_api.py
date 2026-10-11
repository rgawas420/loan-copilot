import json

from fastapi.testclient import TestClient

import app.rag.vectorstore as vs
import app.rag.workflow as wf
from app.core.config import get_settings
from app.main import app
from app.rag import policies

client = TestClient(app)
ADMIN = {"X-Admin-Key": "test-admin-key"}
HL = {"loan_type": "Home Loan", "loan_amount": "40 lakh", "age": "32", "profile": "Salaried", "cibil": "760",
      "overdue": "No", "settled": "No", "existing_emi": "0", "pin": "411001", "hl_purpose": "Resale",
      "agreement_value": "60 lakh", "prop_pin": "411038", "possession": "Ready Possession", "legal_cat": "NA",
      "net_salary": "85000", "current_job_years": "3"}


def no_kb(monkeypatch):
    wf.build_graph.cache_clear()
    monkeypatch.setattr(wf, "search", lambda *a, **k: [])


def test_health():
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["structured_programs"] >= 5


def test_index_page():
    r = client.get("/")
    assert r.status_code == 200 and "Loan Copilot" in r.text


def test_classic_page():
    r = client.get("/classic")
    assert r.status_code == 200 and "/static/js/app.js" in r.text


def test_next_question_flow():
    r = client.post("/api/questionnaire/next", json={"case": {}}).json()
    assert r["question"]["key"] == "loan_type" and "Home Loan" in r["question"]["options"]
    r = client.post("/api/questionnaire/next", json={"case": {"loan_type": "Home Loan"}}).json()
    assert r["question"]["key"] == "loan_amount" and r["question"]["kind"] == "money" and not r["ready"]


def test_answer_endpoint_validates_and_skips():
    r = client.post("/api/questionnaire/answer", json={"case": {"loan_type": "Home Loan"}, "key": "cibil", "value": "999"})
    assert r.status_code == 422 and "CIBIL" in r.json()["detail"]
    r = client.post("/api/questionnaire/answer", json={"case": {"loan_type": "Home Loan"}, "key": "loan_amount",
                                                       "value": "45 lakh"}).json()
    assert r["case"]["loan_amount"] == "45 lakh"
    r = client.post("/api/questionnaire/answer", json={"case": {"loan_type": "Home Loan"}, "key": "name", "skip": True}).json()
    assert r["case"]["_skipped"] == ["name"] and any(s["skipped"] for s in r["summary"])


def test_saved_case_needs_its_token():
    r = client.post("/api/case/save", json={"case": {"loan_type": "LAP", "name": "Ravi"}}).json()
    cid, tok = r["case_id"], r["token"]
    assert client.get(f"/api/case/{cid}").status_code == 404
    assert client.get(f"/api/case/{cid}", headers={"X-Case-Token": "wrong"}).status_code == 404
    assert client.get(f"/api/case/{cid}", headers={"X-Case-Token": tok}).json()["case"]["loan_type"] == "LAP"
    assert client.post("/api/case/save", json={"case": {"loan_type": "LAP"}, "case_id": cid}).status_code == 403
    assert client.post("/api/case/save", json={"case": {"loan_type": "PL"}, "case_id": cid, "token": tok}).status_code == 200


def test_pin_validation():
    assert client.get("/api/pin/12").status_code == 400


def test_recommend_structured_path_without_llm_key(monkeypatch):
    no_kb(monkeypatch)
    r = client.post("/api/recommend", json={"case": HL})
    assert r.status_code == 200 and r.json()["top_lender"] == "Sample Lender A"
    audit = client.get("/api/audit", headers=ADMIN).json()
    assert audit[0]["status"] == "Strong Match" and "Ravi" not in json.dumps(audit)


def test_recommend_stream(monkeypatch):
    no_kb(monkeypatch)
    with client.stream("POST", "/api/recommend/stream", json={"case": HL}) as r:
        body = "".join(r.iter_text())
    events = [blk.split("\n")[0].removeprefix("event: ") for blk in body.strip().split("\n\n")]
    assert events[0] == "step" and events[-1] == "result"


def test_intake_and_chat_need_llm_key():
    assert client.post("/api/intake", json={"text": "salaried PL 5 lakh"}).status_code == 503
    assert client.post("/api/chat", json={"case": HL, "result": {}, "question": "why?"}).status_code == 503


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_per_min", 0)
    assert client.post("/api/intake", json={"text": "salaried PL 5 lakh"}).status_code == 429


def test_admin_endpoints_need_key():
    assert client.get("/api/audit").status_code == 401
    assert client.get("/api/policies").status_code == 401
    assert client.post("/api/ingest", files={"file": ("x.md", b"hello")}).status_code == 401


def test_ingest_structured_policy_and_reject_invalid():
    good = b"lender: Test Bank\nprograms:\n  - id: T-1\n    name: PL Basic\n    loan_type: Personal Loan\n    min_cibil: 700\n"
    stored = get_settings().policy_dir / "test_bank.yaml"
    try:
        r = client.post("/api/ingest", headers=ADMIN, files={"file": ("../../evil.yaml", good)})
        assert r.status_code == 200 and r.json()["programs"] == 1 and stored.exists()
        progs = client.get("/api/policies", headers=ADMIN).json()["structured_programs"]
        assert any(p["lender"] == "Test Bank" and p["stale"] for p in progs)
        bad = client.post("/api/ingest", headers=ADMIN, files={"file": ("x.yaml", b"lender: X\nprograms: nope\n")})
        assert bad.status_code == 422
    finally:
        stored.unlink(missing_ok=True)
        policies.reload()


def test_ingest_document_dedupe_and_delete(monkeypatch):
    added, deleted = {}, {}
    monkeypatch.setattr(vs, "add_documents", lambda docs, ids=None: added.update(ids=ids) or len(docs))
    monkeypatch.setattr(vs, "delete_documents", lambda ids: deleted.update(ids=ids))
    f = {"file": ("policy.txt", b"Real Bank LAP policy. CIBIL 700+.")}
    r = client.post("/api/ingest", headers=ADMIN, files=f, data={"lender": "Real Bank", "loan_type": "LAP"}).json()
    assert r["chunks_indexed"] == 1 and added["ids"][0].startswith(r["id"][:16])
    assert client.post("/api/ingest", headers=ADMIN, files=f).status_code == 409
    docs = client.get("/api/policies", headers=ADMIN).json()["documents"]
    assert docs[0]["lender"] == "Real Bank" and docs[0]["loan_type"] == "LAP"
    assert client.delete(f"/api/policies/doc/{r['id']}", headers=ADMIN).status_code == 200
    assert deleted["ids"] == added["ids"]
    assert client.post("/api/ingest", headers=ADMIN, files=f, data={"loan_type": "Bogus"}).status_code == 400
