"""Is a new chat message a follow-up to the conversation, or a new, independent loan question?

In one chat a partner asks about many customers, products and banks. Before a knowledge-base answer the message
is placed against the conversation (one fast-model call, only when there is a conversation). The customer
discussed is the one in the LAST answer's profile table (with its stored recommendation, when the chat has one:
app/services/recommendations.py); a follow-up refers to the latest exchange.

  update_profile       adds or changes a detail of that customer ("city pune", "CIBIL is 710 now"): the profile is
                       updated and the bankers matched again
  loan_type_change     the same customer for another product ("what about business loan?")
  what_if              a hypothetical change ("what if salary is 50,000?"): answered as a labelled scenario; the
                       customer's stored recommendation is not changed
  ask_lender           the bankers at particular lenders for that customer ("tata capital", "any Bajaj RM?")
  contact_followup     contacts of lenders from the previous answer ("give me their contact numbers")
  alternative_lenders  more lenders, excluding every lender already shown for this customer ("other than above")
  explain_lender       why a lender is or is not in the list ("why not HDFC?"): from the rules engine's result and
                       the RM lists, never a guessed reason
  new_case             start over ("start a new case"): nothing of the previous customer is carried
  clarify              two or more different meanings are plausible: one short question back
  followup             any other follow-up (retrieval.understand resolves it into a standalone question)
  new                  a new, independent question: the conversation is not used

A message that itself describes a customer (profile.detect) starts a new profile and is not routed here.
Without a conversation or a fast model nothing is routed and the message is answered as before.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.rag import entities, profile as profiles

log = logging.getLogger(__name__)
NEEDS_CUSTOMER = ("update_profile", "loan_type_change", "what_if", "ask_lender", "contact_followup",
                  "alternative_lenders", "explain_lender")
RELATIONS = NEEDS_CUSTOMER + ("new_case", "clarify", "followup", "new")
ANSWER_CHARS = 900                  # how much of the last answer the router sees
# Facts that are not "which bankers for this customer": a message asking for one is its own question.
NOT_BANKERS = {"code", "pincode", "payout", "roi", "processing_fee", "documents", "max_amount", "min_amount", "ltv",
               "tenure", "age", "vintage"}
# Lender-specific facts: asked with no lender or product anywhere, the question cannot be answered as meant.
LENDER_FACTS = {"roi", "processing_fee", "code", "payout", "max_amount", "min_amount", "ltv", "foir", "min_income",
                "tenure", "documents"}
GENERIC = set("""what whats is are the a an of for to in on at me us tell give show please current currently latest
how much many which do does can i we you it its this that there any rate rates roi interest fee fees charge charges
processing code codes payout payouts grid max maximum min minimum limit loan loans amount ltv foir income salary tenure
document documents required needed list eligibility criteria policy""".split())
ASK_WHICH = "Which lender and loan product do you mean? For example: \"HDFC Bank home loan ROI\"."
NEW_CASE_REPLY = ("Okay, starting a new case. Nothing from the previous customer will be used.\n\n"
                  "Share the new customer's details: loan type, city, employment, monthly income, loan amount, CIBIL "
                  "score and existing EMIs.")

_LABELS = {label.lower(): key for key, label in profiles.CORE + profiles.EXTRA}
_LABELS["property location"] = "property_location"
_ROW = re.compile(r"^\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|$")
_BANKER_ROW = re.compile(r"^\|\s*\d+\s*\|\s*([^|]+?)\s*\|")


@dataclass
class Active:
    """The customer of the last answer."""
    question: str                   # the partner's message the profile answer replied to
    fields: Dict[str, str]
    other: List[str] = field(default_factory=list)
    shown: List[str] = field(default_factory=list)      # lenders in that answer's bankers table
    also: List[str] = field(default_factory=list)       # its "Also check" lenders (names only, no RM shown)
    seen: List[str] = field(default_factory=list)       # every lender shown for this customer so far
    record_id: Optional[str] = None                     # its stored recommendation
    hypothetical: bool = False
    scenario: str = ""


@dataclass
class Route:
    relation: str
    reason: str = ""
    lenders: List[str] = field(default_factory=list)
    active: Optional[Active] = None
    profile: Optional["profiles.Profile"] = None     # the customer to answer with (updated / scenario / filtered)
    direct: str = ""                                  # the app's own reply (new case, clarification)
    options: List[str] = field(default_factory=list)  # clarification choices

    def trace(self) -> dict:
        return {"relation": self.relation, "reason": self.reason, "lenders": self.lenders,
                "earlier_profile": self.active.fields if self.active else None,
                "earlier_record": self.active.record_id if self.active else None,
                "excluded": self.profile.exclude if self.profile else [], "options": self.options}


class _Decision(BaseModel):
    relation: str = Field(description="one of the listed relations")
    lenders: List[str] = Field(default_factory=list)
    clarification: str = ""
    options: List[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("lenders", "options", mode="before")
    @classmethod
    def _listify(cls, v):
        if v in (None, ""):
            return []
        return [s.strip() for s in re.split(r"[,;\n]", v) if s.strip()] if isinstance(v, str) else v


# ------------------------------------------------------------------ reading the previous answer
def read_table(answer: str) -> Optional[tuple]:
    """(fields, other) from the "Customer Profile" table of an app answer, or None. "Not provided" is left out."""
    i = (answer or "").find("Customer Profile")
    if i < 0:
        return None
    fields, other = {}, []
    for line in answer[i:].splitlines()[1:]:
        line = line.strip()
        m = _ROW.match(line)
        if not m:
            if fields or (line and not line.startswith("#")):
                break                                   # the table ended (or something else came first)
            continue
        label, value = m.group(1).strip("* ").lower(), m.group(2).strip()
        if not value or value == profiles.NOT_PROVIDED or set(value) <= set("-: "):
            continue
        if label == "other":
            other = [s.strip() for s in value.split(";") if s.strip()]
        elif label in _LABELS:
            fields[_LABELS[label]] = value
    return (fields, other) if fields else None


def read_lenders(answer: str) -> tuple:
    """(lenders in the bankers table, "Also check" lenders) of a profile answer."""
    shown, also, section = [], [], ""
    for line in (answer or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            section = s.lower()
            continue
        if "bankers" in section and (m := _BANKER_ROW.match(s)):
            shown.append(m.group(1).strip())
        elif "also check" in section and s.startswith(("-", "*", "•")):
            also.append(s.lstrip("-*• ").strip())
    return list(dict.fromkeys(shown)), list(dict.fromkeys(also))


def active(history: Optional[list], record: Optional[dict] = None) -> Optional[Active]:
    """The customer profile of the last answer, if it showed one. A follow-up refers to the latest exchange: after
    "Kotak code for medical equipment?", "and for HDFC?" asks HDFC's code, not HDFC bankers for an earlier
    customer. A "not found" answer is skipped (that turn added nothing). record: the conversation's latest stored
    recommendation; used only when it is the one that answer showed (same customer details)."""
    from app.rag.grounding import is_not_found
    msgs = list(history or [])
    for i in range(len(msgs) - 1, -1, -1):
        text = str(msgs[i].get("text", ""))
        if msgs[i].get("role") == "user" or is_not_found(text):
            continue
        got = read_table(text)
        if not got:
            return None
        asked = next((str(m.get("text", "")) for m in reversed(msgs[:i]) if m.get("role") == "user"), "")
        shown, also = read_lenders(text)
        act = Active(question=asked, fields=got[0], other=got[1], shown=shown, also=also, seen=list(shown),
                     hypothetical="hypothetical scenario" in text.lower())
        if record and _same(record.get("profile", {}).get("fields", {}), act.fields):
            act.record_id, act.hypothetical = record["id"], record["hypothetical"]
            act.scenario = record.get("profile", {}).get("scenario", "")
            act.seen = list(dict.fromkeys(record.get("seen", []) + shown))
            act.question = record.get("question") or asked
        return act
    return None


def _same(a: Dict[str, str], b: Dict[str, str]) -> bool:
    norm = lambda d: {k: " ".join(str(v).lower().split()) for k, v in d.items()}
    return bool(a) and norm(a) == norm(b)


def _profile_lines(a: Active) -> str:
    labels = dict(profiles.CORE + profiles.EXTRA, property_location="Property Location")
    lines = [f"- {labels.get(k, k)}: {v}" for k, v in a.fields.items()]
    lines += [f"- Other: {'; '.join(a.other)}"] if a.other else []
    lines += [f"- Lenders shown in the last answer: {', '.join(a.shown)}"] if a.shown else []
    lines += [f"- 'Also check' lenders in the last answer (names only): {', '.join(a.also)}"] if a.also else []
    return "\n".join(lines)


# ------------------------------------------------------------------ the decision
def _decide(llm, question: str, history: list, act: Optional[Active]) -> _Decision:
    from app.rag.llm import structured
    from app.rag.metering import purpose
    last_user = next((str(m.get("text", "")) for m in reversed(history) if m.get("role") == "user"), "")
    last_answer = next((str(m.get("text", "")) for m in reversed(history) if m.get("role") != "user"), "")
    customer = """- update_profile: adds or changes a detail of THAT customer (city / location, income, CIBIL, EMIs, loan
  amount, employment, property value, ...), e.g. "city pune", "cibil is 710 now", "loan amount 40 lakh".
- loan_type_change: the same customer for another loan product, e.g. "what about business loan?", "make it LAP".
- what_if: a hypothetical change to see its effect, e.g. "what if salary is 50000?", "if CIBIL were 700?".
- ask_lender: the bankers / RMs at particular lenders for THAT customer, e.g. "tata capital", "what about HDFC?",
  "any Bajaj RM?". Put the lender names in lenders, as written.
- contact_followup: contact numbers of lenders from the last answer, e.g. "give me their contact numbers",
  "numbers for the also-check banks". Put lender names in lenders only if the message itself names them.
- alternative_lenders: more / other lenders than those already shown, e.g. "other than above bank", "any other
  option", "show me other banks".
- explain_lender: why a lender is or is not in the list, or whether it suits this customer, e.g. "why not HDFC?",
  "why did you skip ICICI?". Put the lender names in lenders.
Not about that customer: a question about a code, payout, rate, pincode, document or policy figure, or about
another customer; that is new (or followup if it cannot be understood on its own).
""" if act else ""
    profile_block = f"CUSTOMER IN THE LAST ANSWER:\n{_profile_lines(act)}\n\n" if act else ""
    with purpose("followup_route"):
        return structured(llm, _Decision, f"""A loan partner chats with an assistant about lenders, codes, RMs, payouts,
policies and customer eligibility. In one chat they ask about many customers, products and banks. Decide how the
NEW MESSAGE relates to the conversation.

Relations:
{customer}- new_case: the partner wants to start over with a new customer, e.g. "start a new case", "new customer".
- clarify: ONLY when the message has two or more clearly different meanings and the conversation does not
  settle which one (e.g. "what is the ROI?" with no lender or product anywhere in the conversation). Write one
  short question in clarification and up to 3 options: each a complete short question the partner could send
  (under 40 characters) that names a lender and / or loan product, e.g. "HDFC Bank home loan ROI".
- followup: any other follow-up that needs the conversation to be understood ("what about its LAP?", "why?").
- new: a new, independent question that is fully understandable on its own (a different customer, bank, product
  or topic), even if it is short.

A question about a lender-specific fact (ROI, fee, code, payout, limit, policy) that names no lender or product,
when the conversation names none either, is clarify. Otherwise prefer resolving from the conversation.

{profile_block}LAST QUESTION: {last_user[:500]}
LAST ANSWER (start): {last_answer[:ANSWER_CHARS]}

NEW MESSAGE: {question}

Return JSON only: {{"relation": "...", "lenders": [], "clarification": "", "options": [], "reason": "a few words"}}""")


def _known_banks() -> List[str]:
    from app.rag.retrieval import _known
    return _known()["banks"]


def _customer(act: Active, question: str, fields: Optional[Dict[str, str]] = None, other=None, **kw) -> "profiles.Profile":
    p = profiles.Profile(question=f"{act.question}\n\nFollow-up from the partner: {question}",
                         fields=dict(fields if fields is not None else act.fields),
                         other=list(other if other is not None else act.other), **kw)
    p.calc = profiles.compute(p.fields)
    return p


def _changed(act: Active, question: str) -> Optional[tuple]:
    """(fields, other, the new details) with the message's details on top (a changed detail replaces the old)."""
    new = profiles.extract(question)
    if not new.fields and not new.other:
        return None
    return {**act.fields, **new.fields}, list(dict.fromkeys(act.other + new.other))[:8], new.fields


def _own_question(question: str, act: Active) -> bool:
    """The message asks a fact other than bankers (a code, payout, rate, ...) or names another product."""
    if NOT_BANKERS & set(entities.find_metrics(question)):
        return True
    products = set(entities.find_products(question))
    lt = act.fields.get("loan_type", "")
    return bool(products) and not products & (set(entities.find_products(lt)) or {lt})


def _named(question: str, decided: List[str], history: list, extra: List[str] = ()) -> List[str]:
    """Lenders the partner named (in this message or earlier in the chat): never one the model made up."""
    known = _known_banks()
    convo = question + " " + " ".join(str(m.get("text", "")) for m in history) + " " + " ".join(extra)
    in_convo = set(entities.find_banks(convo, known))
    return entities.find_banks(question, known) or \
        [b for b in entities.find_banks(" ".join(decided), known) if b in in_convo]


def needs_lender(question: str, history: Optional[list]) -> bool:
    """A short question for a lender-specific fact ("what is the ROI?") with no lender or product in it or in the
    chat since the last new case."""
    words = re.findall(r"[a-z]+", question.lower())
    if len(words) > 8 or not LENDER_FACTS & set(entities.find_metrics(question)):
        return False
    if any(w not in GENERIC for w in words):        # "Orix India Finance code": a name the bank list lacks
        return False
    texts = [question]
    for m in reversed(history or []):
        t = str(m.get("text", ""))
        if t.startswith(NEW_CASE_REPLY[:30]):
            break
        texts.append(t)
    known = _known_banks()
    return not any(entities.find_banks(t, known) or entities.find_products(t) for t in texts)


def _options(options: List[str]) -> List[str]:
    """Clarification chips: short, complete questions that name a lender or product (others are dropped)."""
    known = _known_banks()
    return [o.strip() for o in options
            if o.strip() and len(o.strip()) <= 48 and (entities.find_banks(o, known) or entities.find_products(o))][:3]


def _fallback(route: Route, why: str) -> Route:
    route.relation, route.reason, route.profile = "followup", why, None
    return route


def resolve(question: str, history: Optional[list], user_id: Optional[int] = None,
            conversation_id: Optional[str] = None) -> Optional[Route]:
    """How the message relates to the conversation (see the module docstring); None when there is nothing to
    decide (no conversation, no fast model) or the decision failed (then the message is answered as before)."""
    from app.core.config import get_settings
    from app.rag.llm import get_llm
    history = [m for m in (history or []) if str(m.get("text", "")).strip()]
    if not history or not get_settings().chat_followup_router:
        return None
    llm = get_llm("fast")
    if llm is None:
        return None
    from app.services import recommendations
    act = active(history, recommendations.latest(user_id, conversation_id))
    try:
        d = _decide(llm, question, history, act)
    except Exception as e:
        log.warning("Follow-up routing failed: %s", e)
        return None
    rel = d.relation.strip().lower()
    rel = rel if rel in RELATIONS else "followup"
    route = Route(relation=rel, reason=d.reason[:200], active=act)
    try:
        return _apply(route, d, question, history, act)
    except Exception:
        log.exception("Could not apply the follow-up (%s)", rel)
        return _fallback(route, f"{rel} could not be applied")


def _apply(route: Route, d: _Decision, question: str, history: list, act: Optional[Active]) -> Route:
    rel = route.relation
    if rel == "new_case":
        route.direct = NEW_CASE_REPLY
        return route
    if rel == "clarify":
        if not d.clarification.strip():
            return _fallback(route, "clarification without a question")
        route.direct = d.clarification.strip()
        route.options = _options(d.options)
        return route
    if rel in NEEDS_CUSTOMER and act is None:
        return _fallback(route, f"{rel} without a customer in the last answer")
    if rel in ("ask_lender", "explain_lender", "contact_followup") and _own_question(question, act):
        # "Kotak code for medical equipment loan": a lender, but a fact about another product, not this customer
        route.relation, route.reason = "new", "names a lender and asks its own fact / product"
        return route
    keep = dict(hypothetical=act.hypothetical, scenario=act.scenario, base_id=act.record_id,
                seen_before=list(act.seen)) if act else {}
    if rel in ("followup", "new") and needs_lender(question, history):
        route.relation, route.reason = "clarify", "a lender fact with no lender or product in the chat"
        route.direct = d.clarification.strip() or ASK_WHICH
        route.options = _options(d.options)
        return route
    if rel in ("update_profile", "loan_type_change", "what_if"):
        got = _changed(act, question)
        if got is None:
            return _fallback(route, "no customer detail found in the message")
        fields, other, new = got
        if rel == "loan_type_change" and (not new.get("loan_type") or new["loan_type"] == act.fields.get("loan_type")):
            return _fallback(route, "no new loan type found in the message")
        keep["seen_before"] = []                   # changed details: a new search, nothing excluded
        if rel == "what_if":
            keep["hypothetical"] = True
            keep["scenario"] = "; ".join(f"{profiles.label(k)} {act.fields.get(k, profiles.NOT_PROVIDED)} → {v}"
                                         for k, v in new.items())
        route.profile = _customer(act, question, fields, other, relation=rel, **keep)
        return route
    if rel == "alternative_lenders":
        route.profile = _customer(act, question, relation=rel, exclude=list(act.seen), **keep)
        return route
    if rel in ("ask_lender", "explain_lender", "contact_followup"):
        if rel == "contact_followup":
            # "their numbers": the lenders the message names, else those the last answer listed without an RM
            # (its bankers table already shows a number for each of its lenders)
            named = entities.find_banks(question, _known_banks()) or act.also or act.shown
        else:
            named = _named(question, d.lenders, history, act.shown + act.also)
        if not named:
            return _fallback(route, f"lender not recognised ({', '.join(d.lenders) or 'none named'})")
        route.lenders = named
        route.profile = _customer(act, question, relation=rel, lenders=named,
                                  explain=named if rel == "explain_lender" else [], shown_before=list(act.shown), **keep)
        return route
    return route
