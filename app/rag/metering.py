"""Token metering for every LLM call.

A LangChain callback (UsageCallback) is attached to every chat model built in app/rag/llm.py. When a call ends,
it records the token usage the provider reported (Groq returns it with the response and, when streaming, in
the last chunk) in the usage ledger, under the request's reservation (the Meter in context). Calls made outside
a request (golden tests, the admin test bench) are recorded as system usage.

If the provider returned no usage, e.g. the user stopped a stream before the last chunk, the call is recorded
as estimated from the prompt and the text received, so stopped answers still count.

Context: the Meter lives in a context variable. A streaming response is consumed one step at a time from
different worker threads, so streamed generators are wrapped with bind(), which runs every step in one
persistent context.
"""
import contextvars
import logging
import threading
from contextlib import contextmanager
from typing import Dict, Iterator, Optional

from langchain_core.callbacks import BaseCallbackHandler

from app.services import usage

log = logging.getLogger(__name__)

_meter: contextvars.ContextVar = contextvars.ContextVar("usage_meter", default=None)
_call: contextvars.ContextVar = contextvars.ContextVar("usage_call", default=("", None))


class Meter:
    """One metered request: its quota reservation and the LLM calls recorded under it."""

    def __init__(self, reservation: Optional[usage.Reservation]):
        self.reservation = reservation
        self.calls = []

    @property
    def plan(self) -> dict:
        return (self.reservation.plan if self.reservation else None) or {}


def current() -> Optional[Meter]:
    return _meter.get()


@contextmanager
def metered(meter: Meter):
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


@contextmanager
def purpose(name: str, breakdown: Optional[Dict[str, int]] = None):
    """Label the LLM calls made inside this block (and give the prompt's component sizes, for the RAG split)."""
    token = _call.set((name, breakdown))
    try:
        yield
    finally:
        _call.reset(token)


def bind(meter: Meter, iterator) -> Iterator:
    """Run a generator's steps in one context holding the meter (see module docstring)."""
    ctx = contextvars.copy_context()
    ctx.run(_meter.set, meter)
    try:
        while True:
            try:
                item = ctx.run(next, iterator)
            except StopIteration:
                return
            yield item
    finally:
        close = getattr(iterator, "close", None)
        if close:
            ctx.run(close)


def _text(m) -> str:
    c = getattr(m, "content", "")
    return c if isinstance(c, str) else str(c)


def _usage_from(response) -> tuple:
    """(input, output, total, model) from an LLMResult, or Nones when the provider reported nothing."""
    model = None
    try:
        gen = response.generations[0][0]
        msg = getattr(gen, "message", None)
        meta = getattr(msg, "response_metadata", None) or {}
        model = meta.get("model_name") or meta.get("model")
        um = getattr(msg, "usage_metadata", None)
        if um and (um.get("input_tokens") or um.get("output_tokens")):
            return um.get("input_tokens", 0), um.get("output_tokens", 0), um.get("total_tokens"), model
    except (IndexError, AttributeError, TypeError):
        pass
    tu = ((getattr(response, "llm_output", None) or {}).get("token_usage")) or {}
    if tu.get("prompt_tokens") or tu.get("completion_tokens"):
        return tu.get("prompt_tokens", 0), tu.get("completion_tokens", 0), tu.get("total_tokens"), model
    return None, None, None, model


def _generated_text(response) -> str:
    try:
        gen = response.generations[0][0]
        return getattr(gen, "text", "") or _text(getattr(gen, "message", None))
    except (IndexError, AttributeError, TypeError):
        return ""


class UsageCallback(BaseCallbackHandler):
    raise_error = False

    def __init__(self, provider: str = "groq"):
        self.provider = provider
        self._runs: dict = {}
        self._lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, invocation_params=None, **kwargs):
        prompt_chars = sum(len(_text(m)) for batch in messages for m in batch)
        params = invocation_params or kwargs.get("invocation_params") or {}
        name, breakdown = _call.get()
        name = name or (kwargs.get("metadata") or {}).get("langgraph_node") or ""
        with self._lock:
            self._runs[run_id] = {"meter": _meter.get(), "purpose": name, "breakdown": breakdown,
                                  "prompt_chars": prompt_chars, "out": [],
                                  "model": params.get("model") or params.get("model_name") or "unknown"}

    def on_llm_new_token(self, token, *, run_id, **kwargs):
        run = self._runs.get(run_id)
        if run is not None and token:
            run["out"].append(token if isinstance(token, str) else str(token))

    def on_llm_end(self, response, *, run_id, **kwargs):
        self._finish(run_id, response, "completed")

    def on_llm_error(self, error, *, run_id, response=None, **kwargs):
        self._finish(run_id, response, "stopped" if isinstance(error, GeneratorExit) else "failed")

    def _finish(self, run_id, response, status: str):
        with self._lock:
            run = self._runs.pop(run_id, None)
        if run is None:
            return
        i, o, total, model = _usage_from(response) if response is not None else (None, None, None, None)
        source = "provider"
        if i is None:
            out_text = "".join(run["out"]) or (_generated_text(response) if response is not None else "")
            if status != "completed" and not out_text:
                return                       # failed before the model produced anything: nothing to bill
            i, o, total = usage.estimate_tokens(run["prompt_chars"]), usage.estimate_tokens(len(out_text)), None
            source = "estimated"
        meter = run["meter"]
        try:
            rec = usage.record(meter.reservation if meter else None, provider=self.provider,
                               model=model or run["model"], input_tokens=i, output_tokens=o, total_tokens=total,
                               source=source, purpose=run["purpose"], status=status, breakdown=run["breakdown"])
            if meter is not None:
                meter.calls.append(rec)
        except Exception:
            log.exception("Could not record LLM usage")


callback = UsageCallback()
openai_callback = UsageCallback("openai")
