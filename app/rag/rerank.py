"""Reranking: score each (question, passage) pair for how well the passage answers the question.

Default: a local cross-encoder (sentence-transformers; cross-encoder/ms-marco-MiniLM-L-6-v2, ~90 MB, CPU).
Its logit is mapped to 0..1 with a sigmoid. If the model cannot be loaded (no network on first use), or
RERANKER=lexical, a deterministic lexical scorer is used instead: the share of the question's key terms
(bank, product, the fact asked for, other content words) that the passage contains. The reranker actually
used is reported with every result, so a fallback is never silent.
"""
import logging
import math
import re
import threading
from typing import List, Sequence, Tuple

from app.core.config import get_settings
from app.services.ingestion import strip_header

log = logging.getLogger(__name__)
MAX_PASSAGE_CHARS = 1500
_lock = threading.Lock()
_model = None
_model_error = ""

_STOP = {"the", "and", "for", "with", "what", "which", "who", "this", "that", "from", "are", "was", "how", "can",
         "does", "did", "has", "have", "need", "want", "you", "your", "there", "please", "give", "tell", "about",
         "details", "show", "any", "all", "get", "find", "know", "should", "would", "could", "will", "into", "under",
         "over", "than", "then", "also", "only", "is", "a", "an", "of", "to", "in", "on", "me", "my", "i", "it",
         "much", "many", "do", "be", "or", "at", "by"}


def _load():
    global _model, _model_error
    name = get_settings().reranker
    with _lock:
        if _model is None and not _model_error:
            try:
                from sentence_transformers import CrossEncoder
                try:                                    # cached copy first; the Hub check is slow on a poor link
                    _model = CrossEncoder(name, max_length=512, local_files_only=True)
                except Exception:
                    _model = CrossEncoder(name, max_length=512)
                log.info("Reranker loaded: %s", name)
            except Exception as e:                      # offline first start, missing model, ...
                _model_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("Cross-encoder %s unavailable (%s); using the lexical reranker", name, _model_error)
    return _model


def warmup():
    if mode() == "cross-encoder":
        _load()


def mode() -> str:
    r = (get_settings().reranker or "").strip().lower()
    if r in ("", "none", "off"):
        return "none"
    if r == "lexical":
        return "lexical"
    return "cross-encoder"


def describe() -> str:
    """What is actually reranking right now (for traces and the config view)."""
    m = mode()
    if m == "cross-encoder":
        if _model is not None:
            return get_settings().reranker
        if _model_error:
            return f"lexical (fallback: {get_settings().reranker} unavailable)"
        return f"{get_settings().reranker} (not loaded yet)"
    return m


def _terms(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and (len(w) > 2 or w.isdigit())]


def lexical_scores(query: str, passages: Sequence[str], weights: dict = None) -> List[float]:
    """Weighted share of query terms present in each passage (0..1). weights: term -> weight."""
    weights = weights or {}
    terms = list(dict.fromkeys(_terms(query)))
    if not terms:
        return [0.0] * len(passages)
    total = sum(weights.get(t, 1.0) for t in terms)
    out = []
    for p in passages:
        words = set(_terms(p))
        out.append(round(sum(weights.get(t, 1.0) for t in terms if t in words) / total, 4))
    return out


def score(query: str, passages: Sequence[str], weights: dict = None) -> Tuple[List[float], str]:
    """(scores 0..1 or None when reranking is off, reranker used)."""
    m = mode()
    if not passages:
        return [], describe()
    texts = [_trim(p) for p in passages]
    if m == "cross-encoder":
        model = _load()
        if model is not None:
            try:
                # Raw logits from every model (some, e.g. BAAI/bge-reranker, apply a sigmoid by default), so the
                # sigmoid below is applied exactly once and the relevance threshold means the same for all.
                from torch.nn import Identity
                logits = model.predict([(query, t) for t in texts], batch_size=32, show_progress_bar=False,
                                       activation_fn=Identity())
                return [round(1 / (1 + math.exp(-float(x))), 4) for x in logits], get_settings().reranker
            except Exception:
                log.exception("Cross-encoder scoring failed; using the lexical reranker")
        return lexical_scores(query, texts, weights), describe()
    if m == "lexical":
        return lexical_scores(query, texts, weights), "lexical"
    return [None] * len(passages), "none"


def _trim(passage: str) -> str:
    """The header lines (file name, document, section often name the bank / product) plus the body, capped."""
    lines = passage.splitlines()
    keep = [ln.replace("Source: ", "File: ", 1) for ln in lines[:5] if ln.startswith(("Source:", "Document:", "Section:", "Lender:"))]
    body = strip_header(passage)
    return ("\n".join(keep + [body]))[:MAX_PASSAGE_CHARS]
