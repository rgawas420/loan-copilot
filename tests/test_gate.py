"""Release gate: a run must do at least as well as the approved baseline run on the golden tests it shares."""
import json
from contextlib import closing

import pytest

from app.services import rag_debug
from evals import gate as gate_cli


def _case(i, passed=False, faith=1.0, unsupported=0, correctness="correct", not_found=False, expected="x", error=None):
    if error:
        return {"id": i, "question": f"q{i}?", "category": "banker", "passed": False, "error": error}
    return {"id": i, "question": f"q{i}?", "category": "banker", "passed": passed, "not_found": not_found,
            "expected_answer": expected, "retrieval": "GOOD", "answer": "CORRECT", "citations": "CORRECT",
            "judge": {"verdict": "PASS" if passed else "FAIL", "faithfulness": faith, "unsupported": unsupported,
                      "citation_errors": 0, "correctness": correctness}}


def _run(results, status="finished", config_hash="old"):
    rag_debug.init_db()
    with closing(rag_debug._conn()) as c:
        rid = c.execute("INSERT INTO rag_eval_runs(started, finished, trigger, status, config_hash, config, total, passed, "
                        "results) VALUES ('2026-10-11T00:00:00+00:00', '2026-10-11T00:10:00+00:00', 'test', ?, ?, '{}', ?, ?, ?)",
                        (status, config_hash, len(results), sum(1 for r in results if r.get("passed")),
                         json.dumps(results))).lastrowid
        c.commit()
    return rid


@pytest.fixture(autouse=True)
def _clean():
    yield
    with closing(rag_debug._conn()) as c:
        c.execute("DELETE FROM rag_eval_runs WHERE trigger='test'")
        c.commit()


def test_no_baseline_then_baseline_then_pass():
    base = _run([_case(1, True), _case(2, False, faith=0.5, unsupported=1)])
    assert rag_debug.run_detail(base)["gate"]["verdict"] == "NO_BASELINE"
    rag_debug.set_baseline(base)
    assert rag_debug.run_detail(base)["gate"]["verdict"] == "BASELINE"
    better = rag_debug.run_detail(_run([_case(1, True), _case(2, True), _case(3, True)]))
    g = better["gate"]
    assert g["verdict"] == "PASS" and g["compared"] == 2 and g["new_cases"] == 1
    assert [c["id"] for c in g["fixed"]] == [2] and g["regressions"] == []
    assert g["deltas"]["faithfulness"] == [75.0, 100.0] and g["deltas"]["unsupported_claims"] == [1, 0]


def test_a_regression_or_a_worse_score_fails():
    rag_debug.set_baseline(_run([_case(1, True), _case(2, True), _case(3, True, not_found=True, expected="(not found)")]))
    g = rag_debug.run_detail(_run([_case(1, False), _case(2, True, faith=0.9, unsupported=1),
                                   _case(3, True, not_found=False, expected="(not found)")]))["gate"]
    assert g["verdict"] == "FAIL" and [c["id"] for c in g["regressions"]] == [1]
    text = " ".join(g["reasons"])
    assert "now fail" in text and "Faithfulness fell" in text and "Unsupported claims rose from 0 to 1" in text
    assert "Correct refusals fell from 100.0% to 0.0%" in text


def test_judge_noise_within_tolerance_passes_but_errors_and_unfinished_runs_do_not():
    rag_debug.set_baseline(_run([_case(1, True, faith=1.0), _case(2, True, faith=1.0)]))
    assert rag_debug.run_detail(_run([_case(1, True, faith=0.99), _case(2, True)]))["gate"]["verdict"] == "PASS"
    g = rag_debug.run_detail(_run([_case(1, True), _case(2, error="RateLimitError: ...")]))["gate"]
    assert g["verdict"] == "FAIL" and "could not run" in g["reasons"][0]
    stopped = _run([_case(1, True)], status="stopped at 2/2: AI provider daily limit")
    assert rag_debug.run_detail(stopped)["gate"]["verdict"] == "INCOMPLETE"
    with pytest.raises(ValueError):
        rag_debug.set_baseline(stopped)


def test_the_cli_gates_the_current_configuration(capsys):
    rag_debug.set_baseline(_run([_case(1, True)]))
    assert gate_cli.main([]) == 2 and "UNTESTED" in capsys.readouterr().out           # current config never ran
    current = rag_debug.config_hash()
    _run([_case(1, False)], config_hash=current)
    assert gate_cli.main([]) == 1 and "regressed: [banker] q1?" in capsys.readouterr().out
    rid = _run([_case(1, True)], config_hash=current)
    assert gate_cli.main([]) == 0 and f"run {rid}" in capsys.readouterr().out
    assert gate_cli.main(["--set-baseline", str(rid)]) == 0 and rag_debug.baseline_id() == rid
