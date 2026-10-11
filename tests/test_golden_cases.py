"""Golden cases (evals/golden_cases.yaml) through the full graph, without an LLM or KB: the decision must
come from the rules engine alone, so these results are deterministic."""
import pytest

import app.rag.workflow as wf
from evals.run_evals import load_cases

CASES = list(load_cases())


@pytest.mark.parametrize("cid,case,expect", CASES, ids=[c[0] for c in CASES])
def test_golden_case(monkeypatch, cid, case, expect):
    wf.build_graph.cache_clear()
    monkeypatch.setattr(wf, "_llm", lambda kind="fast": None)
    monkeypatch.setattr(wf, "_web", lambda: None)
    monkeypatch.setattr(wf, "search", lambda *a, **k: [])
    r = wf.run_recommendation(case)
    assert (r["status"], r.get("top_lender")) == (expect["status"], expect["lender"])
