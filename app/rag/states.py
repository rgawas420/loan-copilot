"""The state of every chat answer: machine-readable, sent to the UI with the answer ('state' event) and kept in
the trace. The UI shows a natural message and the state's actions; partners never see the enum names.

State                       When                                                         Retry   Suggestions
ANSWER_VERIFIED             answered; every check passed (figures, contacts, citations,  no      customer follow-ups
                            and the live claim check when it ran)
ANSWER_PARTIALLY_VERIFIED   answered, but a figure / contact / claim could not be        no      customer follow-ups
                            confirmed (the confidence badge lists what to check)
ALTERNATIVE_RESULTS_FOUND   "other than above": more lenders found, already-shown ones    no      more, change details
                            excluded
NO_ADDITIONAL_MATCHES       "other than above": the RM lists were read completely and     no      change city / loan type
                            no other lender has an RM for this loan type and city               / amount (if rules)
CLARIFICATION_REQUIRED      the message had two or more plausible meanings               no      the options
MORE_INFORMATION_REQUIRED   a customer detail needed to continue is missing (or a new     no      the missing fields
                            case was started)
KNOWLEDGE_BASE_GAP          every search ran and no passage holds the answer             no      none
RETRIEVAL_FAILURE           the vector search / RM lists failed even after a retry, so   yes     none
                            "not found" could not be established
SOURCE_CONFLICT             the sources disagree (e.g. two current numbers for one RM)   no      customer follow-ups
STALE_SOURCE                the answer cites an older edition of a document              no      customer follow-ups
RATE_LIMITED                the AI provider's rate limit                                 yes     none
TEMPORARY_SERVICE_FAILURE   a timeout / connection / database error                      yes     none
INTERNAL_ERROR              any other error                                              yes     none
"""
import sqlite3
from typing import List, Optional

ANSWER_VERIFIED = "ANSWER_VERIFIED"
ANSWER_PARTIALLY_VERIFIED = "ANSWER_PARTIALLY_VERIFIED"
ALTERNATIVE_RESULTS_FOUND = "ALTERNATIVE_RESULTS_FOUND"
NO_ADDITIONAL_MATCHES = "NO_ADDITIONAL_MATCHES"
CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
MORE_INFORMATION_REQUIRED = "MORE_INFORMATION_REQUIRED"
KNOWLEDGE_BASE_GAP = "KNOWLEDGE_BASE_GAP"
RETRIEVAL_FAILURE = "RETRIEVAL_FAILURE"
SOURCE_CONFLICT = "SOURCE_CONFLICT"
STALE_SOURCE = "STALE_SOURCE"
RATE_LIMITED = "RATE_LIMITED"
TEMPORARY_SERVICE_FAILURE = "TEMPORARY_SERVICE_FAILURE"
INTERNAL_ERROR = "INTERNAL_ERROR"

RETRY = {RETRIEVAL_FAILURE, RATE_LIMITED, TEMPORARY_SERVICE_FAILURE, INTERNAL_ERROR}
NOTICES = {     # shown under the answer (the answer text itself explains the rest)
    SOURCE_CONFLICT: "The sources disagree on a detail; the answer shows both. Confirm with the RM.",
    STALE_SOURCE: "Part of this answer comes from an older edition of a document. Confirm it is still current.",
    RETRIEVAL_FAILURE: "The document search did not complete. Try again in a minute.",
}
RETRIEVAL_FAILURE_REPLY = ("I couldn't complete the document search just now (the search service did not respond, "
                           "even after a retry), so I can't tell whether this information is in the knowledge base. "
                           "Please try again in a minute.")
RM_LISTS_FAILURE_REPLY = ("I couldn't read the RM lists just now, so I can't tell which lenders have RMs for this "
                          "customer. Please try again in a minute.")
_TEMPORARY = ("timeout", "timed out", "connection", "temporarily", "unavailable", "503", "502", "504",
              "database is locked", "server disconnected")


def action(label: str, text: str, send: bool = True) -> dict:
    """A suggestion chip: send=True sends text as the partner's next message; False puts it in the composer to
    complete ("Change the city to ...")."""
    return {"label": label, "text": text, "send": send}


def for_error(e: BaseException) -> str:
    """The state of a failed answer: never "not found"."""
    from app.rag.llm import is_rate_limit
    if is_rate_limit(e):
        return RATE_LIMITED
    name, text = type(e).__name__.lower(), str(e).lower()
    if isinstance(e, (TimeoutError, ConnectionError, sqlite3.OperationalError)) or \
            any(w in name for w in ("timeout", "connection", "unavailable")) or any(w in text for w in _TEMPORARY):
        return TEMPORARY_SERVICE_FAILURE
    return INTERNAL_ERROR


ERROR_MESSAGES = {
    TEMPORARY_SERVICE_FAILURE: "A service the answer depends on did not respond (a timeout or connection problem). "
                               "This is not a missing document. Try again in a moment.",
    INTERNAL_ERROR: "Something went wrong while preparing the answer. Try again; if it keeps failing, tell the admin.",
}


def customer_actions(prof, has_rules: bool) -> List[dict]:
    acts = [action("Other lenders", "Other than the above lenders"),
            action("What if…", "What if the monthly income is ", send=False),
            action("Change loan type", "Change the loan type to ", send=False)]
    if prof.explain or prof.lenders:
        acts.insert(1, action("All lenders again", "Show all lenders for this customer"))
    return acts


def no_more_actions(has_rules: bool) -> List[dict]:
    acts = [action("Change city", "Change the city to ", send=False),
            action("Change loan type", "Change the loan type to ", send=False)]
    if has_rules:                       # the amount only changes eligibility when lender rules are loaded
        acts.append(action("Change loan amount", "Change the loan amount to ", send=False))
    acts.append(action("New case", "Start a new case"))
    return acts


def missing_actions(missing: List[str]) -> List[dict]:
    acts = []
    for m in missing:
        if m.startswith("the city"):
            acts.append(action("Add city", "City: ", send=False))
        elif m.startswith("the loan type"):
            acts.append(action("Add loan type", "Loan type: ", send=False))
    return acts


def decide(prep, post: dict, conf: Optional[dict]) -> dict:
    """{state, retry, notice, actions} for a finished answer."""
    from app.rag import lender_eligibility
    prof, route = prep.profile, getattr(prep, "route_obj", None)
    has_rules = bool(prof and lender_eligibility.any_rules(prof.loan_type))
    state, acts = prep.state, []
    if state is None:
        if post.get("not_found"):
            state = KNOWLEDGE_BASE_GAP
        elif prof and prof.exclude and prep.bankers is not None and prep.bankers.top:
            state = ALTERNATIVE_RESULTS_FOUND
        elif prep.bankers is not None and any(n.startswith("verify:") for b in prep.bankers.top for n in b.notes):
            state = SOURCE_CONFLICT             # a shown banker has two current numbers (marked "verify")
        elif any(r.get("older_edition") for r in prep.refs if r.get("ref") in _cited(post)):
            state = STALE_SOURCE
        elif post.get("unsupported_figures") or post.get("contact_mismatches") or post.get("invalid_citations") \
                or (conf and conf.get("level") != "high"):
            state = ANSWER_PARTIALLY_VERIFIED
        else:
            state = ANSWER_VERIFIED
    if state == NO_ADDITIONAL_MATCHES:
        acts = no_more_actions(has_rules)
    elif state == MORE_INFORMATION_REQUIRED and prof is not None:
        acts = missing_actions(prof.missing_for_bankers())
    elif state == CLARIFICATION_REQUIRED and route is not None:
        acts = [action(o[:40], o) for o in route.options]
    elif prof is not None and prep.bankers is not None and state not in RETRY:
        acts = customer_actions(prof, has_rules)
    return {"state": state, "retry": state in RETRY, "notice": NOTICES.get(state, ""), "actions": acts}


def _cited(post: dict) -> set:
    return {s.get("ref") for s in post.get("sources", []) if not s.get("uncited")}
