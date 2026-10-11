"""Chat models on Groq or OpenAI (LLM_PROVIDER). Returns None when the provider's API key is missing so callers
can fall back gracefully.

Every model reports its token usage to the usage ledger (app/rag/metering.py). Inside a metered request on Groq,
the user's plan decides the answer model (its allowed models) and caps the output tokens. On OpenAI the plan's
model list (Groq model names) does not apply, and output is not capped by the plan: OpenAI's reasoning models
count their hidden reasoning as output, so a small cap would cut answers off.

Images are always read on Groq (VISION_MODEL); the golden-test judge has its own provider (EVAL_JUDGE_PROVIDER)."""
from functools import lru_cache
from typing import Optional

from app.core.config import get_settings


OPENAI_JUDGE_DEFAULT = "gpt-5"


def provider() -> str:
    return "openai" if get_settings().llm_provider == "openai" else "groq"


def model_for(kind: str) -> str:
    """The model for a kind of call ("fast", "smart", "rag") on the configured provider."""
    s = get_settings()
    if provider() == "openai":
        return (s.rag_model or s.openai_model_smart) if kind == "rag" else s.openai_model_smart if kind == "smart" else s.openai_model
    return (s.rag_model or s.groq_model_smart) if kind == "rag" else s.groq_model_smart if kind == "smart" else s.groq_model


def configured() -> bool:
    s = get_settings()
    return bool(s.openai_api_key if provider() == "openai" else s.groq_api_key)


def judge_model() -> str:
    s = get_settings()
    if s.eval_judge_provider == "openai":
        return s.eval_judge_model or OPENAI_JUDGE_DEFAULT
    return s.eval_judge_model or s.groq_model_smart


def get_llm(kind: str = "fast", max_tokens: Optional[int] = None):
    """max_tokens: a larger answer budget for one kind of answer (the plan's output cap still applies on Groq)."""
    s = get_settings()
    if kind == "judge" and s.eval_judge_provider == "openai":
        if not s.openai_api_key:
            return None
        return _build_openai(_freeze({"model": judge_model(), "max_tokens": 16000, "timeout": max(s.llm_timeout_s, 180),
                                      **({"reasoning_effort": s.eval_judge_reasoning_effort} if s.eval_judge_reasoning_effort else {})}))
    if kind not in ("vision", "judge") and provider() == "openai":
        if not s.openai_api_key:
            return None
        params = rag_params() if kind == "rag" else {"model": model_for(kind), "max_tokens": s.openai_max_output_tokens,
                                                     "reasoning_effort": s.openai_reasoning_effort}
        if max_tokens:
            params = {**params, "max_tokens": max(params["max_tokens"], max_tokens)}
        params = {k: v for k, v in params.items() if v not in ("", None)}
        return _build_openai(_freeze({**params, "timeout": max(s.llm_timeout_s, 120)}))
    if not s.groq_api_key:
        return None
    from app.rag import metering
    meter = metering.current()
    plan = meter.plan if meter else {}
    if kind == "vision":
        if not s.vision_model:
            return None
        params = {"model": s.vision_model, "temperature": 0, "max_tokens": 3000}
    elif kind == "judge":           # golden-test judge: never limited by a user's plan
        params = {"model": judge_model(), "temperature": 0, "max_tokens": 6000}
        if s.eval_judge_reasoning_effort:
            params["reasoning_effort"] = s.eval_judge_reasoning_effort
        return _build(_freeze(params))
    else:
        params = rag_params() if kind == "rag" else {"model": model_for(kind), "temperature": 0}
    if max_tokens:
        params = {**params, "max_tokens": max(params.get("max_tokens") or 0, max_tokens)}
    if plan:
        allowed = plan.get("allowed_models") or []
        if kind in ("smart", "rag") and allowed and params["model"] not in allowed:
            params = {**params, "model": allowed[-1]}           # the plan's best model (list them basic -> advanced)
        cap = plan.get("max_output_tokens")
        if cap and kind in ("smart", "rag"):
            params = {**params, "max_tokens": min(params.get("max_tokens") or cap, cap)}
    return _build(_freeze(params))


def _freeze(p: dict) -> tuple:
    return tuple(sorted((k, tuple(sorted(v.items())) if isinstance(v, dict) else v) for k, v in p.items()))


@lru_cache(maxsize=32)
def _build(frozen: tuple):
    from langchain_groq import ChatGroq
    from app.rag.metering import callback
    s = get_settings()
    p = {k: dict(v) if k == "model_kwargs" else v for k, v in frozen}
    return ChatGroq(**p, api_key=s.groq_api_key, timeout=s.llm_timeout_s, max_retries=2, callbacks=[callback])


@lru_cache(maxsize=16)
def _build_openai(frozen: tuple):
    """A model on OpenAI. Reasoning models take no temperature or top_p; output is capped generously because the
    reasoning tokens count toward it. stream_usage: streamed answers report their token usage too."""
    from langchain_openai import ChatOpenAI
    from app.rag.metering import openai_callback
    s = get_settings()
    return ChatOpenAI(**dict(frozen), api_key=s.openai_api_key, max_retries=3, stream_usage=True,
                      callbacks=[openai_callback])


def model_name(llm) -> Optional[str]:
    return getattr(llm, "model_name", None)


def rag_params() -> dict:
    """The exact generation parameters for grounded answers (also shown in traces and golden-test runs)."""
    s = get_settings()
    if provider() == "openai":
        return {"model": model_for("rag"), "max_tokens": max(s.rag_max_tokens, s.openai_max_output_tokens),
                "reasoning_effort": s.rag_reasoning_effort or s.openai_reasoning_effort}
    p = {"model": model_for("rag"), "temperature": s.rag_temperature, "max_tokens": s.rag_max_tokens,
         "model_kwargs": {"top_p": s.rag_top_p}}
    if s.rag_reasoning_effort:
        p["reasoning_effort"] = s.rag_reasoning_effort
    return p


def describe_params(p: dict) -> dict:
    """Generation parameters for traces and the configuration (temperature / top_p do not apply on OpenAI)."""
    return {"provider": provider(), "model": p["model"], "temperature": p.get("temperature", "n/a"),
            "max_tokens": p["max_tokens"], "top_p": (p.get("model_kwargs") or {}).get("top_p", "n/a"),
            "reasoning_effort": p.get("reasoning_effort") or "model default"}


def structured(llm, schema, prompt: str):
    """JSON-mode structured output with one retry when the model returns invalid JSON."""
    return llm.with_structured_output(schema, method="json_mode").with_retry(stop_after_attempt=2).invoke(prompt)


def is_rate_limit(e: BaseException) -> bool:
    return type(e).__name__ == "RateLimitError" or "rate limit" in str(e).lower() or " 429" in str(e)


def rate_limit_wait_s(e: BaseException) -> Optional[float]:
    """The wait the provider suggests ("Please try again in 1m2.5s"), in seconds; None when it gives none."""
    import re
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", str(e))
    if not m or not any(m.groups()):
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)


def is_out_of_credit(e: BaseException) -> bool:
    """The provider refuses because the account has no credit (OpenAI: 429 insufficient_quota): waiting won't help."""
    text = str(e)
    return "insufficient_quota" in text or "credit_balance_exhausted" in text or "no credits remaining" in text


def is_daily_limit(e: BaseException) -> bool:
    text = str(e)
    return "per day" in text or "TPD" in text or "RPD" in text


def rate_limit_message(e: BaseException) -> str:
    """A partner-readable message for the AI provider's rate limit, with its suggested wait when it gives one."""
    import re
    if is_out_of_credit(e):
        return ("The AI service is unavailable because its account has run out of credit, so this answer could not "
                "be written. Please let an admin know.")
    text = str(e)
    wait = re.search(r"try again in ([0-9hms.]+)", text)
    daily = "per day" in text or "TPD" in text
    when = ""
    if wait:
        m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", wait.group(1))
        mins = (int(m.group(1) or 0) * 60 + int(m.group(2) or 0) + (1 if float(m.group(3) or 0) else 0)) if m else 0
        when = f" Try again in about {mins} minute{'s' if mins != 1 else ''}." if mins else " Try again in a moment."
    return ("The AI service's " + ("daily" if daily else "per-minute") + " usage limit has been reached, so this answer "
            "could not be written." + (when or " Try again shortly.") + " If this keeps happening, ask an admin to "
            "raise the AI provider's plan limit.")
