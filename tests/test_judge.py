"""The claim-level judge, its part in golden runs and scores, and the credit team's review workbook."""
from openpyxl import Workbook

from app.rag import judge
from app.rag.judge import JudgedClaim, Judgement
from app.services import rag_debug
from evals import build_golden_review, import_golden_review

TRACE = {"prompt": {"full": "--- SYSTEM ---\nrules\n\n--- HUMAN ---\nKNOWLEDGE BASE CONTEXT:\n\n[S1] Bajaj Finserv | "
                            "CHETAN RATHOD | 9604454809", "context": "[S1] ..."}}


def _clear():
    rag_debug.init_db()
    for g in rag_debug.golden_list():
        rag_debug.golden_delete(g["id"])


def test_refusals_are_judged_without_the_llm():
    nothing = judge.judge("Zentrix payout?", "I couldn't find this information in the available knowledge base.", {},
                          "(not found)", not_found=True)
    assert nothing["verdict"] == "PASS" and nothing["correctness"] == "correct" and nothing["how"] == "rule"
    false = judge.judge("Bajaj RM Pune?", "x", {}, "Chetan Rathod, 9604454809", not_found=True)
    assert false["verdict"] == "FAIL" and false["correctness_reason"].startswith("False refusal")


def test_app_written_replies_are_not_judged_as_model_claims():
    v = judge.judge("Salaried, 90k, wants 30 lakh LAP", "Which city is the property in?", {"app_written": True}, "city")
    assert v["verdict"] == "PASS" and v["how"] == "app" and v["claims"] == 0


def test_no_judge_model_means_no_verdict():
    assert judge.judge("q", "An answer [S1]", TRACE, "ref") is None          # no GROQ_API_KEY in tests


def test_an_unsupported_claim_fails_and_a_wrong_citation_warns(monkeypatch):
    from app.rag import llm
    seen = {}

    def fake_structured(_llm, schema, prompt):
        seen["prompt"] = prompt
        return Judgement(claims=[
            JudgedClaim(claim="RM is Chetan Rathod, 9604454809", cited=["【S1】"], verdict="Supported"),
            JudgedClaim(claim="Bajaj needs CIBIL 750", cited=["S1"], verdict="unsupported", reason="not in S1"),
            JudgedClaim(claim="Verify with the lender", verdict="not checkable")], correctness="correct")

    monkeypatch.setattr(llm, "get_llm", lambda kind="fast", max_tokens=None: object())
    monkeypatch.setattr(llm, "structured", fake_structured)
    v = judge.judge("Bajaj RM Pune?", "Chetan Rathod 9604454809 [S1]. Needs CIBIL 750 [S1].", TRACE, "Chetan Rathod")
    assert v["verdict"] == "FAIL" and v["faithfulness"] == 0.5 and v["claims"] == 2 and v["not_checkable"] == 1
    assert v["problems"][0]["claim"] == "Bajaj needs CIBIL 750" and v["claims_detail"][0]["cited"] == ["S1"]
    assert "9604454809" in seen["prompt"] and "--- SYSTEM ---" not in seen["prompt"]      # sources, not the rules

    monkeypatch.setattr(llm, "structured", lambda *a: Judgement(claims=[
        JudgedClaim(claim="RM is Chetan Rathod", cited=["S2"], verdict="supported", citation_ok=False)], correctness="correct"))
    v = judge.judge("Bajaj RM Pune?", "Chetan Rathod [S2]", TRACE, "Chetan Rathod")
    assert v["verdict"] == "WARN" and v["citation_errors"] == 1 and v["faithfulness"] == 1.0

    # an answer to a question the knowledge base cannot answer is incorrect, whatever its claims
    monkeypatch.setattr(llm, "structured", lambda *a: Judgement(claims=[
        JudgedClaim(claim="Payout is 1%", cited=["S1"], verdict="supported")], correctness="correct"))
    assert judge.judge("Zentrix payout?", "Payout is 1% [S1]", TRACE, "(not found)")["verdict"] == "FAIL"


def test_a_golden_run_uses_history_the_judge_and_scores(monkeypatch):
    from app.rag import chat
    _clear()
    calls = []

    def fake_run(mode, question, loan_type=None, history=None, origin="api", **k):
        calls.append(history)
        nf = question.startswith("Zentrix")
        return {"answer": "I couldn't find this information in the available knowledge base." if nf else "Chetan Rathod 9604454809 [S1]",
                "sources": [], "not_found": nf, "trace": TRACE}

    verdicts = iter([{"verdict": "FAIL", "faithfulness": 0.5, "unsupported": 1, "citation_errors": 0, "correctness": "correct"},
                     {"verdict": "PASS", "faithfulness": None, "unsupported": 0, "citation_errors": 0, "correctness": "correct"}])
    monkeypatch.setattr(chat, "run", fake_run)
    monkeypatch.setattr(judge, "judge", lambda *a, **k: next(verdicts))
    rag_debug.golden_save({"question": "what about Mumbai?", "expected_answer": "9604454809", "category": "followup",
                           "history": "Who is the Bajaj RM in Pune?\n", "reference_answer": "Chetan Rathod"})
    rag_debug.golden_save({"question": "Zentrix payout?", "expected_answer": "(not found)", "category": "missing"})
    run = rag_debug.run_detail(rag_debug.run_golden("test"))
    assert calls[0] == [{"role": "user", "text": "Who is the Bajaj RM in Pune?"}] and calls[1] == []
    first, second = run["results"]
    assert not first["passed"] and first["judge"]["verdict"] == "FAIL" and first["answer"] == "CORRECT"
    assert second["passed"] and second["not_found"]
    s = run["scores"]
    assert s["overall"]["pass_rate"] == 50.0 and s["overall"]["faithfulness"] == 50.0
    assert s["overall"]["unsupported_claims"] == 1 and s["overall"]["correct_refusals"] == 100.0
    assert s["overall"]["false_refusals"] == 0 and set(s["by_category"]) == {"followup", "missing"}
    assert run["config"]["judge"]["prompt_version"] == judge.JUDGE_PROMPT_VERSION
    _clear()


class RateLimitError(Exception):
    pass


def test_a_per_minute_limit_is_waited_out_and_a_daily_limit_stops_a_resumable_run(monkeypatch):
    import time
    from app.rag import chat
    _clear()
    monkeypatch.setattr(rag_debug.get_settings(), "eval_judge", False)
    slept, script = [], {}

    def fake_run(mode, question, **k):
        step = script[question].pop(0)
        if step != "ok":
            raise RateLimitError(step)
        return {"answer": "x 1", "sources": [], "not_found": False, "trace": {}}

    monkeypatch.setattr(chat, "run", fake_run)
    monkeypatch.setattr(time, "sleep", slept.append)
    for q in ("one?", "two?", "three?"):
        rag_debug.golden_save({"question": q, "expected_answer": "1"})
    script.update({"one?": ["Rate limit reached on tokens per minute (TPM). Please try again in 7.5s.", "ok"],
                   "two?": ["Rate limit reached on tokens per day (TPD). Please try again in 21m30.8s."],
                   "three?": ["ok"]})
    run = rag_debug.run_detail(rag_debug.run_golden("test"))
    assert slept == [8.5] and run["status"] == "stopped at 2/3: AI provider daily limit"
    assert [r["question"] for r in run["results"]] == ["one?"] and run["results"][0]["passed"]

    script.update({"two?": ["ok"], "three?": ["ok"], "one?": []})          # one? must not run again
    resumed = rag_debug.run_detail(rag_debug.run_golden("resume", resume=run["id"]))
    assert resumed["status"] == "finished" and resumed["passed"] == 3
    assert [r["question"] for r in resumed["results"]] == ["one?", "two?", "three?"]
    import pytest
    with pytest.raises(ValueError):
        rag_debug.resumable(resumed["id"])                                  # finished runs can't be resumed
    _clear()


def test_an_empty_openai_account_stops_the_run_with_a_clear_reason(monkeypatch):
    from app.rag import chat
    from app.rag.llm import is_rate_limit, rate_limit_message
    _clear()
    e = RateLimitError("Error code: 429 - {'error': {'message': 'You have no credits remaining.', "
                       "'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}}")
    assert is_rate_limit(e) and "run out of credit" in rate_limit_message(e)

    def broke(*a, **k):
        raise e
    monkeypatch.setattr(chat, "run", broke)
    rag_debug.golden_save({"question": "one?", "expected_answer": "1"})
    run = rag_debug.run_detail(rag_debug.run_golden("test"))
    assert run["status"] == "stopped at 1/1: AI provider account has no credit left (add credit, then resume)"
    _clear()


def test_the_judge_can_run_on_openai(monkeypatch):
    from app.rag import llm
    s = rag_debug.get_settings()
    monkeypatch.setattr(s, "eval_judge_provider", "openai")
    monkeypatch.setattr(s, "eval_judge_model", "")
    assert llm.get_llm("judge") is None                                    # no OPENAI_API_KEY
    monkeypatch.setattr(s, "openai_api_key", "sk-test")
    j = llm.get_llm("judge")
    assert type(j).__name__ == "ChatOpenAI" and llm.model_name(j) == llm.OPENAI_JUDGE_DEFAULT
    assert rag_debug.judge_config()["provider"] == "openai"
    llm._build_openai.cache_clear()


def test_the_judge_does_not_change_the_configuration_hash():
    cfg = rag_debug.config()
    assert rag_debug.config_hash(cfg) == rag_debug.config_hash({**cfg, "judge": {"model": "other"}})


def test_review_workbook_round_trip(tmp_path):
    _clear()
    path = tmp_path / "review.xlsx"
    n = build_golden_review.build(path)
    assert n >= 120
    items, problems = import_golden_review.read(path)
    assert items == [] and problems == []                       # nothing approved yet

    wb = Workbook()
    ws = wb.active
    ws.title = import_golden_review.SHEET
    ws.append(build_golden_review.HEADERS)
    blank = {h: None for h in build_golden_review.HEADERS}
    ws.append(list({**blank, "ID": "G001", "Category": "banker", "Question": "Who is the home loan RM for Bajaj Finserv in Pune?",
                    "Should the app answer?": "Answer", "Reference answer": "Chetan Rathod, 9604454809",
                    "Must contain": "9604454809", "Expected document": "Pan India RM list Aug", "Expected page": 3,
                    "Reviewed by": "Priya", "Approved?": "Yes"}.values()))
    ws.append(list({**blank, "ID": "G002", "Category": "missing", "Question": "Zentrix payout?",
                    "Should the app answer?": "Not found", "Reviewed by": "Priya", "Approved?": "Yes"}.values()))
    ws.append(list({**blank, "ID": "G003", "Question": "No reference", "Should the app answer?": "Answer",
                    "Reviewed by": "Priya", "Approved?": "Yes"}.values()))
    ws.append(list({**blank, "ID": "G004", "Question": "Dropped", "Approved?": "No"}.values()))
    wb.save(path)
    items, problems = import_golden_review.read(path)
    assert [i["question"][:7] for i in items] == ["Who is ", "Zentrix"] and len(problems) == 1 and "G003" in problems[0]
    assert items[0]["expected_page"] == 3 and items[0]["approved_by"] == "Priya"
    assert items[1]["reference_answer"] == items[1]["expected_answer"] == "(not found)"

    rag_debug.golden_save({"question": "who is the home loan RM for Bajaj Finserv in  Pune?", "expected_answer": "x"})
    assert import_golden_review.apply(items) == (1, 1)          # the existing question is updated, not duplicated
    g = {x["question"][:7]: x for x in rag_debug.golden_list()}
    assert len(g) == 2 and g["Who is "]["reference_answer"] == "Chetan Rathod, 9604454809"     # reviewer's wording
    assert g["Who is "]["category"] == "banker" and "review: G001" in g["Who is "]["notes"]
    _clear()
