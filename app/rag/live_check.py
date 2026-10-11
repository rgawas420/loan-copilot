"""Live claim check and answer confidence (LIVE_CHECK).

After a knowledge-base answer is written, the fast model checks each of its claims against the passages the answer
was given (the golden-test judge's check, without a reference answer). An answer with an unsupported claim gets one
rewrite without it, which is checked again. The partner then sees a confidence level, without source names:

    high    every claim is supported, no figure / contact mismatch, no source warning
    medium  a claim is only partly supported, the claims could not be checked, the evidence was weak, or a cited
            source is an OCR scan nobody verified, expired, or an older edition
    low     a claim or figure could still not be matched to the documents: confirm with the lender before use

Replies without model claims (not found, the app's own questions) get no confidence level.
"""
import logging
from typing import List, Optional

from app.rag import grounding, judge

log = logging.getLogger(__name__)
LABEL = "answer_claim_check"


def enabled() -> bool:
    from app.core.config import get_settings
    return get_settings().live_check


def check(prep, answer: str, llm) -> Optional[dict]:
    """The claim check of one answer, or None when there is nothing to check (or no model)."""
    if prep.not_found or prep.direct or not (answer or "").strip() or grounding.is_not_found(answer):
        return None
    trace = {"prompt": {"full": grounding.prompt_text(prep.messages)}}
    try:
        return judge.judge(prep.question, answer, trace, llm=llm, label=LABEL)
    except Exception as e:                        # a rate limit here must not lose the answer already shown
        log.warning("Live claim check failed: %s", e)
        return {"verdict": "ERROR", "error": f"{type(e).__name__}: {e}"[:300]}


def unsupported(result: Optional[dict]) -> List[str]:
    return [p["claim"] for p in (result or {}).get("problems", []) if p.get("verdict") == "unsupported"]


def correction(prep, answer: str, claims: List[str]):
    """Messages asking the answer model to rewrite the answer without the unsupported claims."""
    return grounding.correction_messages(prep.messages, answer, [], [], [], claims)


def confidence(prep, post: dict, result: Optional[dict], corrected: bool = False) -> Optional[dict]:
    """The partner-facing confidence of a finished answer (None: no level, e.g. a 'not found' reply)."""
    if prep.not_found or prep.direct or post.get("not_found"):
        return None
    low, medium = [], []
    figures = list(post.get("unsupported_figures") or []) + list(post.get("contact_mismatches") or [])
    if figures:
        low.append("These figures or numbers could not be matched to the documents: " + ", ".join(figures[:6]))
    claims = unsupported(result)
    if claims:
        low.append("Could not confirm: " + "; ".join(claims[:4]))
    if result is None or result.get("verdict") == "ERROR":
        medium.append("The facts could not be checked automatically this time.")
    else:
        partial = [p["claim"] for p in result.get("problems", []) if p.get("verdict") == "partial"]
        if partial:
            medium.append("Only partly stated in the documents: " + "; ".join(partial[:3]))
    if not (prep.retrieval.validation or {}).get("sufficient", True) and not (prep.lookup or prep.bankers or prep.facts):
        medium.append("The documents only partly cover this question.")
    cited = [s for s in post.get("sources") or [] if not s.get("uncited") and not s.get("attachment") and not s.get("web")]
    warned = sorted({w.split(" 20")[0] for s in cited for w in s.get("warnings") or []})
    if any("OCR" in w for w in warned):
        medium.append("Part of it comes from a scanned document that has not been checked by an admin.")
    if any(w.startswith("expired") for w in warned):
        medium.append("Part of it comes from a document past its validity date.")
    if any(s.get("older_edition") for s in cited):
        medium.append("Part of it comes from an older edition of a document.")
    level = "low" if low else "medium" if medium else "high"
    reasons = low + medium or ["Every fact in this answer matches the documents."]
    out = {"level": level, "reasons": reasons, "corrected": corrected}
    if result and result.get("verdict") != "ERROR":
        out["claims"], out["supported"] = result.get("claims", 0), result.get("supported", 0)
    return out
