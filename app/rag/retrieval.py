"""Retrieval pipeline for grounded answers.

    question ─> understand (entities, follow-up resolution, query rewrite)
             ─> hybrid search per query: vector (embeddings) + BM25 keyword + exact terms (codes, pincodes)
             ─> merge candidates (reciprocal-rank fusion)
             ─> metadata filtering (ACTIVE version only, bank / product / loan type)
             ─> rerank the top candidates (cross-encoder, or lexical fallback), adjusted by source authority
                (document priority, and the document type the question asks for: payout -> payout sheets)
             ─> select the top passages above the relevance threshold
             ─> validate the evidence (relevance, bank coverage, the asked-for fact with a value)
                 insufficient -> expand the search once (more candidates, broader queries, no bank filter)
                 still insufficient -> the caller answers "not found" without asking the LLM

Every step's inputs, scores and decisions are kept on the Retrieval object, so an admin can see exactly why
a passage was or was not used (see app/services/rag_debug.py).
"""
import contextvars
import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from langchain_core.documents import Document
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.rag import entities, rerank
from app.rag.entities import METRICS, VALUE_PATTERNS

log = logging.getLogger(__name__)
RETRIEVAL_VERSION = "hybrid-rerank-v3"      # v3: one longer retry when the vector search failed
RRF_K = 60
WEIGHTS = {"vector": 1.0, "lexical": 1.0, "exact": 2.0}
PER_DOCUMENT_CAP = 4                  # passages from one document in the final context
BANK_BONUS, PRODUCT_BONUS, PRODUCT_PENALTY = 0.05, 0.03, 0.08
PRIORITY_WEIGHT = 0.06               # priority 100 -> +0.06, 50 -> 0, 0 -> -0.06 (orders evidence; never filters it)
TYPE_BONUS = 0.05                    # the document type the question asks for
OLDER_EDITION_PENALTY = 0.04         # a newer edition of the same document (e.g. Aug vs May RM list) also matched
TYPE_FOR_METRIC = {"payout": {"Payout"}, "code": {"Codes"}, "contact": {"Contacts"}, "pincode": {"Pincode list"},
                   "roi": {"Rate sheet", "Policy"}, "eligibility": {"Policy", "Eligibility"}}
MAX_HISTORY_TURNS = 4
RECOVERY_TIMEOUT_SCALE = 3           # the recovery search waits this many times VECTOR_TIMEOUT_S
SUPPORTING_CANDIDATES = 10           # candidates reranked when passages only support a directory / lookup answer


# ------------------------------------------------------------------ query understanding
@dataclass
class Plan:
    question: str
    standalone: str
    queries: List[str]
    banks: List[str] = field(default_factory=list)
    products: List[str] = field(default_factory=list)
    metrics: List[str] = field(default_factory=list)
    followup: bool = False
    rewrite: str = "none"                       # none | rules | llm | llm rejected: <why> | llm failed
    loan_type: Optional[str] = None
    carried: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


class QueryRewrite(BaseModel):
    standalone_question: str = Field(description="The question rewritten to stand on its own")
    search_query: str = Field(description="Keywords to search the knowledge base with")


def _user_turns(history: Optional[list]) -> List[str]:
    return [str(m.get("text", ""))[:500] for m in (history or []) if m.get("role", "user") == "user"][-MAX_HISTORY_TURNS:]


def _known():
    try:
        from app.services import kb_versions
        return kb_versions.known_names()
    except Exception:
        return {"banks": [], "products": []}


def _metric_phrase(metrics: List[str]) -> str:
    return " ".join(METRICS[m]["search"] for m in metrics)


def _search_query(text: str, banks, products, metrics, loan_type) -> str:
    """Keyword-rich query: canonical bank / product names, the fact asked for, acronym expansions, the words."""
    bits = list(banks) + list(products)
    if loan_type and loan_type not in products and loan_type != "Other":
        bits.append(loan_type)
    bits.append(_metric_phrase(metrics))
    bits += entities.expand_acronyms(text)
    bits.append(text)
    seen, out = set(), []
    for w in " ".join(bits).split():
        k = w.lower().strip("?,.!")
        if k and k not in seen:
            seen.add(k)
            out.append(w.strip("?,.!"))
    return " ".join(out)[:400]


def _needs_llm_rewrite(question: str, banks, products, metrics) -> bool:
    """Only conversational questions are rewritten; a precise one (bank or product, and the fact asked for, or
    an exact code) is searched as it is."""
    words = re.findall(r"\w+", question)
    if entities.salient_terms(question) or len(words) < 5:
        return False
    return not ((banks or products) and metrics)


def _rewrite_ok(question: str, history_text: str, new: str) -> Optional[str]:
    """None if the rewrite keeps the meaning, else why it was rejected."""
    for t in entities.salient_terms(question):
        if t.lower() not in new.lower():
            return f"dropped '{t}'"
    allowed = set(entities.find_banks(question + " " + history_text))
    added = [b for b in entities.find_banks(new) if b not in allowed]
    if added:
        return f"added {', '.join(added)}"
    if len(new) > 3 * len(question) + 200:
        return "too long"
    return None


def understand(question: str, history: Optional[list] = None, loan_type: Optional[str] = None,
               use_llm: bool = True) -> Plan:
    known = _known()
    banks = entities.find_banks(question, known["banks"])
    products = entities.find_products(question, known["products"])
    metrics = entities.find_metrics(question)
    followup = entities.is_followup(question, history)
    standalone, how, carried = question.strip(), "none", {}
    prior = _user_turns(history)
    if followup and prior:
        prior_text = " ".join(prior)
        if not banks and (b := entities.find_banks(prior_text, known["banks"])):
            carried["banks"] = b[-1:]
        if not products and (p := entities.find_products(prior_text, known["products"])):
            carried["products"] = p[-1:]
        if not metrics and (m := entities.find_metrics(prior[-1])):
            carried["metrics"] = m
        if carried:
            banks = banks + carried.get("banks", [])
            products = products + carried.get("products", [])
            metrics = metrics + carried.get("metrics", [])
            about = "; ".join(f"{k[:-1]}: {', '.join(METRICS[x]['search'] if k == 'metrics' else x for x in v)}"
                              for k, v in carried.items())
            standalone = f"{question.strip()} (follow-up to: {prior[-1]}; {about})"
            how = "rules"

    queries = []
    s = get_settings()
    from app.rag.llm import get_llm, structured
    llm = get_llm("fast") if use_llm and s.rag_query_rewrite else None
    if llm is not None and ((followup and prior) or _needs_llm_rewrite(question, banks, products, metrics)):
        conv = "\n".join(f"{m.get('role', 'user').upper()}: {str(m.get('text', ''))[:400]}"
                         for m in (history or [])[-MAX_HISTORY_TURNS:])
        try:
            from app.rag.metering import purpose
            with purpose("query_rewrite"):
                r = structured(llm, QueryRewrite, f"""You prepare a user's question for searching a loan knowledge base
(lender policies, codes, payout sheets, RM contacts, pincode lists).

Rules:
- Do not change the meaning of the question. Do not add facts, numbers, banks or products that are not in the
  question or the conversation.
- If the question is a follow-up ("what about HDFC?"), resolve it with the conversation into a standalone question.
- If the question is already precise and standalone, return it unchanged.
- Keep names, numbers and codes exactly as written.
- search_query: the important keywords (bank, product, the fact asked for), no filler words.

CONVERSATION:
{conv or '(none)'}

QUESTION: {question}

Return JSON only: {{"standalone_question": "...", "search_query": "..."}}""")
            why = _rewrite_ok(question, " ".join(prior), r.standalone_question + " " + r.search_query)
            if why:
                how = f"llm rejected: {why}"
            else:
                standalone, how = r.standalone_question.strip() or standalone, "llm"
                queries.append(r.search_query.strip())
                banks = list(dict.fromkeys(banks + entities.find_banks(standalone, known["banks"])))
                products = list(dict.fromkeys(products + entities.find_products(standalone, known["products"])))
                metrics = list(dict.fromkeys(metrics + entities.find_metrics(standalone)))
        except Exception as e:
            log.warning("Query rewrite failed: %s", e)
            how = how if how != "none" else "llm failed"
    banks, products, metrics = (list(dict.fromkeys(x)) for x in (banks, products, metrics))
    rules_query = _search_query(standalone if how == "llm" else question + " " + " ".join(carried.get("banks", []) + carried.get("products", [])),
                                banks, products, metrics, loan_type)
    queries = [q for q in dict.fromkeys([question.strip() if how == "none" else standalone, *queries, rules_query]) if q]
    return Plan(question=question.strip(), standalone=standalone, queries=queries[:3], banks=banks, products=products,
                metrics=metrics, followup=followup, rewrite=how, loan_type=loan_type, carried=carried)


# ------------------------------------------------------------------ candidates
@dataclass
class Candidate:
    doc: Document
    key: str
    vector_score: Optional[float] = None
    vector_rank: Optional[int] = None
    keyword_score: Optional[float] = None
    exact: bool = False
    fused: float = 0.0
    found_by: List[str] = field(default_factory=list)
    rerank: Optional[float] = None
    priority: Optional[int] = None
    score: float = 0.0
    bank_match: bool = False
    product_match: Optional[bool] = None
    dropped: str = ""
    selected: bool = False
    newer_edition: str = ""                     # label of a newer edition of this document in the pool

    def view(self, text_chars: int = 700) -> dict:
        m = self.doc.metadata
        return {"id": self.key, "source": m.get("source"), "title": m.get("title") or "", "page": m.get("page"),
                "section": m.get("section") or "", "bank": m.get("bank") or m.get("lender") or "",
                "product": m.get("product") or m.get("loan_type") or "", "version": m.get("version"),
                "status": m.get("status") or "", "corpus": m.get("corpus") or "policy_upload",
                "vector_score": _r(self.vector_score), "vector_rank": self.vector_rank,
                "keyword_score": _r(self.keyword_score), "exact": self.exact, "found_by": self.found_by,
                "fused": _r(self.fused, 5), "rerank": _r(self.rerank), "priority": self.priority, "score": _r(self.score),
                "bank_match": self.bank_match, "product_match": self.product_match,
                "dropped": self.dropped, "selected": self.selected, "newer_edition": self.newer_edition,
                "text": self.doc.page_content[:text_chars]}


def _r(x, n=4):
    return None if x is None else round(float(x), n)


def _key(d: Document) -> str:
    return d.id or "h-" + hashlib.sha1(d.page_content.encode()).hexdigest()[:16]


def vector_hits(query: str, k: int, loan_type: Optional[str], banks: Optional[List[str]] = None) -> List[tuple]:
    """(Document, similarity) from the vector store: Urban Money chunks, plus policy uploads for the loan type.
    With banks: only documents tagged with those banks, filtered in the search itself (without it a bank's
    passages can fall outside the top candidates, and the bank filter afterwards finds nothing)."""
    from app.rag import vectorstore as v
    from app.services.knowledge_base import CORPUS
    vs = v.get_vectorstore()
    if banks:
        if v.is_pinecone():
            return v._scored(vs, query, k, {"corpus": CORPUS, "bank": {"$in": list(banks)}})
        return v._scored(vs, query, k, lambda d: d.metadata.get("corpus") == CORPUS and d.metadata.get("bank") in banks)
    types = [loan_type, "Unknown"] if loan_type else None
    if v.is_pinecone():                  # two network round-trips: run them side by side
        with ThreadPoolExecutor(2) as pool:
            primary = pool.submit(contextvars.copy_context().run, v._scored, vs, query, k, {"corpus": CORPUS})
            others = pool.submit(contextvars.copy_context().run, v._scored, vs, query, max(3, k // 3),
                                 {"loan_type": {"$in": types}} if types else None)
            primary, others = primary.result(), others.result()
    else:
        primary = v._scored(vs, query, k, lambda d: d.metadata.get("corpus") == CORPUS)
        others = v._scored(vs, query, max(3, k // 3),
                           lambda d: d.metadata.get("corpus") != CORPUS and (not types or d.metadata.get("loan_type") in types))
    return sorted(primary + others, key=lambda x: -x[1])


def bank_keyword_hits(query: str, k: int, banks: List[str]) -> List[Document]:
    """BM25 hits that must name one of the banks (any alias) as well as the question's words."""
    from app.services.knowledge_base import lexical_search
    aliases = [a for b in banks for a in entities.bank_aliases(b)]
    return lexical_search(query, k, require_any=aliases)


def keyword_hits(query: str, k: int, exact: bool) -> List[Document]:
    from app.services.knowledge_base import keyword_search, lexical_search
    return keyword_search(query, k) if exact else lexical_search(query, k)


def gather(plan: Plan, k: int) -> Dict[str, Candidate]:
    """Run every query through vector, BM25 and exact-term search and fuse the ranked lists (RRF)."""
    from app.rag import vectorstore as vstore
    cands: Dict[str, Candidate] = {}

    def add(kind: str, rank: int, d: Document, score: Optional[float]):
        key = _key(d)
        c = cands.get(key)
        if c is None:
            c = cands[key] = Candidate(doc=d, key=key)
        c.fused += WEIGHTS[kind] / (RRF_K + rank)
        if kind not in c.found_by:
            c.found_by.append(kind)
        if kind == "vector":
            c.vector_score = max(c.vector_score or 0.0, float(score))
            c.vector_rank = rank + 1 if c.vector_rank is None else min(c.vector_rank, rank + 1)
        elif kind == "lexical":
            c.keyword_score = max(c.keyword_score or 0.0, float(d.metadata.get("keyword_score") or 0.0))
        else:
            c.exact = True
            c.keyword_score = max(c.keyword_score or 0.0, float(d.metadata.get("keyword_score") or 0.0))

    def vector(q):
        try:
            return vector_hits(q, k, plan.loan_type)
        except Exception:
            log.exception("Vector search failed")
            vstore.note_issue("error")
            return []

    def scoped(q):
        try:
            return vector_hits(q, max(4, k // 2), plan.loan_type, banks=plan.banks)
        except Exception:
            log.debug("Bank-scoped vector search failed", exc_info=True)
            vstore.note_issue("error")
            return []

    queries = list(plan.queries)
    with ThreadPoolExecutor(max(1, min(4, len(queries) + 1))) as pool:   # each query is a vector-store round-trip
        vectors = [f.result() for f in [pool.submit(contextvars.copy_context().run, vector, q) for q in queries]]
        banked = pool.submit(contextvars.copy_context().run, scoped, queries[0]) if plan.banks and queries else None
        banked = banked.result() if banked else []
    for q, hits in zip(queries, vectors):
        for rank, (d, s) in enumerate(hits):
            add("vector", rank, d, s)
        for rank, d in enumerate(keyword_hits(q, k, exact=False)):
            add("lexical", rank, d, None)
    if plan.banks and queries:
        for rank, (d, s) in enumerate(banked):
            add("vector", rank, d, s)
        for rank, d in enumerate(bank_keyword_hits(queries[-1], max(4, k // 2), plan.banks)):
            add("lexical", rank, d, None)
    for q in dict.fromkeys([plan.question, plan.standalone]):
        for rank, d in enumerate(keyword_hits(q, k, exact=True)):
            add("exact", rank, d, None)
    for c in cands.values():                     # FTS copies of a chunk carry scores the vector copy lacks
        c.doc.metadata.pop("keyword_score", None)
        c.doc.metadata.pop("match", None)
    return cands


# ------------------------------------------------------------------ metadata filtering
def _active():
    try:
        from app.services import kb_versions
        return kb_versions.active_versions()
    except Exception:
        log.exception("Could not read active document versions")
        return None


def apply_filters(plan: Plan, cands: Dict[str, Candidate], bank_filter: bool = True) -> dict:
    """Drop inactive versions, other loan types and other banks' documents; mark bank / product matches.
    Returns a summary of what the filters did (for the trace)."""
    from app.services.knowledge_base import CORPUS
    active = _active()
    summary = {"status": "ACTIVE versions only", "banks": plan.banks, "products": plan.products,
               "bank_filter": bank_filter and bool(plan.banks), "loan_type": plan.loan_type, "dropped": {}}
    for c in cands.values():
        m, text = c.doc.metadata, c.doc.page_content
        if m.get("corpus") == CORPUS:
            if m.get("status") and m["status"] != "ACTIVE":
                c.dropped = f"{m['status'].lower()} version"
            elif active is not None and m.get("sha") and active.get(m.get("source")) not in (None, m["sha"]):
                c.dropped = "not the active version"
            elif active is not None and m.get("sha") and m.get("source") not in active:
                c.dropped = "document disabled"
        elif plan.loan_type and m.get("loan_type") not in (None, "Unknown", plan.loan_type):
            c.dropped = f"other loan type ({m.get('loan_type')})"
        if c.dropped:
            continue
        doc_bank = m.get("bank") or ""
        if plan.banks:
            c.bank_match = doc_bank in plan.banks or entities.mentions(text, plan.banks, entities.table("bank"))
            if bank_filter and doc_bank and not c.bank_match:
                c.dropped = f"different bank ({doc_bank})"
                continue
        if plan.products:
            doc_product = m.get("product") or ""
            c.product_match = doc_product in plan.products or entities.mentions(text, plan.products, entities.table("product"))
            if doc_product and not c.product_match:
                c.score -= PRODUCT_PENALTY        # product tags are fuzzy (one poster covers HL and LAP): demote only
    for c in cands.values():
        if c.dropped:
            reason = re.sub(r" \(.*", "", c.dropped)
            summary["dropped"][reason] = summary["dropped"].get(reason, 0) + 1
    return summary


# ------------------------------------------------------------------ reranking + selection
def rerank_and_select(plan: Plan, cands: Dict[str, Candidate], pool_size: Optional[int] = None) -> dict:
    s = get_settings()
    pool = sorted((c for c in cands.values() if not c.dropped), key=lambda c: -c.fused)[:pool_size or s.rag_candidates]
    weights = {}
    for b in plan.banks:
        for a in entities.bank_aliases(b):
            for w in a.split():
                weights[w] = 3.0
    for w in _metric_phrase(plan.metrics).lower().split():
        weights.setdefault(w, 2.0)
    scores, used = rerank.score(plan.standalone, [c.doc.page_content for c in pool], weights)
    top_fused = max((c.fused for c in pool), default=1.0) or 1.0
    authority = _authority()
    wanted = set().union(*(TYPE_FOR_METRIC.get(m, set()) for m in plan.metrics)) if plan.metrics else set()
    for c, sc in zip(pool, scores):
        c.rerank = sc
        base = sc if sc is not None else c.fused / top_fused
        c.score += base + (BANK_BONUS if c.bank_match else 0.0) + (PRODUCT_BONUS if c.product_match else 0.0)
        a = authority.get(c.doc.metadata.get("source") or "")
        if a:
            c.priority = a["priority"]
            c.score += PRIORITY_WEIGHT * (a["priority"] - 50) / 50 + (TYPE_BONUS if a.get("doc_type", a["category"]) in wanted else 0.0)
    _mark_older_editions(pool)
    pool.sort(key=lambda c: -c.score)
    threshold = s.rag_min_relevance if used != "none" else 0.0
    picked, per_doc, seen, duplicates = [], {}, set(), 0
    for c in pool:
        if len(picked) >= s.rag_top_k:
            break
        if (c.rerank if c.rerank is not None else c.score) < threshold:
            continue
        src = c.doc.metadata.get("source")
        if per_doc.get(src, 0) >= PER_DOCUMENT_CAP:
            continue
        body = _body_key(c.doc.page_content)
        if body in seen:
            duplicates += 1                       # the same text in another file or copy: no new evidence
            continue
        seen.add(body)
        per_doc[src] = per_doc.get(src, 0) + 1
        c.selected = True
        picked.append(c)
    return {"reranker": used, "pool": len(pool), "threshold": threshold, "duplicates_skipped": duplicates,
            "selected": [c.key for c in picked]}


def _mark_older_editions(pool: List[Candidate]):
    """Demote, and label for the prompt, passages from an older edition of a document whose newer edition is also
    a candidate (never dropped: monthly lists and sheets are updates, not full replacements)."""
    try:
        from app.services import kb_versions
        older = kb_versions.older_editions(c.doc.metadata.get("source") or "" for c in pool)
    except Exception:
        log.exception("Could not compare document editions")
        return
    for c in pool:
        label = older.get(c.doc.metadata.get("source") or "")
        if label:
            c.newer_edition = label
            c.score -= OLDER_EDITION_PENALTY
            c.doc = Document(id=c.doc.id, page_content=c.doc.page_content,
                             metadata={**c.doc.metadata, "newer_edition": label})


def _authority() -> dict:
    try:
        from app.services import kb_versions
        return kb_versions.authority_map()
    except Exception:
        log.exception("Could not read document priorities")
        return {}


def _body_key(text: str) -> str:
    """Passage text without its file header, case and spacing: equal keys mean duplicate passages."""
    from app.services.ingestion import strip_header
    return " ".join(re.findall(r"\w+", strip_header(text).lower()))


# ------------------------------------------------------------------ evidence validation
def _has_value(text: str, metric: str) -> bool:
    d = METRICS[metric]
    t = text.lower()
    if not re.search(d["evidence"], t):
        return False
    return d["value"] is None or bool(re.search(VALUE_PATTERNS[d["value"]], text, re.I))


def validate(plan: Plan, selected: List[Candidate]) -> dict:
    """Is there reliable evidence for this question in the selected passages?"""
    checks = []
    top = max(((c.rerank if c.rerank is not None else c.score) for c in selected), default=None)
    checks.append({"check": "relevant passages", "ok": bool(selected),
                   "detail": f"{len(selected)} passage(s) above the relevance threshold" + (f", best {top:.2f}" if top is not None else "")})
    if plan.banks:
        hit = [c for c in selected if c.bank_match]
        checks.append({"check": "bank covered", "ok": bool(hit),
                       "detail": f"{len(hit)} passage(s) about {', '.join(plan.banks)}" if hit else f"no passage mentions {', '.join(plan.banks)}"})
    if plan.products:
        hit = [c for c in selected if c.product_match]
        checks.append({"check": "product covered", "ok": True, "info": True,
                       "detail": f"{len(hit)} passage(s) mention {', '.join(plan.products)}"})
    # Only facts that have a value (amount, %, tenure, score, code, pincode) are required. Generic asks
    # (eligibility, documents, contacts) are worded too many ways; relevance and bank coverage decide those.
    general = [m for m in plan.metrics if METRICS[m]["value"] is None]
    if general:
        hit = sorted({m for m in general for c in selected if _has_value(c.doc.page_content, m)})
        checks.append({"check": "topic wording", "ok": True, "info": True,
                       "detail": f"passages mention: {', '.join(METRICS[m]['search'] for m in hit)}" if hit
                       else "no passage uses the usual wording for " + ", ".join(METRICS[m]["search"] for m in general)})
    asked = [m for m in plan.metrics if METRICS[m]["value"] is not None]
    if asked:
        found = sorted({m for m in asked for c in selected if _has_value(c.doc.page_content, m)})
        names = ", ".join(METRICS[m]["search"] for m in asked)
        checks.append({"check": "asked-for fact present", "ok": bool(found),
                       "detail": f"found: {', '.join(METRICS[m]['search'] for m in found)}" if found
                       else f"no passage states {names} with a value"})
    required = [c for c in checks if not c.get("info")]
    ok = all(c["ok"] for c in required)
    return {"sufficient": ok, "checks": checks,
            "reason": "" if ok else "; ".join(c["detail"] for c in required if not c["ok"])}


# ------------------------------------------------------------------ the whole pipeline
@dataclass
class Retrieval:
    plan: Plan
    candidates: List[Candidate]
    selected: List[Candidate]
    validation: dict
    passes: List[dict]
    timings: Dict[str, float]
    degraded: List[str] = field(default_factory=list)     # vector searches that timed out / failed (final pass)

    @property
    def docs(self) -> List[Document]:
        return [c.doc for c in self.selected]

    def trace(self) -> dict:
        return {"plan": self.plan.to_dict(), "passes": self.passes, "validation": self.validation,
                "candidates": [c.view() for c in sorted(self.candidates, key=lambda c: (not c.selected, bool(c.dropped), -c.score, -c.fused))][:60],
                "selected": [c.key for c in self.selected], "timings": self.timings, "degraded": self.degraded,
                "version": RETRIEVAL_VERSION}


def _run_pass(plan: Plan, k: int, bank_filter: bool):
    t = time.perf_counter()
    cands = gather(plan, k)
    t_search = time.perf_counter() - t
    filt = apply_filters(plan, cands, bank_filter)
    t = time.perf_counter()
    sel = rerank_and_select(plan, cands, k)
    t_rerank = time.perf_counter() - t
    selected = sorted((c for c in cands.values() if c.selected), key=lambda c: -c.score)
    return cands, selected, validate(plan, selected), {"queries": plan.queries, "k": k, "filters": filt, **sel,
                                                       "search_s": round(t_search, 3), "rerank_s": round(t_rerank, 3)}


def retrieve(question: str, history: Optional[list] = None, loan_type: Optional[str] = None,
             use_llm: bool = True, plan: Optional[Plan] = None, supporting: bool = False) -> Retrieval:
    """supporting: the answer comes from elsewhere (the banker directory, a table lookup) and passages are only
    supporting evidence: a smaller candidate pool and no second, wider pass (it cost up to 40 s on the live
    index for nothing)."""
    from app.rag import vectorstore as vstore
    s = get_settings()
    t0 = time.perf_counter()
    plan = plan or understand(question, history, loan_type, use_llm)
    t_plan = time.perf_counter() - t0
    k = min(s.rag_candidates, SUPPORTING_CANDIDATES) if supporting else s.rag_candidates
    token = vstore.issues.set([])
    try:
        cands, selected, val, info = _run_pass(plan, k, bank_filter=True)
        passes = [{"pass": 1, **info, "sufficient": val["sufficient"], **({"supporting": True} if supporting else {}),
                   **({"vector_issues": list(vstore.issues.get())} if vstore.issues.get() else {})}]
        if not val["sufficient"] and not supporting and vstore.issues.get():
            # The vector search timed out or failed: before calling anything "not found", search again once with
            # a longer time limit (an outage must not look like missing information).
            vstore.issues.set([])
            scale = vstore.timeout_scale.set(RECOVERY_TIMEOUT_SCALE)
            try:
                cands, selected, val, info = _run_pass(plan, k, bank_filter=True)
            finally:
                vstore.timeout_scale.reset(scale)
            passes.append({"pass": "recovery", **info, "sufficient": val["sufficient"], "why": "vector search failed",
                           **({"vector_issues": list(vstore.issues.get())} if vstore.issues.get() else {})})
        cands, selected, val, passes = _widen(plan, s, supporting, cands, selected, val, passes)
        degraded = list(vstore.issues.get())
    finally:
        vstore.issues.reset(token)
    timings = {"understand_s": round(t_plan, 3), "total_s": round(time.perf_counter() - t0, 3)}
    return Retrieval(plan=plan, candidates=list(cands.values()), selected=selected, validation=val,
                     passes=passes, timings=timings, degraded=degraded)


def _widen(plan: Plan, s, supporting: bool, cands, selected, val, passes):
    """The second, wider pass for a question without enough evidence."""
    if not val["sufficient"] and not supporting:
        # Expand once: more candidates, a broader entity + fact query, the original wording, no bank filter.
        broad = " ".join(plan.banks + plan.products + [_metric_phrase(plan.metrics)]).strip()
        wider = Plan(**{**plan.to_dict(), "queries": list(dict.fromkeys(plan.queries + [q for q in (broad, plan.question) if q]))[:4]})
        c2, sel2, val2, info2 = _run_pass(wider, s.rag_candidates * 2, bank_filter=False)
        passes.append({"pass": 2, **info2, "sufficient": val2["sufficient"], "why": val["reason"]})
        cands, selected, val = c2, sel2, val2
    return cands, selected, val, passes
