"""Questions answered from the knowledge base, grounded in the retrieved passages.

mode 'kb':  standalone questions ("Bajaj LAP RM contact in Pune?", "Max HDFC business loan?").
mode 'ask': follow-up questions about a recommendation ("Why not Lender B?", "What if CIBIL is 710?"); these are
            also grounded on the structured policies for the case's loan type, the rules-engine result and the
            case (personal data removed).

Every answer goes through the same pipeline (app/rag/retrieval.py): query understanding and rewriting, hybrid
search, metadata filtering, reranking and an evidence check. For a knowledge-base question without reliable
evidence the LLM is not asked at all: the user is told the information was not found. Otherwise the model gets
the numbered passages under a strict grounding prompt (app/rag/grounding.py), its [S#] citations are mapped back
to the exact passages, and every figure in the answer is checked against the sources. An answer that fails the
check is rewritten once with the problems pointed out, and the rewrite is kept only if it has fewer problems.
Each answer's full trace is recorded for admins (app/services/rag_debug.py).

A question asking for table rows (RM lists, contacts, codes, payout grids, "how many") also gets the exact
matching spreadsheet rows, counted in code (app/rag/lookup.py), as the first passages.
"""
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, List, Optional, Tuple

from app.rag import (bankers, followup, grounding, lender_eligibility, live_check, lookup, profile as profiles,
                     retrieval, smalltalk, states, websearch)
from app.rag.llm import describe_params, get_llm, model_name, provider, rag_params
from app.rag.metering import purpose
from app.rag.policies import programs_for
from app.rag.questionnaire import llm_safe_case

log = logging.getLogger(__name__)
CONTACT_FIELDS = {"banker_rm", "mobile", "urban_money_code"}
ATTACHMENT_CHARS = 12000
NOTHING_FOUND = grounding.NOT_FOUND
PLACES_WAIT_S = 20                    # how long a finished answer waits for its branch map
PROFILE_POLICY_PASSAGES = 3           # policy passages sent with a profile's banker match
_PLACES_POOL = ThreadPoolExecutor(2, thread_name_prefix="places")


def _llm(required: bool = True):
    llm = get_llm("rag")
    if llm is None and required:
        key = "OPENAI_API_KEY" if provider() == "openai" else "GROQ_API_KEY"
        raise RuntimeError(f"{key} is not configured, so AI answers are unavailable.")
    return llm


def _ask_sections(case: dict, result: dict) -> List[Tuple[str, str]]:
    policies = [p.model_dump(mode="json", exclude=CONTACT_FIELDS | {"source"}) for p in programs_for(case.get("loan_type"))]
    slim = {k: result.get(k) for k in ("status", "top_lender", "program", "indicative_eligibility", "comparison",
                                       "evidence_source", "concerns_or_deviations")}
    return [("CASE (personal data removed)", json.dumps(llm_safe_case(case), indent=1, default=str)),
            ("LENDER PROGRAM RULES for this loan type (structured policy) [Rules]",
             json.dumps(policies, indent=1, default=str) if policies else "none (recommendation came from policy text or web)"),
            ("RESULT SHOWN TO THE PARTNER [Result]", json.dumps(slim, indent=1, default=str, ensure_ascii=False)[:8000])]


def _att(a):
    """(name, text, id, kind) for an attachment given as an Attachment or a plain (name, text) pair."""
    name, text = a[0], a[1]
    return name, text, (a[2] if len(a) > 2 else ""), (a[3] if len(a) > 3 else "file")


def _attachment_section(attachments) -> List[Tuple[str, str]]:
    if not attachments:
        return []
    blocks = []
    for i, a in enumerate(attachments, 1):
        name, text, _, kind = _att(a)
        what = "image, transcribed" if kind == "image" else "file"
        blocks.append(f"[A{i}] {name} ({what})\n{text[:ATTACHMENT_CHARS]}")
    return [("FILES ATTACHED BY THE USER to this question (user-provided, not part of the knowledge base). If the "
             "question is about them, answer from them with the same rules (exact figures, nothing invented) and cite "
             "them as [A1], [A2]", "\n\n".join(blocks))]


# "Summarize this", "what does the image say", "explain the attached file": answer from the upload alone.
_ABOUT_UPLOAD = re.compile(r"\b(this|these|the|attached|uploaded|above|my)\s+(docs?|documents?|files?|images?|pictures?|photos?|"
                           r"posters?|screenshots?|pdfs?|sheets?|letters?|attachments?|upload)\b|\battach|\bupload|"
                           r"\b(summari[sz]e|describe|explain|extract|transcribe|read)\b(?!.*\b(knowledge base|kb)\b)", re.I)


def about_upload(question: str, attachments) -> bool:
    return bool(attachments) and bool(_ABOUT_UPLOAD.search(question))


def _attachment_refs(attachments, answer: str, focus: bool) -> List[dict]:
    """Source cards for the attachments the answer cites (all of them when the question was about the upload)."""
    cited = {int(n) for n in re.findall(r"\[A(\d+)\]", answer)}
    out = []
    for i, a in enumerate(attachments, 1):
        name, text, aid, kind = _att(a)
        if i in cited or (focus and not cited):
            out.append({"ref": f"A{i}", "title": name, "source": name, "attachment": True, "attachment_id": aid,
                        "kind": kind, "location": "Your upload", "primary": False,
                        "excerpt": text[:700] + ("…" if len(text) > 700 else "")})
    return out


def _tail(partial: str) -> str:
    if not partial:
        return ""
    return ("\n\nYOUR EARLIER ANSWER TO THIS QUESTION WAS CUT OFF. Continue it from exactly where it stopped. "
            "Do not repeat any of it and do not start over. It ended with:\n" + partial[-1500:])


class Prepared:
    """Everything decided before generation: retrieval, context, prompt and whether to answer at all."""

    def __init__(self, mode, question, case, result, loan_type, history, attachments, partial, use_llm=True, profile=None):
        self.mode, self.question, self.profile, self.bankers, self.facts = mode, question, profile, None, None
        self.route = None                   # how the message relates to the conversation (app/rag/followup.py)
        self.route_obj = None
        self.state = None                   # set here when decided before generation (app/rag/states.py)
        self.state_info = None              # the final state, with its actions
        # A customer profile without a loan type or city: the app asks for them (nothing is searched or guessed).
        self.direct = profile.ask_answer() if profile and profile.missing_for_bankers() else ""
        if self.direct:
            self.state = states.MORE_INFORMATION_REQUIRED
        self.case, self.history, self.attachments = case or {}, history or [], list(attachments or [])
        # A question about the uploaded file is answered from the upload; the knowledge base is not searched, so
        # unrelated passages cannot leak into a summary of the user's own document.
        self.focus = about_upload(question, self.attachments)
        if self.direct:
            plan = retrieval.understand(question, self.history, loan_type, use_llm=False)
            self.retrieval = retrieval.Retrieval(
                plan=plan, candidates=[], selected=[], passes=[], timings={},
                validation={"sufficient": True, "reason": "", "checks": [
                    {"check": "customer profile complete", "ok": False, "info": True,
                     "detail": "missing: " + "; ".join(profile.missing_for_bankers()) + " (asked; not searched)"}]})
        elif self.focus:
            plan = retrieval.understand(question, self.history, loan_type, use_llm=False)
            self.retrieval = retrieval.Retrieval(
                plan=plan, candidates=[], selected=[], passes=[], timings={},
                validation={"sufficient": True, "reason": "", "checks": [
                    {"check": "question about the attached file", "ok": True,
                     "detail": "answered from the user's attachments; the knowledge base was not searched"}]})
        elif profile:      # a customer profile: the app picks the bankers; a focused query finds the product's policy
            lt = profile.loan_type or loan_type or ""
            self.bankers = (bankers.find(lt, profile.city, profile.lenders or None, profile.exclude or None)
                            if profile.lenders or profile.exclude else bankers.find(lt, profile.city))
            profile.eligibility = lender_eligibility.evaluate_all(profile.fields, self.bankers.lenders + profile.explain)
            stop = _profile_stop(profile, self.bankers)
            if stop:           # the app answers itself: no further lenders, or the RM lists could not be read
                self.direct, self.state = stop
                self.retrieval = retrieval.Retrieval(
                    plan=retrieval.understand(question, [], lt, use_llm=False), candidates=[], selected=[],
                    passes=[], timings={}, validation={"sufficient": True, "reason": "", "checks": [
                        {"check": "banker search", "ok": self.state != states.RETRIEVAL_FAILURE, "info": True,
                         "detail": self.bankers.error or "no further lender has an RM for this loan type and city"}]})
            else:
                self.retrieval = retrieval.retrieve(profile.search, [], profile.loan_type or loan_type, use_llm,
                                                    supporting=True)
        else:
            lt = loan_type or self.case.get("loan_type")
            plan = retrieval.understand(question, self.history, lt, use_llm)
            # "Bajaj home loan RM in Pune": the banker directory answers (de-duplicated, newest list first)
            # instead of raw spreadsheet rows, which repeat each banker once per monthly list and copy. Otherwise
            # a question asking for rows (codes, payouts, counts) gets the exact rows. Both are decided before the
            # passage search, which then only has to find supporting passages.
            self.bankers = _banker_question(question, plan)
            self.lookup = None if self.bankers else _table_lookup(question, plan)
            self.facts = None if self.bankers else _policy_facts(plan)        # approved exact values, if any
            self.retrieval = retrieval.retrieve(question, self.history, lt, use_llm, plan=plan,
                                                supporting=bool(self.bankers or self.lookup or self.facts))
        if self.focus or profile or self.direct:
            self.lookup = None
        docs = self.retrieval.docs
        if self.bankers:   # the chosen bankers' lists first ([S1]...), then at most 3 policy passages
            # Other contact-list passages are left out: their raw rows (other lenders, older numbers) only compete
            # with the app's de-duplicated choice, and the model picked from them instead.
            docs = self.bankers.documents() + [d for d in docs if d.metadata.get("source") not in
                                               {b.path for b in self.bankers.top} and not _is_contacts(d)][:PROFILE_POLICY_PASSAGES]
        if self.lookup:     # the exact rows first; chunks of the same files would only repeat (part of) them
            docs = self.lookup.documents() + [d for d in docs if d.metadata.get("source") not in self.lookup.paths]
        if self.facts:      # approved values come before everything else
            docs = [self.facts] + docs
        self.context, self.refs = grounding.build_context(docs)
        if self.bankers:
            bankers.number(self.bankers, self.refs)
        sections = ((_ask_sections(self.case, result or {}) if mode == "ask" else [])
                    + (profile.sections(self.bankers) if profile else [])
                    + _attachment_section(self.attachments))
        self.extra_text = "\n".join(body for _, body in sections)
        # A knowledge-base question with no reliable evidence is answered "not found" without the LLM.
        # (A profile is still answered: its table and eligibility stand without a passage.)
        self.not_found = (mode == "kb" and not self.retrieval.validation["sufficient"] and not self.attachments
                          and not partial and not self.lookup and not profile and not self.bankers and not self.facts
                          and not self.direct)
        if self.not_found and self.retrieval.degraded:
            # the search itself failed (even after its retry): "not found" was not established
            self.not_found, self.direct, self.state = False, states.RETRIEVAL_FAILURE_REPLY, states.RETRIEVAL_FAILURE
        rules = grounding.ASK_RULES if mode == "ask" else profiles.PROFILE_RULES if profile else ""
        # a profile carried over from earlier in the chat is answered for the whole customer, not "city pune"
        self.messages = grounding.messages(profile.question if profile else question, self.context, self.history,
                                           "" if profile else self.retrieval.plan.standalone, sections, rules, _tail(partial))

    def breakdown(self) -> dict:
        """Prompt size by component (characters), to split the provider's input tokens for admin reports."""
        human = len(self.messages[1].content)
        parts = {"system": len(self.messages[0].content), "context": len(self.context),
                 "history": len(grounding.history_text(self.history)), "question": len(self.question)}
        parts["other"] = max(0, human - parts["context"] - parts["history"] - parts["question"])
        return parts

    def finish(self, answer: str) -> dict:
        if self.direct:
            return {"sources": [], "unsupported_figures": [], "contact_mismatches": [], "invalid_citations": [],
                    "not_found": False}
        answer = grounding.normalize_citations(answer)
        sources = [] if self.not_found else grounding.cited_refs(answer, self.refs)
        if self.attachments and not self.not_found:
            att = _attachment_refs(self.attachments, answer, self.focus)
            # an answer that cites the upload but no passage did not use the knowledge base
            sources = ([r for r in sources if not r.get("uncited")] if att else sources) + att
        allowed = self.question + "\n" + "\n".join(str(m.get("text", "")) for m in self.history) + "\n" + self.extra_text
        figures = [] if self.not_found else grounding.verify_figures(answer, self.context, allowed)
        contacts = [] if self.not_found else grounding.verify_contacts(answer, self.context)
        return {"sources": sources, "unsupported_figures": figures, "contact_mismatches": contacts,
                "invalid_citations": grounding.invalid_citations(answer, self.refs),
                "not_found": self.not_found or grounding.is_not_found(answer)}

    @staticmethod
    def problems(post: dict) -> int:
        return len(post["unsupported_figures"]) + len(post["invalid_citations"]) + len(post.get("contact_mismatches", []))

    def trace(self, answer: str, post: dict, generation: Optional[dict], timings: dict) -> dict:
        from app.services import rag_debug
        cfg = rag_debug.config()
        return {"mode": self.mode, "question": self.question, "retrieval": self.retrieval.trace(),
                "context_refs": [{k: r.get(k) for k in ("ref", "source", "title", "page")} for r in self.refs],
                "lookup": self.lookup.trace() if self.lookup else None,
                "prompt": {"version": grounding.PROMPT_VERSION, "system": self.messages[0].content,
                           "context": self.context, "full": grounding.prompt_text(self.messages)},
                "attachments": [_att(a)[0] for a in self.attachments], "about_upload": self.focus,
                "app_written": bool(self.direct),           # the app's own reply (asks for details), not the model's
                "profile": self.profile.trace() if self.profile else None, "route": self.route,
                "state": self.state_info,
                "bankers": self.bankers.trace() if self.bankers else None,
                "generation": generation, "answer": answer, "citations": post["sources"],
                "unsupported_figures": post["unsupported_figures"], "invalid_citations": post["invalid_citations"],
                "contact_mismatches": post.get("contact_mismatches", []),
                "not_found": post["not_found"], "timings": {**self.retrieval.timings, **timings},
                "config": {**cfg, "hash": rag_debug.config_hash(cfg)}}


# Asks for people at a lender (not codes, payouts or pincodes, which the table lookup answers).
_ASKS_BANKER = re.compile(r"\b(rms?|sms?|asms?|bankers?|relationship managers?|sales managers?|managers?|contacts?|"
                          r"phone|mobile|whom|who is)\b", re.I)


def _banker_question(question: str, plan) -> Optional["bankers.Match"]:
    """The directory's bankers for a question naming a bank or product and a city; None when it is not such a
    question, the directory is not built yet, or nothing matches (then the table lookup is tried)."""
    from app.services import banker_directory
    text = plan.standalone if plan.rewrite == "llm" else question
    if not (_ASKS_BANKER.search(text) and (plan.banks or plan.products)) or {"code", "payout", "pincode"} & set(plan.metrics):
        return None
    try:
        city = banker_directory.known_city(text)
        if not city:
            return None
        m = bankers.find(plan.products[0] if plan.products else "", city, plan.banks or None)
    except Exception:
        log.exception("Banker directory lookup failed")
        return None
    return m if m.top and m.source == "directory" else None


def _is_contacts(doc) -> bool:
    try:
        from app.services import kb_versions
        return (kb_versions.authority_map().get(doc.metadata.get("source") or "") or {}).get("doc_type") == "Contacts"
    except Exception:
        return False


def _policy_facts(plan):
    from app.services import policy_facts
    return policy_facts.for_question(plan)


def _table_lookup(question: str, plan) -> Optional["lookup.Lookup"]:
    try:
        return lookup.find(plan.standalone if plan.rewrite == "llm" else question, plan.banks, plan.products)
    except Exception:
        log.exception("Table lookup failed")
        return None


def _correct(llm, prep: Prepared, answer: str, post: dict) -> Tuple[str, dict, dict]:
    """One rewrite of an answer that failed the source check. Returns (answer, post, what happened); the
    rewrite is used only if it has fewer problems and still answers (it is not a 'not found' reply)."""
    keys = ("unsupported_figures", "invalid_citations", "contact_mismatches")
    info = {"original": answer, "before": {k: post[k] for k in keys},
            "accepted": False}
    try:
        with purpose("answer_check"):
            msgs = grounding.correction_messages(prep.messages, answer, post["unsupported_figures"], post["invalid_citations"],
                                                 post["contact_mismatches"])
            fixed = grounding.normalize_citations(llm.invoke(msgs).content.strip())
    except Exception as e:
        log.warning("Answer correction failed: %s", e)
        info["error"] = f"{type(e).__name__}: {e}"[:200]
        return answer, post, info
    fpost = prep.finish(fixed)
    info["after"] = {k: fpost[k] for k in keys}
    if fixed and not grounding.is_not_found(fixed) and Prepared.problems(fpost) < Prepared.problems(post):
        info["accepted"] = True
        return fixed, fpost, info
    return answer, post, info


def _generation(llm=None, stopped: bool = False, correction: Optional[dict] = None, live: Optional[dict] = None) -> dict:
    d = describe_params(rag_params())
    return {**d, "model": model_name(llm) or d["model"], "stopped": stopped,
            **({"correction": correction} if correction else {}), **({"live_check": live} if live else {})}


def _brief(r: Optional[dict]) -> Optional[dict]:
    return r and {k: r.get(k) for k in ("verdict", "claims", "supported", "partial", "unsupported", "problems", "error")}


def _live_check(llm, prep: Prepared, answer: str, post: dict) -> Tuple[str, dict, dict, Optional[dict]]:
    """Check every claim of a finished answer (fast model); rewrite it once without unsupported claims and check the
    rewrite. Returns (answer, post, what happened, the partner-facing confidence)."""
    fast = get_llm("fast")
    first = live_check.check(prep, answer, fast)
    result, corrected, info = first, False, {"model": model_name(fast), "first": _brief(first)}
    claims = live_check.unsupported(first)
    if claims:
        try:
            with purpose("answer_check"):
                fixed = grounding.normalize_citations(llm.invoke(live_check.correction(prep, answer, claims)).content.strip())
            fpost = prep.finish(fixed)
            if fixed and not grounding.is_not_found(fixed) and Prepared.problems(fpost) <= Prepared.problems(post):
                again = live_check.check(prep, fixed, fast)
                info["after"] = _brief(again)
                if again and again.get("verdict") != "ERROR" and len(live_check.unsupported(again)) < len(claims):
                    answer, post, result, corrected = fixed, fpost, again, True
        except Exception as e:
            log.warning("Claim-check rewrite failed: %s", e)
            info["error"] = f"{type(e).__name__}: {e}"[:200]
    info["accepted"] = corrected
    conf = live_check.confidence(prep, post, result, corrected)
    info["confidence"] = conf
    return answer, post, info, conf


def _record(prep: Prepared, answer: str, post: dict, generation, timings, user_id, origin) -> Tuple[Optional[str], dict]:
    from app.services import rag_debug
    data = prep.trace(answer, post, generation, timings)
    return rag_debug.record(data, user_id, origin), data


def _status_after_search(prep: Prepared) -> dict:
    r = prep.retrieval
    if prep.direct:
        return {"stage": "read", "text": "Some customer details are missing"}
    if prep.focus:
        n = len(prep.attachments)
        return {"stage": "read", "text": f"Reading your {'file' if n == 1 else f'{n} files'}"}
    files = len({d.metadata.get("source") for d in r.docs})
    if prep.bankers is not None:
        n = len(prep.bankers.top)
        return {"stage": "read", "text": f"Matched {n} banker{'' if n == 1 else 's'} in {prep.bankers.city}" if n
                else "No matching banker in the knowledge base"}
    if prep.lookup:
        n, f = prep.lookup.total, prep.lookup.file_count
        return {"stage": "read", "text": f"Found {n} matching row{'' if n == 1 else 's'} in {f} table{'' if f == 1 else 's'}"}
    if prep.not_found:
        return {"stage": "read", "text": "No reliable evidence found in the knowledge base"}
    if not files:
        return {"stage": "read", "text": "No matching documents found"}
    return {"stage": "read", "text": f"Reading {files} relevant document{'' if files == 1 else 's'}"}


def _direct_trace(mode: str, question: str, what: str, answer: str, seconds: float) -> dict:
    """Trace for a small-talk reply: no retrieval, no knowledge-base prompt."""
    from app.services import rag_debug
    cfg = rag_debug.config()
    return {"mode": mode, "question": question, "route": f"direct ({what})",
            "retrieval": {"plan": {"question": question, "standalone": question, "queries": [], "rewrite": "none"},
                          "passes": [], "candidates": [], "selected": [], "timings": {},
                          "validation": {"sufficient": True, "reason": "", "checks": [
                              {"check": "conversational message", "ok": True, "info": True,
                               "detail": f"{what}: answered directly, the knowledge base was not searched"}]}},
            "prompt": {"version": "smalltalk (fixed reply, no LLM)", "system": "", "context": "", "full": ""},
            "generation": None,
            "answer": answer, "citations": [], "unsupported_figures": [], "invalid_citations": [], "not_found": False,
            "attachments": [], "timings": {"generate_s": round(seconds, 3)}, "config": {**cfg, "hash": rag_debug.config_hash(cfg)}}


def _web_trace(mode: str, question: str, results: List[dict], msgs, answer: str, post: dict, llm, seconds: dict) -> dict:
    """Trace for a web-search answer, in the same shape as a knowledge-base trace."""
    from app.services import rag_debug
    cfg = rag_debug.config()
    cands = [{"id": r["url"], "source": r["url"], "title": r["title"], "page": None, "section": websearch.domain(r["url"]),
              "bank": "", "product": "", "version": None, "status": "", "corpus": "web", "vector_score": None,
              "vector_rank": None, "keyword_score": None, "exact": False, "found_by": ["web"], "fused": None,
              "rerank": None, "score": None, "bank_match": False, "product_match": None, "dropped": "",
              "selected": True, "text": r["content"][:700]} for r in results]
    return {"mode": mode, "question": question, "route": "web",
            "retrieval": {"plan": {"question": question, "standalone": question, "queries": [question], "rewrite": "none"},
                          "passes": [], "candidates": cands, "selected": [c["id"] for c in cands], "timings": {},
                          "validation": {"sufficient": bool(results), "reason": "" if results else "no web results", "checks": [
                              {"check": "public-information question", "ok": True, "info": True,
                               "detail": f"answered from a Tavily web search ({len(results)} results); the knowledge base was not searched"}]}},
            "prompt": {"version": "web-v1", "system": websearch.SYSTEM, "context": websearch.context(results),
                       "full": grounding.prompt_text(msgs) if msgs else ""},
            "generation": _generation(llm) if llm is not None and results else None, "answer": answer,
            "citations": post["sources"], "unsupported_figures": post["unsupported"], "invalid_citations": [],
            "not_found": not results, "attachments": [], "timings": seconds, "config": {**cfg, "hash": rag_debug.config_hash(cfg)}}


def _web_post(answer: str, question: str, results: List[dict]) -> dict:
    refs = websearch.refs(results)
    answer = grounding.normalize_citations(answer)
    figures = grounding.verify_figures(answer, websearch.context(results), question) if results else []
    figures = [f for f in figures if not re.fullmatch(r"W\d+", f)]
    return {"sources": websearch.cited(answer, refs) if results else [],
            "unsupported": list(dict.fromkeys(websearch.unsupported_links(answer, results) + figures))}


def _stream_web(mode, question, history, user_id, llm) -> Iterator[Tuple[str, object]]:
    from app.services import rag_debug
    yield "status", {"stage": "search", "text": "Searching the web"}
    t0 = time.perf_counter()
    results = websearch.search(question)
    t_search = time.perf_counter() - t0
    yield "status", {"stage": "read", "text": f"Reading {len(results)} web result{'' if len(results) == 1 else 's'}"
                     if results else "No web results found"}
    msgs, parts, done = None, [], False
    try:
        if not results:
            parts.append(websearch.NOT_FOUND)
            yield "token", websearch.NOT_FOUND
            done = True
        else:
            yield "status", {"stage": "write", "text": "Preparing answer"}
            msgs = websearch.messages(question, results, history)
            with purpose("answer_web"):
                for chunk in llm.stream(msgs):
                    if chunk.content:
                        parts.append(chunk.content)
                        yield "token", chunk.content
            done = True
    finally:
        answer = "".join(parts)
        post = _web_post(answer, question, results)
        rag_debug.record(_web_trace(mode, question, results, msgs, answer, post, llm,
                                    {"search_s": round(t_search, 3), "total_s": round(time.perf_counter() - t0, 3),
                                     **({} if done else {"stopped": True})}), user_id, "chat")
    yield "sources", post["sources"]
    if post["unsupported"]:
        yield "verification", {"unsupported": post["unsupported"]}


def _is_profile(mode: str, question: str, attachments, partial: str) -> bool:
    """A new knowledge-base question that describes a customer and asks which bankers to approach."""
    return mode == "kb" and not partial and not attachments and profiles.detect(question)


def _read_profile(question: str) -> Optional["profiles.Profile"]:
    """The extracted profile, or None (then the question is answered like any other)."""
    try:
        return profiles.extract(question)
    except Exception:
        log.exception("Could not read the customer profile")
        return None


def _route(mode: str, question: str, history, attachments, partial: str, user_id: Optional[int] = None):
    """A knowledge-base message in a conversation: how it relates to the customer and answers before it
    (followup.py). Returns (route or None, the history to answer with: none for a new question or case)."""
    if mode != "kb" or partial or attachments:
        return None, history
    if not history:
        if followup.needs_lender(question, []):
            return followup.Route(relation="clarify", reason="a lender fact with no lender or product",
                                  direct=followup.ASK_WHICH), history
        return None, history
    route = followup.resolve(question, history, user_id, _conversation_id())
    if route and route.relation in ("new", "new_case"):
        return route, []
    return route, history


def _conversation_id() -> Optional[str]:
    from app.services import rag_debug
    try:
        return (rag_debug.message_link.get() or (None, None))[0]
    except LookupError:
        return None


def _profile_stop(profile, match) -> Optional[Tuple[str, str]]:
    """(the app's own reply, state) when a customer's banker search cannot be answered from the RM lists."""
    if match.top:
        return None
    if match.error:
        return states.RM_LISTS_FAILURE_REPLY, states.RETRIEVAL_FAILURE
    if profile.exclude and not profile.lenders:
        lt, city = profile.loan_type or "loan", profile.city
        rules = ("" if lender_eligibility.any_rules(profile.loan_type) else
                 " These lenders are matched by RM availability; their eligibility rules aren't loaded in the app, so "
                 "this is not a judgement on eligibility.")
        return (f"I couldn't find additional lenders beyond the {len(profile.exclude)} already listed for this customer "
                f"({lt} in {city}): the current RM lists have no other {lt} RM in {city}.{rules}\n\n"
                "You can broaden the search by changing the city, the loan type or the customer's details.",
                states.NO_ADDITIONAL_MATCHES)
    return None


def _direct_route(mode, question, route, user_id, origin) -> Tuple[str, dict, Optional[str]]:
    """A follow-up the app answers itself (a new case, a clarifying question): (answer, state, trace id)."""
    st = states.CLARIFICATION_REQUIRED if route.relation == "clarify" else states.MORE_INFORMATION_REQUIRED
    info = {"state": st, "retry": False, "notice": "", "actions": [states.action(o[:40], o) for o in route.options]}
    tid, _ = _record_direct(mode, question, f"follow-up: {route.relation}", route.direct, 0.0, user_id, origin,
                            extra={"route": route.trace(), "state": info})
    return route.direct, info, tid


def _remember(prep: "Prepared", user_id: Optional[int]) -> Optional[str]:
    """Store a customer's banker recommendation (app/services/recommendations.py)."""
    prof, match = prep.profile, prep.bankers
    if prof is None or match is None:
        return None
    from app.services import recommendations
    shown = match.lenders
    return recommendations.save(
        user_id, _conversation_id(), relation=prof.relation or "new_profile", question=prof.question,
        loan_type=prof.loan_type, city=prof.city,
        profile={"fields": prof.fields, "other": prof.other, "scenario": prof.scenario},
        shown=shown, seen=list(dict.fromkeys(prof.seen_before + shown)), evaluations=prof.eligibility,
        sources=list(dict.fromkeys(b.path for b in match.top)), state=(prep.state_info or {}).get("state", ""),
        base_id=prof.base_id, hypothetical=prof.hypothetical)


def _start_places(prep: Prepared):
    """For a customer profile with a city: start looking up the lenders' branches (map and place cards)."""
    if not (prep.profile and prep.profile.city):
        return None
    from app.services import places
    lenders = prep.bankers.lenders if prep.bankers else []
    if not lenders:
        return None
    return _PLACES_POOL.submit(places.find, lenders, prep.profile.city)


def _finish_places(future) -> Optional[dict]:
    """The branches of the app's chosen lenders; None if the lookup failed or is too slow."""
    try:
        return future.result(timeout=PLACES_WAIT_S)
    except Exception:
        log.warning("Branch lookup unavailable", exc_info=True)
        return None


def _record_direct(mode, question, what, answer, seconds, user_id, origin, extra: Optional[dict] = None):
    from app.services import rag_debug
    data = {**_direct_trace(mode, question, what, answer, seconds), **(extra or {})}
    return rag_debug.record(data, user_id, origin), data


def stream(mode: str, question: str, case: Optional[dict] = None, result: Optional[dict] = None,
           loan_type: Optional[str] = None, history: Optional[List[dict]] = None, attachments=(), partial: str = "",
           user_id: Optional[int] = None) -> Iterator[Tuple[str, object]]:
    """Streaming answer for the chat UI. Yields (event, payload): ('status', {'stage', 'text'}) as each step
    starts, ('token', text) as the model writes, then ('sources', [...]) and, when a figure in the answer could
    not be matched to the sources, ('verification', {'unsupported': [...]}).
    Small talk ("hi", "thanks") is answered directly, without searching the knowledge base."""
    what = None if partial else smalltalk.kind(question, attachments)
    if what:
        parts, t0 = [], time.perf_counter()
        try:
            for piece in smalltalk.reply_stream(question, history, what):
                parts.append(piece)
                yield "token", piece
        finally:
            _record_direct(mode, question, what, "".join(parts), time.perf_counter() - t0, user_id, "chat")
        yield "sources", []
        return
    llm = _llm()
    if not partial and websearch.available() and websearch.is_web_question(question, attachments):
        yield from _stream_web(mode, question, history, user_id, llm)
        return
    prof, route = None, None
    if _is_profile(mode, question, attachments, partial):
        yield "status", {"stage": "search", "text": "Reading the customer profile"}
        prof = _read_profile(question)
    elif mode == "kb" and not partial and not attachments:
        if history:
            yield "status", {"stage": "search", "text": "Reading the conversation"}
        route, history = _route(mode, question, history, attachments, partial, user_id)
        prof = route.profile if route else None
        if route is not None and route.direct:
            text, info, _ = _direct_route(mode, question, route, user_id, "chat")
            yield "token", text
            yield "sources", []
            yield "state", info
            return
    if prof:
        where = " ".join(x for x in [prof.city, prof.loan_type.lower()] if x)
        yield "status", {"stage": "search", "text": f"Searching knowledge base for {where} bankers" if where
                         else "Searching knowledge base for bankers"}
    else:
        yield "status", ({"stage": "search", "text": "Looking at your upload"} if about_upload(question, attachments)
                         else {"stage": "search", "text": "Searching knowledge base"})
    if prof:
        llm = get_llm("rag", max_tokens=profiles.MAX_TOKENS) or llm
    prep = Prepared(mode, question, case, result, loan_type, history, attachments, partial, profile=prof)
    prep.route, prep.route_obj = (route.trace() if route else None), route
    yield "status", _status_after_search(prep)
    if prep.not_found or prep.direct:
        text = prep.direct or NOTHING_FOUND
        post = prep.finish(text)
        prep.state_info = states.decide(prep, post, None)
        _record(prep, text, post, None, {}, user_id, "chat")
        _remember(prep, user_id)
        yield "token", text
        yield "sources", []
        yield "state", prep.state_info
        return
    places = _start_places(prep)                  # looked up while the answer is written
    yield "status", {"stage": "write", "text": "Preparing answer"}
    parts, done, t0 = [], False, time.perf_counter()
    answer, post, correction, live, conf = partial or "", None, None, None, None
    try:
        with purpose("answer", prep.breakdown()):
            for chunk in llm.stream(prep.messages):
                if chunk.content:
                    parts.append(chunk.content)
                    yield "token", chunk.content
        done = True
        answer = (partial or "") + "".join(parts)
        post = prep.finish(answer)
        if not partial and prep.problems(post):          # failed the source check: one rewrite
            yield "status", {"stage": "check", "text": "Double-checking figures against the sources"}
            answer, post, correction = _correct(llm, prep, answer, post)
            if correction["accepted"]:
                yield "replace", answer
        if not partial and live_check.enabled():          # every claim against the passages
            yield "status", {"stage": "check", "text": "Checking every fact against the documents"}
            checked, post, live, conf = _live_check(llm, prep, answer, post)
            if checked != answer:
                answer = checked
                yield "replace", answer
    finally:
        if post is None:
            answer = (partial or "") + "".join(parts)
            post = prep.finish(answer)
        prep.state_info = states.decide(prep, post, conf)
        _record(prep, answer, post, _generation(llm, stopped=not done, correction=correction, live=live),
                {"generate_s": round(time.perf_counter() - t0, 3)}, user_id, "chat")
        if done:
            _remember(prep, user_id)
    yield "sources", post["sources"]
    if conf:
        yield "confidence", conf
    yield "state", prep.state_info
    if places is not None:
        found = _finish_places(places)
        if found:
            yield "places", found
    if post["unsupported_figures"] or post["contact_mismatches"]:
        yield "verification", {"unsupported": post["unsupported_figures"] + post["contact_mismatches"],
                               **({"contact_mismatches": post["contact_mismatches"]} if post["contact_mismatches"] else {})}


def run(mode: str, question: str, case: Optional[dict] = None, result: Optional[dict] = None,
        loan_type: Optional[str] = None, history: Optional[List[dict]] = None, attachments=(),
        user_id: Optional[int] = None, origin: str = "api", require_llm: bool = True) -> dict:
    """Non-streaming answer (API, admin test bench, golden tests). Without an LLM (require_llm=False) the
    retrieval still runs and is evaluated; the answer is then empty and marked llm_skipped."""
    what = smalltalk.kind(question, attachments)
    if what:
        t0 = time.perf_counter()
        answer = "".join(smalltalk.reply_stream(question, history, what)).strip()
        tid, data = _record_direct(mode, question, what, answer, time.perf_counter() - t0, user_id, origin)
        return {"answer": answer, "sources": [], "unsupported_figures": [], "not_found": False, "llm_skipped": False,
                "trace_id": tid, "trace": data}
    llm = _llm(required=require_llm)
    if llm is not None and websearch.available() and websearch.is_web_question(question, attachments):
        from app.services import rag_debug
        t0 = time.perf_counter()
        results = websearch.search(question)
        msgs = websearch.messages(question, results, history) if results else None
        if msgs:
            with purpose("answer_web"):
                answer = llm.invoke(msgs).content.strip()
        else:
            answer = websearch.NOT_FOUND
        post = _web_post(answer, question, results)
        data = _web_trace(mode, question, results, msgs, answer, post, llm, {"total_s": round(time.perf_counter() - t0, 3)})
        return {"answer": answer, "sources": post["sources"], "unsupported_figures": post["unsupported"],
                "not_found": not results, "llm_skipped": False, "trace_id": rag_debug.record(data, user_id, origin), "trace": data}
    prof, route = None, None
    if llm is not None and _is_profile(mode, question, attachments, ""):
        prof = _read_profile(question)
    elif llm is not None:
        route, history = _route(mode, question, history, attachments, "", user_id)
        prof = route.profile if route else None
        if route is not None and route.direct:
            text, info, tid = _direct_route(mode, question, route, user_id, origin)
            return {"answer": text, "sources": [], "unsupported_figures": [], "not_found": False, "llm_skipped": False,
                    "state": info, "trace_id": tid, "trace": {"route": route.trace(), "state": info}}
    if prof:
        llm = get_llm("rag", max_tokens=profiles.MAX_TOKENS) or llm
    prep = Prepared(mode, question, case, result, loan_type, history, attachments, "", use_llm=llm is not None, profile=prof)
    prep.route, prep.route_obj = (route.trace() if route else None), route
    generation, t0 = None, time.perf_counter()
    if prep.direct:
        answer = prep.direct
    elif prep.not_found:
        answer = NOTHING_FOUND
    elif llm is None:
        answer = ""
    else:
        with purpose("answer", prep.breakdown()):
            answer = llm.invoke(prep.messages).content.strip()
        generation = _generation(llm)
    post = prep.finish(answer)
    correction, live, conf = None, None, None
    if llm is not None and not prep.not_found and not prep.direct and prep.problems(post):
        answer, post, correction = _correct(llm, prep, answer, post)
    if llm is not None and generation is not None and live_check.enabled():
        answer, post, live, conf = _live_check(llm, prep, answer, post)
    if generation is not None:
        generation = _generation(llm, correction=correction, live=live)
    prep.state_info = states.decide(prep, post, conf)
    tid, data = _record(prep, answer, post, generation, {"generate_s": round(time.perf_counter() - t0, 3)}, user_id, origin)
    _remember(prep, user_id)
    return {"answer": answer, "sources": post["sources"], "unsupported_figures": post["unsupported_figures"],
            "contact_mismatches": post["contact_mismatches"], "not_found": post["not_found"], "llm_skipped": llm is None and not prep.not_found,
            "confidence": conf, "state": prep.state_info, "trace_id": tid, "trace": data}


def answer(question: str, case: dict, result: dict, history: List[dict]) -> str:
    """Follow-up question about a recommendation (POST /api/chat)."""
    return run("ask", question, case, result, history=history)["answer"]


def ask_kb(question: str, loan_type: Optional[str] = None, history: Optional[List[dict]] = None) -> dict:
    """Standalone knowledge-base question (POST /api/kb/ask)."""
    r = run("kb", question, loan_type=loan_type, history=history)
    return {"answer": r["answer"], "sources": r["sources"], "unsupported_figures": r["unsupported_figures"]}
