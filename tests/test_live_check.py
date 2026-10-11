"""Live claim check: every claim of a chat answer is checked against the passages; an unsupported claim gets one
rewrite; partners see a confidence level."""
from types import SimpleNamespace

import pytest

import app.rag.chat as chat
from app.core.config import get_settings
from app.rag import live_check
from tests.test_chat_stream import DOCS, _events, _kb, _user


class FakeLLM:
    """Streams the first answer; returns the rewrite when asked to correct it."""

    def __init__(self, answer="Axis code is 6159MUM [S1]. Axis also funds self-employed customers [S1].", rewrite="Axis code is 6159MUM [S1]."):
        self.answer, self.rewrite, self.invoked = answer, rewrite, []

    def stream(self, msgs):
        yield type("Chunk", (), {"content": self.answer})()

    def invoke(self, msgs):
        self.invoked.append(msgs)
        return type("Msg", (), {"content": self.rewrite})()


def _verdict(unsupported=(), partial=(), error=False):
    if error:
        return {"verdict": "ERROR", "error": "boom"}
    problems = [{"claim": c, "verdict": "unsupported"} for c in unsupported] + [{"claim": c, "verdict": "partial"} for c in partial]
    return {"verdict": "FAIL" if unsupported else "WARN" if partial else "PASS", "claims": 2 + len(problems),
            "supported": 2, "unsupported": len(unsupported), "partial": len(partial), "problems": problems}


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(get_settings(), "live_check", True)
    _kb(monkeypatch, DOCS)
    checks = []

    def use(llm, *verdicts):
        it = iter(verdicts)
        monkeypatch.setattr(chat, "get_llm", lambda kind="fast", max_tokens=None: llm)

        def fake_check(prep, answer, fast):
            checks.append(answer)
            return next(it)
        monkeypatch.setattr(live_check, "check", fake_check)
    return use, checks


def _ask():
    c, _ = _user()
    r = c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?"})
    assert r.status_code == 200
    return _events(r.text)


def test_a_fully_supported_answer_gets_high_confidence(live):
    use, checks = live
    llm = FakeLLM()
    use(llm, _verdict())
    ev = _ask()
    conf = dict(ev)["confidence"]
    assert conf["level"] == "high" and conf["reasons"] == ["Every fact in this answer matches the documents."]
    assert "replace" not in dict(ev) and not llm.invoked
    assert any(k == "status" and p["text"] == "Checking every fact against the documents" for k, p in ev)


def test_an_unsupported_claim_is_rewritten_and_the_rewrite_rechecked(live):
    use, checks = live
    llm = FakeLLM()
    use(llm, _verdict(unsupported=["Axis also funds self-employed customers"]), _verdict())
    ev = dict(_ask())
    assert ev["replace"] == "Axis code is 6159MUM [S1]." and ev["confidence"]["level"] == "high"
    assert ev["confidence"]["corrected"] and checks == [FakeLLM().answer, "Axis code is 6159MUM [S1]."]
    assert '"Axis also funds self-employed customers"' in llm.invoked[0][-1].content


def test_a_claim_that_survives_the_rewrite_means_low_confidence(live):
    use, _ = live
    use(FakeLLM(), _verdict(unsupported=["Axis also funds self-employed customers"]), _verdict(unsupported=["Axis also funds self-employed customers"]))
    ev = dict(_ask())
    assert "replace" not in ev and ev["confidence"]["level"] == "low"
    assert ev["confidence"]["reasons"][0] == "Could not confirm: Axis also funds self-employed customers"


def test_partial_claims_and_a_failed_check_mean_medium(live):
    use, _ = live
    use(FakeLLM(), _verdict(partial=["Axis code is for all home loans"]))
    conf = dict(_ask())["confidence"]
    assert conf["level"] == "medium" and conf["reasons"][0].startswith("Only partly stated in the documents")
    use(FakeLLM(), _verdict(error=True))
    conf = dict(_ask())["confidence"]
    assert conf["level"] == "medium" and "could not be checked" in conf["reasons"][0]


def test_source_warnings_lower_confidence_and_refusals_get_none():
    prep = SimpleNamespace(not_found=False, direct="", lookup=None, bankers=None, facts=None,
                           retrieval=SimpleNamespace(validation={"sufficient": True}))
    post = {"sources": [{"ref": "S1", "warnings": ["OCR, not verified", "expired 2026-09-30"], "older_edition": True}],
            "unsupported_figures": [], "contact_mismatches": [], "not_found": False}
    conf = live_check.confidence(prep, post, _verdict())
    assert conf["level"] == "medium" and len(conf["reasons"]) == 3 and "scanned document" in conf["reasons"][0]
    post["unsupported_figures"] = ["2%"]
    assert live_check.confidence(prep, post, _verdict())["level"] == "low"
    assert live_check.confidence(SimpleNamespace(**{**vars(prep), "not_found": True}), post, None) is None
