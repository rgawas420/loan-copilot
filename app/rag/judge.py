"""Claim-level judge for golden tests.

The answer check in grounding.py only proves that figures, citation numbers and phone numbers appear in the
sources. It cannot tell whether a sentence is true: "SBI does not accept self-employed applicants [S2]" passes
it even when S2 says nothing of the kind. This judge closes that gap for the golden tests:

    answer ─> split into factual claims ─> each claim checked against the passages it cites
                                            supported / partial / unsupported / not_checkable, citation right?
           ─> the whole answer compared with the reviewer's reference answer: correct / partial / incorrect

Refusals ("couldn't find this information") are judged without the LLM: right when the reference says the
knowledge base has no answer, a false refusal when it has one.

verdict: FAIL  an unsupported claim, or an incorrect answer (a golden test with a FAIL does not pass)
         WARN  a partly supported claim, a claim citing the wrong passage, or a partly correct answer
         PASS  otherwise
"""
import logging
from typing import List, Optional

from pydantic import BaseModel, Field

from app.rag import grounding

log = logging.getLogger(__name__)
JUDGE_PROMPT_VERSION = "judge-v2"
NOT_FOUND_REFERENCE = "(not found)"
CLAIM_VERDICTS = ("supported", "partial", "unsupported", "not_checkable")
CORRECTNESS = ("correct", "partial", "incorrect", "n/a")


class JudgedClaim(BaseModel):
    claim: str = ""
    cited: List[str] = Field(default_factory=list)
    verdict: str = "unsupported"
    citation_ok: bool = True
    evidence: str = ""
    reason: str = ""


class Judgement(BaseModel):
    claims: List[JudgedClaim] = Field(default_factory=list)
    correctness: str = "n/a"
    correctness_reason: str = ""
    missing: List[str] = Field(default_factory=list)


PROMPT = """You are a strict auditor of answers given by a loan knowledge-base assistant to loan DSAs. Wrong figures,
wrong contacts or invented policy can cost a customer their loan, so judge strictly.

You get the SOURCES the assistant was given (passages numbered [S1], [S2], ...; possibly also app-calculated
sections such as a customer profile or eligibility), the QUESTION, the ANSWER, and possibly a REFERENCE answer
written by the credit team.

What the app guarantees (treat these as stated in the SOURCES):
- Every passage comes from the currently ACTIVE version of its document, i.e. the current policy / list. A passage
  marked "older edition" has a newer edition in the SOURCES. So "current", "active" or "latest" is supported for
  values from passages not marked older edition.
- A BANKER MATCH passage was filtered by lender, product and city, de-duplicated and ranked by the app (newest list
  first); its counts ("63 distinct bankers", "best 10 shown") are the app's.
- Sections without [S#] numbers (CUSTOMER PROFILE, ELIGIBILITY, RESULT, LENDER PROGRAM RULES, ...) are calculated by
  the app and are authoritative. Facts taken from them are supported and need no [S#] citation (citation_ok true).
- RM, relationship manager, banker, sales manager and contact person mean the same here.

Task 1 - claims. Split the ANSWER into its factual claims: one claim per fact (a figure, a name with a phone
number, an eligibility condition, a yes/no statement about a policy, a lender being offered or not). A table row
is one claim per fact in it. For each claim:
- cited: the passage numbers the answer cites for it, e.g. ["S1"] ([] if none).
- verdict:
  "supported"      the SOURCES state it, with the same figures, names, numbers and conditions.
  "partial"        the facts are stated, but a qualifier or condition is missing or broader than in the SOURCES
                   (e.g. "max tenure 60 months" where the SOURCES say "60 months for the surrogate program").
  "unsupported"    the SOURCES do not state it, or contradict it. Any wrong figure, digit, name, product or lender
                   makes the claim unsupported, not partial. Facts from general knowledge are unsupported.
  "not_checkable"  not a factual claim: greetings, formatting, "verify with the lender", "indicative, not a sanction",
                   a statement that something was not found, a request for details the app needs (city, loan type).
- citation_ok: false when the fact is stated in the SOURCES but not in the passage(s) it cites (or it cites none).
- evidence: the exact words from the SOURCES that support it (empty when unsupported).
- reason: one short sentence, required for partial and unsupported.
Check figures character by character: 1.5% is not 1.25%, 9604454809 is not 9604454890, ₹50 lakh is not ₹5 lakh. A
phone number is supported only when the SOURCES give it for that same person.

Task 2 - correctness (only when a REFERENCE is given; otherwise "n/a"). If the REFERENCE is a KEY VALUE, the
answer is "correct" when it gives that value (as written or an equal amount: 100 Lacs = ₹1 crore) without
contradicting it; extra supported detail, conditions, or a range or list that contains the value are fine. It is
"incorrect" when the value is missing or the answer says it is wrong or uncertain without a source saying so.
Otherwise compare the ANSWER with the full REFERENCE answer:
  "correct"    it gives every key fact of the REFERENCE, with nothing that contradicts it.
  "partial"    some key facts are right, others are missing (list them in missing), nothing contradicts it.
  "incorrect"  it contradicts the REFERENCE, gives a different key figure or contact, or misses the main point.
The wording may differ; the facts may not. Extra correct, supported detail is fine.

Return JSON only:
{{"claims": [{{"claim": "...", "cited": ["S1"], "verdict": "supported", "citation_ok": true, "evidence": "...",
"reason": ""}}], "correctness": "correct", "correctness_reason": "...", "missing": []}}

SOURCES:
{sources}

QUESTION:
{question}

ANSWER:
{answer}

REFERENCE:
{reference}"""


def sources_text(trace: dict) -> str:
    """What the model was given: the user message of the recorded prompt (passages, app sections, history)."""
    prompt = (trace or {}).get("prompt") or {}
    full = prompt.get("full") or ""
    if "--- HUMAN ---" in full:
        return full.split("--- HUMAN ---", 1)[1].strip()
    return prompt.get("context") or "(no sources were given)"


def _clean(j: Judgement) -> Judgement:
    for c in j.claims:
        c.verdict = c.verdict.strip().lower().replace(" ", "_").replace("-", "_")
        if c.verdict not in CLAIM_VERDICTS:
            c.verdict = "unsupported"
        c.cited = [str(x).strip("[]【】 ").upper() for x in c.cited if str(x).strip("[]【】 ")]
    j.correctness = j.correctness.strip().lower()
    if j.correctness not in CORRECTNESS:
        j.correctness = "n/a"
    return j


def summarize(j: Judgement, model: Optional[str] = None, how: str = "llm") -> dict:
    counts = {v: sum(1 for c in j.claims if c.verdict == v) for v in CLAIM_VERDICTS}
    checkable = counts["supported"] + counts["partial"] + counts["unsupported"]
    citation_errors = sum(1 for c in j.claims if c.verdict in ("supported", "partial") and not c.citation_ok)
    if counts["unsupported"] or j.correctness == "incorrect":
        verdict = "FAIL"
    elif counts["partial"] or citation_errors or j.correctness == "partial":
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {"verdict": verdict, "how": how,
            "faithfulness": round((counts["supported"] + 0.5 * counts["partial"]) / checkable, 3) if checkable else None,
            "claims": checkable, **counts, "citation_errors": citation_errors,
            "correctness": j.correctness, "correctness_reason": j.correctness_reason, "missing": j.missing,
            "problems": [{"claim": c.claim, "verdict": c.verdict, "cited": c.cited, "citation_ok": c.citation_ok,
                          "reason": c.reason} for c in j.claims
                         if c.verdict in ("partial", "unsupported") or (c.verdict == "supported" and not c.citation_ok)],
            "claims_detail": [c.model_dump() for c in j.claims],
            "model": model, "prompt_version": JUDGE_PROMPT_VERSION}


def judge(question: str, answer: str, trace: dict, reference: str = "", not_found: bool = False,
          key_value: bool = False, llm=None, label: str = "eval_judge") -> Optional[dict]:
    """Judge one answer. reference: the reviewer's answer, "(not found)" when the knowledge base has none, or ""
    (claims are still checked). key_value: the reference is only a value the answer must contain (a golden test
    without a reviewed answer). llm / label: another model and usage label (the live claim check in chat uses the
    fast model). Returns None when no judge model is configured. Rate-limit errors propagate so the golden run can
    stop; other errors are returned as {"verdict": "ERROR", ...}."""
    ref = (reference or "").strip()
    expects_nothing = ref.lower() == NOT_FOUND_REFERENCE
    refused = not_found or grounding.is_not_found(answer or "")
    if refused:
        if expects_nothing:
            return summarize(Judgement(correctness="correct", correctness_reason="Correctly said it was not found."), how="rule")
        if ref:
            return summarize(Judgement(correctness="incorrect", correctness_reason="False refusal: the reference has an "
                                       "answer, but the assistant said it was not found."), how="rule")
        return summarize(Judgement(), how="rule")
    if not (answer or "").strip():
        return None
    if (trace or {}).get("app_written"):        # a fixed reply of the app (e.g. asking for the city): no model claims
        return summarize(Judgement(correctness="n/a", correctness_reason="Written by the app, not the model."), how="app")
    from app.rag.llm import get_llm, is_rate_limit, model_name, structured
    from app.rag.metering import purpose
    llm = llm or get_llm("judge")
    if llm is None:
        return None
    prompt = PROMPT.format(sources=sources_text(trace), question=question, answer=answer,
                           reference="(none given)" if not ref else
                           "The knowledge base has NO answer to this question: the assistant should have said it was "
                           "not found." if expects_nothing else f"KEY VALUE: {ref}" if key_value else ref)
    try:
        with purpose(label):
            j = _clean(structured(llm, Judgement, prompt))
    except Exception as e:
        if is_rate_limit(e):
            raise
        log.warning("Judge failed: %s", e)
        return {"verdict": "ERROR", "error": f"{type(e).__name__}: {e}"[:300], "model": model_name(llm),
                "prompt_version": JUDGE_PROMPT_VERSION}
    if expects_nothing:
        j.correctness, j.correctness_reason = "incorrect", ("Answered although the knowledge base has no answer: "
                                                            "should have said it was not found.")
    return summarize(j, model_name(llm))
