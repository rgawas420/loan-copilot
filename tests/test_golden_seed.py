"""The golden seed loads once, and golden results are scored on what the model was given."""
from app.services import rag_debug
from evals import seed_rag_golden


def test_seed_is_idempotent_and_valid():
    cases = seed_rag_golden.load()
    assert len(cases) >= 40 and all(c["question"] and (c.get("expected_answer") or c.get("expected_document")) for c in cases)
    first = seed_rag_golden.seed()
    assert len(first) == len(cases) and seed_rag_golden.seed() == []
    assert {g["notes"] for g in rag_debug.golden_list()} >= {"seed: banker", "seed: missing", "seed: profile"}
    for g in rag_debug.golden_list():
        rag_debug.golden_delete(g["id"])


def test_evaluation_uses_the_context_and_knows_not_found():
    result = {"answer": "| 1 | Bajaj Finserv | CHETAN RATHOD [S1] | 9604454809 |",
              "sources": [{"ref": "S1", "source": "03/Pan India RM list Augest 2026..xlsx", "title": "Pan India RM list Augest 2026."}],
              "trace": {"context_refs": [{"ref": "S1", "source": "03/Pan India RM list Augest 2026..xlsx",
                                          "title": "Pan India RM list Augest 2026."}], "retrieval": {"candidates": []}}}
    ev = rag_debug.evaluate(result, "9604454809", "Pan India RM list Augest 2026")
    assert ev == {"retrieval": "GOOD", "answer": "CORRECT", "citations": "CORRECT", "rank": 1, "passed": True}
    assert rag_debug.evaluate({"answer": "x", "not_found": True}, "(not found)")["passed"]
    assert not rag_debug.evaluate({"answer": "Payout is 1%", "not_found": False}, "(not found)")["passed"]


def test_a_golden_run_stops_at_the_providers_rate_limit(monkeypatch):
    from app.rag import chat

    class RateLimitError(Exception):
        pass

    calls = []

    def limited(*a, **k):
        calls.append(1)
        raise RateLimitError("Rate limit reached ... tokens per day (TPD). Please try again in 21m30.8s.")

    for q in ("one?", "two?", "three?"):
        rag_debug.golden_save({"question": q, "expected_answer": "x"})
    monkeypatch.setattr(chat, "run", limited)
    rid = rag_debug.run_golden("test")
    run = rag_debug.run_detail(rid)
    assert len(calls) == 1 and run["status"].startswith("stopped at 1/3")
    for g in rag_debug.golden_list():
        rag_debug.golden_delete(g["id"])


def test_rate_limit_message_is_readable():
    from app.rag.llm import is_rate_limit, rate_limit_message

    class RateLimitError(Exception):
        pass

    e = RateLimitError("429 Rate limit reached on tokens per day (TPD). Please try again in 21m30.8s.")
    assert is_rate_limit(e) and "daily usage limit" in rate_limit_message(e) and "about 22 minutes" in rate_limit_message(e)
