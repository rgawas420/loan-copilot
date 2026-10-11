"""Agentic RAG graph. The rules engine decides; the LLM explains.

check_case --missing--> more_info
    | ready
compute_eligibility -> match_policies
    structured programs found -> retrieve_evidence -> explain                     (source: structured_policy)
    none -> build_query -> retrieve_kb -> grade_kb
                good -> recommend_from_text                                       (source: private_kb)
                weak -> rewrite_query -> retrieve_kb (once)
                weak again -> search_web -> web_guidance (general criteria only)  (source: web_general)
"""
import json
import logging
from functools import lru_cache
from typing import Iterator, Literal, Tuple

from langgraph.graph import END, START, StateGraph

from app.core.config import get_settings
from app.rag.llm import get_llm, structured
from app.rag.questionnaire import enough_info, llm_safe_case, missing_critical, prune_case
from app.rag.rules import DEV, FAIL, PASS, UNKNOWN, WARN, match, rupees
from app.rag.state import AgentState, EvidenceGrade, Explanation, Recommendation, WebGuidance
from app.rag.eligibility import indicative_eligibility

log = logging.getLogger(__name__)


def search(query: str, loan_type=None, lenders=None, k: int = 6):
    """Policy passages for the recommendation, from the same pipeline as chat answers (hybrid search, ACTIVE
    versions only, reranking, relevance threshold, evidence check): passages below the threshold are not
    evidence, so a weak result goes on to the rewrite / web steps instead of being explained. The matched
    lenders' own policy text comes first."""
    from app.rag import retrieval
    q = f"{query} {' '.join(lenders)}" if lenders else query
    docs = retrieval.retrieve(q, loan_type=loan_type, use_llm=False).docs
    if lenders:
        own = [d for d in docs if d.metadata.get("lender") in lenders or d.metadata.get("bank") in lenders]
        docs = own + [d for d in docs if d not in own]
    return docs[:k]
MAX_RETRIES = 1
QUERY_KEYS = ["loan_type", "hl_purpose", "profile", "age", "cibil", "overdue", "settled", "prop_use", "legal_cat",
              "possession", "rera", "constitution", "business_years", "salary_mode"]
NOT_IN_KB = "Not available in KB"


def _llm(kind: str = "fast"):
    return get_llm(kind)


def _web():
    if not get_settings().tavily_api_key:
        return None
    from langchain_tavily import TavilySearch
    return TavilySearch(max_results=5, topic="general", include_answer=True, include_raw_content=False,
                        tavily_api_key=get_settings().tavily_api_key)


def _safe_json(case):
    return json.dumps(llm_safe_case(case), indent=1, default=str)


# ---------------------------------------------------------------- gate + eligibility
def check_case(state: AgentState):
    ok, _ = enough_info(state["case"])
    missing = [] if ok else missing_critical(state["case"])
    return {"missing": missing, "retry_count": 0,
            "trace": [f"Case check -> {'READY' if ok else 'MISSING: ' + ', '.join(missing)}"]}


def route_after_gate(state) -> Literal["compute_eligibility", "more_info"]:
    return "more_info" if state["missing"] else "compute_eligibility"


def more_info(state: AgentState):
    return {"result": {"status": "More Information Required", "missing_fields": state["missing"],
                       "message": "Answer the missing questions, then search again."},
            "evidence_source": "none", "trace": ["Stopped: more information required"]}


def compute_eligibility(state: AgentState):
    c = state["case"]
    e = indicative_eligibility(c)
    flags = []
    if c.get("overdue") == "Yes": flags.append("Customer reports current overdue/DPD")
    if c.get("settled") == "Yes": flags.append("Customer reports a settled/written-off account")
    if str(c.get("cibil", "")).lower().startswith("not"): flags.append("CIBIL not checked")
    if "Do Not Know" in (c.get("overdue"), c.get("settled")): flags.append("Overdue/settlement history unknown")
    return {"eligibility": e, "flags": flags,
            "trace": [f"Indicative eligibility ({e['method']}) -> max {rupees(e['indicative_max_loan'])} "
                      f"vs requested {rupees(e['requested'])}"]}


# ---------------------------------------------------------------- structured path
def match_policies(state: AgentState):
    m = match(state["case"])
    if not m:
        return {"matches": [], "trace": ["Rules engine -> no structured policy for this loan type"]}
    counts = {s: sum(r["status"] == s for r in m) for s in ("Strong Match", "Possible - Credit Check Required", "Not Suitable")}
    return {"matches": m, "trace": [f"Rules engine -> {len(m)} programs checked: {counts['Strong Match']} strong, "
                                    f"{counts['Possible - Credit Check Required']} possible, {counts['Not Suitable']} not suitable"]}


def route_after_match(state) -> Literal["retrieve_evidence", "build_query"]:
    return "retrieve_evidence" if state["matches"] else "build_query"


def retrieve_evidence(state: AgentState):
    top = [r["lender"] for r in state["matches"][:3]]
    try:
        docs = search(f"{state['case'].get('loan_type')} policy eligibility", state["case"].get("loan_type"), top, k=6)
    except Exception:
        log.exception("Evidence retrieval failed")
        docs = []
    return {"kb_docs": docs, "trace": [f"Policy text retrieval -> {len(docs)} supporting sections"]}


def _concerns(r):
    return [f"{c['criterion']}: {c['detail']}" for c in r["checks"] if c["result"] in (FAIL, DEV, UNKNOWN, WARN)]


def _fallback_explanation(top):
    return Explanation(
        summary=f"{top['lender']} ({top['program']}) is the best fit under the structured rules: {top['status']}.",
        why_suitable=[f"{c['criterion']}: {c['detail']}" for c in top["checks"] if c["result"] == PASS],
        next_steps=[f"Resolve: {c['criterion']}: {c['detail']}" for c in top["checks"] if c["result"] in (DEV, UNKNOWN)])


def explain(state: AgentState):
    m = state["matches"]
    top = m[0]
    fits = top["status"] != "Not Suitable"
    brief = [{"lender": r["lender"], "program": r["program"], "status": r["status"],
              "max_loan": rupees(r["max_loan"]),
              "checks": [f"{c['result'].upper()} {c['criterion']}: {c['detail']}" for c in r["checks"]]} for r in m[:3]]
    llm = _llm("smart")
    how = "template"
    exp = _fallback_explanation(top)
    if llm is not None:
        evidence = "\n\n".join(f"[{d.metadata.get('source')}]\n{d.page_content}" for d in state["kb_docs"])
        try:
            exp = structured(llm, Explanation, f"""You explain a lender match to an Urban Money channel partner.
The rules engine has ALREADY decided the status of each program. Do not change or question it.

CASE (personal data removed):
{_safe_json(state['case'])}

RISK FLAGS: {state['flags'] or 'none'}

RULES ENGINE RESULTS (best first):
{json.dumps(brief, indent=1, ensure_ascii=False)}

POLICY TEXT (supporting evidence):
{evidence or 'none'}

Write for the TOP program ({top['lender']} - {top['program']}, status {top['status']}).
- why_suitable: 2-4 short points, only from PASS checks or the policy text. {'' if fits else 'The case does not fit; return an empty list.'}
- next_steps: 1-4 concrete actions that would close DEVIATION / UNKNOWN / FAIL checks (e.g. pull CIBIL, reduce amount to X).
- Never invent rates, limits, contacts or codes. Plain language. No markdown.
- Policy text that starts with "Source:" comes from the Urban Money knowledge base and is authoritative; use it
  for next steps (e.g. which RM / code applies), but never to override the rules engine's status.
Return JSON only matching this schema:
{json.dumps(Explanation.model_json_schema())}""")
            how = "LLM"
        except Exception:
            log.exception("Explanation LLM call failed; using template")

    base = {"evidence_source": "structured_policy", "eligibility_calc": top["eligibility"],
            "summary": exp.summary, "next_steps": exp.next_steps,
            "comparison": [{k: r[k] for k in ("lender", "program", "status", "max_loan", "checks", "sample")} for r in m],
            "citations": [{"source": r["source"], "lender": r["lender"], "program": r["program"]} for r in m[:3]] +
                         [{"source": d.metadata.get("source"), "lender": d.metadata.get("lender"),
                           "program": d.metadata.get("program")} for d in state["kb_docs"]]}
    e = top["eligibility"]
    if fits:
        result = {**base, "status": top["status"], "top_lender": top["lender"], "program": top["program"],
                  "indicative_eligibility": (f"Up to {rupees(top['max_loan'])} ({e['method']}; FOIR {e['assumed_FOIR']:.0%}, "
                                             f"{e['assumed_ROI_%']}% for {e['assumed_tenure_yrs']:g} yrs, lender assumptions)"
                                             if top["max_loan"] is not None else "Not computed: income not captured"),
                  "why_suitable": exp.why_suitable, "concerns_or_deviations": _concerns(top),
                  "banker_rm": top["banker_rm"], "mobile": top["mobile"], "urban_money_code": top["urban_money_code"],
                  "policy_last_verified": top["policy_last_verified"],
                  "alternative_lenders": [{"lender": r["lender"], "program": r["program"], "status": r["status"]}
                                          for r in m[1:] if r["status"] != "Not Suitable"]}
    else:
        result = {**base, "status": "Not Suitable", "top_lender": None,
                  "message": f"No program in the policy base fits this case. Closest: {top['lender']} - {top['program']}.",
                  "why_suitable": [], "concerns_or_deviations": _concerns(top), "alternative_lenders": []}
    return {"result": result, "evidence_source": "structured_policy",
            "trace": [f"Explanation ({how}) -> {result['status']}" + (f" / {top['lender']}" if fits else "")]}


# ---------------------------------------------------------------- text-only path
def build_query(state: AgentState):
    c = state["case"]
    q = "Lender policy eligibility for " + "; ".join(f"{k}={c[k]}" for k in QUERY_KEYS if c.get(k))
    return {"query": state.get("query") or q, "trace": ["Built retrieval query"]}


def retrieve_kb(state: AgentState):
    try:
        docs = search(state["query"], state["case"].get("loan_type"), k=6)
    except Exception:
        log.exception("KB retrieval failed")
        docs = []
    return {"kb_docs": docs, "trace": [f"Lender KB retrieval -> {len(docs)} chunks"]}


def grade_kb(state: AgentState):
    lt = state["case"].get("loan_type")
    docs = state["kb_docs"]
    if any(d.metadata.get("loan_type") == lt for d in docs):
        return {"kb_grade": "good", "trace": ["KB evidence grade -> GOOD (policy tagged with this loan type)"]}
    llm = _llm("fast")
    if not docs or llm is None:
        return {"kb_grade": "weak", "trace": ["KB evidence grade -> WEAK (no tagged policy)"]}
    ctx = "\n\n".join(d.page_content for d in docs)
    g = structured(llm, EvidenceGrade, f"""You grade evidence.
Case: loan_type={lt}, profile={state['case'].get('profile')}.
Policy evidence:
{ctx}

Does this evidence contain a lender policy for THIS loan type that can be used to judge suitability?
Return JSON only, e.g. {{"grade": "good"}} or {{"grade": "weak"}}.""")
    return {"kb_grade": g.grade, "trace": [f"KB evidence grade -> {g.grade.upper()} (untagged documents, LLM graded)"]}


def after_kb_grade(state) -> Literal["recommend_from_text", "rewrite_query", "search_web"]:
    if state["kb_grade"] == "good":
        return "recommend_from_text"
    if state["retry_count"] < MAX_RETRIES and _llm("fast") is not None:
        return "rewrite_query"
    return "search_web"


def rewrite_query(state: AgentState):
    new = _llm("fast").invoke(f"""Rewrite this search query to retrieve lender eligibility policy better. Keep the intent,
be specific, return only the query.\n\n{state['query']}""").content.strip()
    return {"query": new, "retry_count": state["retry_count"] + 1, "kb_grade": "",
            "trace": [f"Query rewritten -> {new[:90]}"]}


def _verbatim(value, evidence):
    v = (value or "").strip()
    return v if v and v.lower() not in ("none", "unknown") and v in evidence else None


def recommend_from_text(state: AgentState):
    llm = _llm("smart")
    if llm is None:
        return {"result": {"status": "More Information Required", "evidence_source": "private_kb",
                           "message": "Policy text was found but GROQ_API_KEY is not set, so it cannot be read. "
                                      "Add a structured policy (YAML) for this loan type, or configure the LLM."},
                "evidence_source": "private_kb", "trace": ["Stopped: LLM not configured"]}
    docs = state["kb_docs"]
    evidence = "\n\n".join(f"[{d.metadata.get('source')}]\n{d.page_content}" for d in docs)
    out = structured(llm, Recommendation, f"""You are a loan recommendation analyst for Urban Money channel partners.

CASE (structured, personal data removed):
{_safe_json(state['case'])}

INDICATIVE ELIGIBILITY (computed, assumptions included):
{json.dumps(state['eligibility'])}

RISK FLAGS FROM THE FORM: {state['flags'] or 'none'}

EVIDENCE (lender policy text):
{evidence}

RULES
- Use ONLY the evidence. Never invent lenders, rates, limits, contacts or codes.
- Excerpts that start with "Source:" come from the Urban Money knowledge base and are authoritative.
- banker_rm, mobile, urban_money_code and policy_last_verified must be copied exactly from the evidence; otherwise write "Not available in KB" / "Unknown".
- Strong Match = meets stated criteria. Possible - Credit Check Required = borderline (CIBIL band, unknown DPD, deviation needed).
  Not Suitable = clearly breaches a hard rule. More Information Required = policy needs facts we do not have.
- Put other qualifying lenders/programs in alternative_lenders.
Return valid JSON only matching this JSON schema:
{json.dumps(Recommendation.model_json_schema())}""")
    d = out.model_dump()
    # Guardrails: contacts must appear verbatim in the evidence; text-only matches are never 'Strong Match'.
    for k in ("banker_rm", "mobile", "urban_money_code"):
        d[k] = _verbatim(d[k], evidence) or NOT_IN_KB
    d["policy_last_verified"] = _verbatim(d["policy_last_verified"], evidence) or "Unknown"
    if str(d["top_lender"]).strip().lower() in ("", "none", "null", "n/a"):
        d["top_lender"] = None
    if d["status"] == "Strong Match":
        d["status"] = "Possible - Credit Check Required"
        d["concerns_or_deviations"].append("Matched from policy text only (no structured rules for this lender yet); "
                                           "verify the criteria with the RM.")
    d.update(evidence_source="private_kb", eligibility_calc=state["eligibility"],
             citations=[{"source": x.metadata.get("source"), "lender": x.metadata.get("lender"),
                         "program": x.metadata.get("program")} for x in docs])
    return {"result": d, "evidence_source": "private_kb", "trace": [f"Recommendation from policy text -> {d['status']}"]}


def search_web(state: AgentState):
    web = _web()
    if web is None:
        return {"web_results": "", "trace": ["Web search skipped (TAVILY_API_KEY not set)"]}
    c = state["case"]
    q = f"{c.get('loan_type')} India lender eligibility criteria {c.get('profile') or ''} CIBIL income".strip()
    try:
        r = web.invoke({"query": q})
    except Exception:
        log.exception("Web search failed")
        return {"web_results": "", "trace": ["Web search failed"]}
    if isinstance(r, dict):
        lines = [f"Tavily answer: {r.get('answer', '')}"] if r.get("answer") else []
        lines += [f"Title: {i.get('title')}\nURL: {i.get('url')}\nContent: {i.get('content')}" for i in r.get("results", [])]
        text = "\n\n".join(lines)
    else:
        text = str(r)
    return {"web_results": text, "trace": [f"Web search -> {len(text)} characters (unverified, general only)"]}


def web_guidance(state: AgentState):
    base = {"status": "More Information Required", "evidence_source": "web_general", "top_lender": None,
            "message": "No lender policy for this product is in the knowledge base. Add a structured policy (YAML) "
                       "or a policy document for this loan type to get a lender recommendation."}
    llm = _llm("fast")
    if not state["web_results"] or llm is None:
        return {"result": {**base, "evidence_source": "none"}, "trace": ["Stopped: insufficient evidence"]}
    g = structured(llm, WebGuidance, f"""Summarise GENERAL eligibility criteria that Indian lenders typically apply
for a {state['case'].get('loan_type')}, from the web results below. Do not name or recommend any lender.
{state['web_results'][:6000]}
Return JSON only matching: {json.dumps(WebGuidance.model_json_schema())}""")
    return {"result": {**base, "web_summary": g.summary, "general_criteria": g.general_criteria,
                       "citations": [{"source": "Tavily web search (unverified)"}]},
            "evidence_source": "web_general", "trace": ["General guidance from web (no lender recommended)"]}


# ---------------------------------------------------------------- graph
@lru_cache
def build_graph():
    g = StateGraph(AgentState)
    for fn in (check_case, more_info, compute_eligibility, match_policies, retrieve_evidence, explain, build_query,
               retrieve_kb, grade_kb, rewrite_query, recommend_from_text, search_web, web_guidance):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "check_case")
    g.add_conditional_edges("check_case", route_after_gate, ["compute_eligibility", "more_info"])
    g.add_edge("compute_eligibility", "match_policies")
    g.add_conditional_edges("match_policies", route_after_match, ["retrieve_evidence", "build_query"])
    g.add_edge("retrieve_evidence", "explain")
    g.add_edge("build_query", "retrieve_kb")
    g.add_edge("retrieve_kb", "grade_kb")
    g.add_conditional_edges("grade_kb", after_kb_grade, ["recommend_from_text", "rewrite_query", "search_web"])
    g.add_edge("rewrite_query", "retrieve_kb")
    g.add_edge("search_web", "web_guidance")
    for n in ("more_info", "explain", "recommend_from_text", "web_guidance"):
        g.add_edge(n, END)
    return g.compile()


def _initial(case):
    return {"case": prune_case(case), "missing": [], "eligibility": {}, "flags": [], "matches": [], "query": "",
            "kb_docs": [], "web_results": "", "kb_grade": "", "retry_count": 0, "evidence_source": "",
            "result": {}, "trace": []}


def run_recommendation(case: dict) -> dict:
    final = build_graph().invoke(_initial(case))
    return {**final["result"], "trace": final["trace"]}


def stream_recommendation(case: dict) -> Iterator[Tuple[str, object]]:
    """Yield ('step', trace line) as each node finishes, then ('result', final result)."""
    trace, result = [], {}
    for update in build_graph().stream(_initial(case), stream_mode="updates"):
        for node_update in update.values():
            for line in (node_update or {}).get("trace", []):
                trace.append(line)
                yield "step", line
            if (node_update or {}).get("result"):
                result = node_update["result"]
    yield "result", {**result, "trace": trace}
