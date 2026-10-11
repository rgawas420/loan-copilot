"""LLM_PROVIDER=openai: answers, rewrites and intake on OpenAI; the plan's Groq model list and output cap do not
apply; images stay on Groq."""
import pytest

from app.rag import llm, metering
from app.services import rag_debug


@pytest.fixture
def openai(monkeypatch):
    s = llm.get_settings()
    for k, v in {"llm_provider": "openai", "openai_api_key": "sk-test", "groq_api_key": "gsk-test", "rag_model": "",
                 "rag_reasoning_effort": "", "openai_model": "gpt-5-mini", "openai_model_smart": "gpt-5-mini",
                 "vision_model": "qwen/qwen3.8-27b"}.items():
        monkeypatch.setattr(s, k, v)
    yield s
    llm._build_openai.cache_clear()
    llm._build.cache_clear()


def test_answers_and_fast_calls_use_openai_reasoning_models(openai):
    rag = llm.get_llm("rag")
    assert type(rag).__name__ == "ChatOpenAI" and rag.model_name == "gpt-5-mini"
    assert rag.reasoning_effort == "low" and rag.temperature is None and rag.max_tokens == 8000
    assert rag.stream_usage and metering.openai_callback in rag.callbacks
    fast = llm.get_llm("fast")
    assert type(fast).__name__ == "ChatOpenAI" and fast.model_name == "gpt-5-mini"
    assert llm.get_llm("rag", max_tokens=12000).max_tokens == 12000
    openai.rag_model, openai.rag_reasoning_effort = "gpt-5", "medium"
    rag = llm.get_llm("rag")
    assert rag.model_name == "gpt-5" and rag.reasoning_effort == "medium"


def test_images_stay_on_groq_and_a_missing_key_means_no_model(openai, monkeypatch):
    assert type(llm.get_llm("vision")).__name__ == "ChatGroq"
    monkeypatch.setattr(openai, "openai_api_key", "")
    assert llm.get_llm("rag") is None and not llm.configured()
    from app.rag import chat
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        chat._llm()


def test_the_plans_groq_models_and_output_cap_do_not_apply_on_openai(openai):
    plan = {"allowed_models": ["openai/gpt-oss-20b"], "max_output_tokens": 600}
    token = metering._meter.set(type("M", (), {"plan": plan})())
    try:
        rag = llm.get_llm("rag")
    finally:
        metering._meter.reset(token)
    assert rag.model_name == "gpt-5-mini" and rag.max_tokens == 8000


def test_the_configuration_names_the_provider_and_changes_its_hash(openai, monkeypatch):
    cfg = rag_debug.config()
    assert cfg["provider"] == "openai" and cfg["model"] == "gpt-5-mini" and cfg["rewrite_model"] == "gpt-5-mini"
    assert cfg["temperature"] == "n/a" and cfg["reasoning_effort"] == "low" and cfg["llm_configured"]
    openai_hash = rag_debug.config_hash(cfg)
    monkeypatch.setattr(openai, "llm_provider", "groq")
    groq = rag_debug.config()
    assert groq["provider"] == "groq" and groq["model"] == "openai/gpt-oss-120b" and groq["temperature"] == 0.0
    assert rag_debug.config_hash(groq) != openai_hash                   # a provider switch is a new configuration
