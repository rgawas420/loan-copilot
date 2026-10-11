"""Graph routing with stubbed LLM / retriever / web (no network, no API keys)."""
from langchain_core.documents import Document

import app.rag.workflow as wf
from app.rag.state import EvidenceGrade, Explanation, Recommendation, WebGuidance

HL = {"loan_type": "Home Loan", "loan_amount": "40 lakh", "age": "32", "profile": "Salaried", "cibil": "760",
      "overdue": "No", "settled": "No", "existing_emi": "0", "pin": "411001", "hl_purpose": "Resale",
      "agreement_value": "60 lakh", "prop_pin": "411038", "possession": "Ready Possession", "legal_cat": "NA",
      "net_salary": "85000", "current_job_years": "3", "name": "Ravi Kumar", "mobile": "9876543210",
      "employer": "Acme Pvt Ltd"}
EDU = {"loan_type": "Education Loan", "loan_amount": "20 lakh", "age": "45", "profile": "Salaried", "cibil": "760",
       "overdue": "No", "settled": "No", "existing_emi": "0", "pin": "411001", "course": "MS", "country": "USA",
       "course_cost": "40 lakh", "coapp_income": "18 lakh"}
EVIDENCE = ("Lender: Real Bank\nProgram: Education Loan\nLoan type: Education Loan\nCIBIL 700+.\n"
            "Banker/RM: Asha | Mobile: 90000-11111 | Urban Money Code: UM-RB-01 | Policy last verified: 2026-09-01")
DOC = Document(page_content=EVIDENCE, metadata={"source": "rb.md", "loan_type": "Education Loan",
                                                "lender": "Real Bank", "program": "EDU"})
UNTAGGED = Document(page_content="general policy text", metadata={"source": "x.pdf", "loan_type": "Unknown"})


class FakeLLM:
    def __init__(self, grades=(), rec=None):
        self.grades, self.rec, self.prompts = list(grades), rec, []

    def with_structured_output(self, schema, **kw):
        outer = self

        class R:
            def with_retry(self, **kw): return self

            def invoke(self, prompt):
                outer.prompts.append(prompt)
                if schema is Recommendation: return outer.rec
                if schema is Explanation: return Explanation(summary="s", why_suitable=["LLM reason"], next_steps=[])
                if schema is WebGuidance: return WebGuidance(summary="general", general_criteria=["CIBIL 700+"])
                return EvidenceGrade(grade=outer.grades.pop(0))
        return R()

    def invoke(self, prompt):
        self.prompts.append(prompt)

        class M: content = "rewritten query"
        return M()


def run(monkeypatch, case, docs=(DOC,), llm=None, web=True):
    wf.build_graph.cache_clear()
    monkeypatch.setattr(wf, "_llm", lambda kind="fast": llm)
    monkeypatch.setattr(wf, "search", lambda *a, **k: list(docs))

    class W:
        def invoke(self, _): return {"answer": "web says", "results": [{"title": "t", "url": "u", "content": "c"}]}
    monkeypatch.setattr(wf, "_web", lambda: W() if web else None)
    return wf.run_recommendation(case)


def rec(**kw):
    base = dict(status="Strong Match", top_lender="Real Bank", program="EDU", indicative_eligibility="ok",
                why_suitable=["a"], concerns_or_deviations=[], banker_rm="Asha", mobile="90000-11111",
                urban_money_code="UM-RB-01", alternative_lenders=[], policy_last_verified="2026-09-01")
    return Recommendation(**{**base, **kw})


def test_missing_info_stops_early(monkeypatch):
    r = run(monkeypatch, {"loan_type": "Personal Loan"})
    assert r["status"] == "More Information Required" and r["missing_fields"]


def test_structured_path_works_without_llm(monkeypatch):
    r = run(monkeypatch, HL, docs=())
    assert r["evidence_source"] == "structured_policy" and r["status"] == "Strong Match"
    assert r["top_lender"] == "Sample Lender A" and r["urban_money_code"] == "UM-SAMPLE-A01"
    assert {c["lender"] for c in r["comparison"]} == {"Sample Lender A", "Sample Lender B"}
    assert any("template" in t for t in r["trace"])


def test_structured_path_llm_only_explains_and_never_sees_pii(monkeypatch):
    llm = FakeLLM()
    r = run(monkeypatch, HL, llm=llm)
    assert r["why_suitable"] == ["LLM reason"] and r["status"] == "Strong Match"
    prompt = " ".join(llm.prompts)
    assert "Ravi" not in prompt and "9876543210" not in prompt and "Acme" not in prompt


def test_structured_not_suitable_names_closest(monkeypatch):
    r = run(monkeypatch, {**HL, "cibil": "600"}, docs=())
    assert r["status"] == "Not Suitable" and r["top_lender"] is None and "Closest" in r["message"]


def test_text_path_caps_status_and_rejects_invented_contacts(monkeypatch):
    llm = FakeLLM(rec=rec(urban_money_code="UM-INVENTED-99"))
    r = run(monkeypatch, EDU, llm=llm)
    assert r["evidence_source"] == "private_kb"
    assert r["status"] == "Possible - Credit Check Required"           # text-only is never Strong Match
    assert r["banker_rm"] == "Asha" and r["urban_money_code"] == "Not available in KB"
    assert any("tagged" in t for t in r["trace"])                      # graded without an LLM call


def test_text_path_none_lender_becomes_null(monkeypatch):
    r = run(monkeypatch, EDU, llm=FakeLLM(rec=rec(status="Not Suitable", top_lender="None")))
    assert r["top_lender"] is None


def test_weak_kb_rewrites_once_then_web_gives_general_guidance_only(monkeypatch):
    r = run(monkeypatch, EDU, docs=(UNTAGGED,), llm=FakeLLM(grades=["weak", "weak"]))
    assert sum("Query rewritten" in t for t in r["trace"]) == 1
    assert sum("Web search ->" in t for t in r["trace"]) == 1            # the web is searched once, not twice
    assert r["evidence_source"] == "web_general" and r["top_lender"] is None
    assert r["general_criteria"] == ["CIBIL 700+"]


def test_no_kb_no_web_key_is_insufficient(monkeypatch):
    r = run(monkeypatch, EDU, docs=(), llm=None, web=False)
    assert r["status"] == "More Information Required" and r["evidence_source"] == "none"


def test_stream_yields_steps_then_result(monkeypatch):
    wf.build_graph.cache_clear()
    monkeypatch.setattr(wf, "_llm", lambda kind="fast": None)
    monkeypatch.setattr(wf, "search", lambda *a, **k: [])
    events = list(wf.stream_recommendation(HL))
    assert events[0][0] == "step" and events[-1][0] == "result"
    assert events[-1][1]["status"] == "Strong Match" and len(events[-1][1]["trace"]) == len(events) - 1
