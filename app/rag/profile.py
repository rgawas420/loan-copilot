"""Customer -> banker recommendations: "Salaried, ₹1,45,000 a month, wants a ₹98 lakh home loan for a flat in
Pune, CIBIL 845, no EMIs. Give banker list in Pune."

Extract -> Retrieve -> Calculate -> Match -> Present; never guess:

1. Extract: one fast LLM call turns the message into a profile (only what the user wrote; never a calculation).
2. Retrieve: the RM lists are read for that loan type in that city (app/rag/bankers.py), and the knowledge base
   is searched with a focused query for that product's policy, never with the whole paragraph.
3. Calculate: the eligibility (EMI capacity, EMI on the requested loan, indicative maximum, LTV) is computed
   here. The customer's own rate / tenure are used when given, otherwise the app's configured defaults, and
   every assumption is labelled as one. An existing EMI that was not given is not assumed to be zero.
4. Match: bankers are filtered, de-duplicated and ranked by the app (bankers.py), at most 10.
5. Present: the app builds the profile, eligibility and banker tables; the model copies them and writes only
   the one-line summary and the important eligibility point (PROFILE_RULES).
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

from app.rag import eligibility
from app.rag.rules import rupees

log = logging.getLogger(__name__)
MAX_TOKENS = 2500     # room for the tables and the model's reasoning; the answer itself stays short (PROFILE_RULES)
NOT_PROVIDED = "Not provided"

# Asks which lenders / bankers to go to.
_ASK_WHO = re.compile(r"\b(bankers?|rms?|relationship managers?|lenders?|banks?|nbfcs?|hfcs?|financiers?|financers?)\b", re.I)
_ASK_LIST = re.compile(r"\b(list|which|suggest\w*|recommend\w*|approach|options?|best|suitable|give|share|send|where|"
                       r"who|contacts?)\b", re.I)
# Describes a customer: at least two of these.
_SIGNALS = [
    re.compile(r"\b(salar\w*|income|in[- ]?hand|ctc|lpa|earn\w*|turnover|itr|net profit)\b", re.I),
    re.compile(r"\b(loan (?:of|amount|for|requirement)|looking for|requires?|required|needs?|wants?)\b.{0,40}"
               r"(₹|rs\.?|inr|\d)|(₹|rs\.?|inr)\s*\d[\d,.]*\s*(lakhs?|lacs?|crores?|cr|l)?\b.{0,30}\bloan\b", re.I),
    re.compile(r"\b(cibil|credit score|bureau score|credit history)\b", re.I),
    re.compile(r"\b(existing emis?|emis?|obligations?|outstanding dues|dues|overdue|dpd)\b", re.I),
    re.compile(r"\b(salaried|self[- ]employed|business ?(?:man|owner)|proprietor|professional|doctor|ca\b)", re.I),
]


def detect(question: str) -> bool:
    """A question that describes a customer and asks which lenders / bankers to approach."""
    q = question or ""
    if len(q) < 40 or not (_ASK_WHO.search(q) and _ASK_LIST.search(q)):
        return False
    return sum(1 for s in _SIGNALS if s.search(q)) >= 2


class ExtractedProfile(BaseModel):
    customer_name: Optional[str] = None
    employment: Optional[str] = None
    monthly_income: Optional[str] = None
    annual_income: Optional[str] = None
    loan_amount: Optional[str] = None
    loan_type: Optional[str] = Field(default=None, description="One of the listed loan types")
    property_location: Optional[str] = None
    job_location: Optional[str] = None
    cibil: Optional[str] = None
    existing_emi: Optional[str] = None
    existing_loans: Optional[str] = None
    age: Optional[str] = None
    property_value: Optional[str] = None
    agreement_value: Optional[str] = None
    down_payment: Optional[str] = None
    tenure: Optional[str] = None
    interest_rate: Optional[str] = None
    co_applicant_income: Optional[str] = None
    business_details: Optional[str] = None
    outstanding_dues: Optional[str] = None
    credit_history: Optional[str] = None
    other: List[str] = Field(default_factory=list)

    @field_validator("other", mode="before")
    @classmethod
    def _listify(cls, v):                      # small models sometimes return one string
        if v in (None, ""):
            return []
        return [s.strip() for s in re.split(r"[;\n]", v) if s.strip()] if isinstance(v, str) else v

    @field_validator("*", mode="before")
    @classmethod
    def _text(cls, v):
        return str(v) if isinstance(v, (int, float)) else v


_SHAPE = "{" + ", ".join(f'"{k}": null' for k in ExtractedProfile.model_fields if k != "other") + ', "other": []}'

# Always shown (Not provided when missing), then any of the others the user gave.
CORE = [("loan_type", "Loan Type"), ("employment", "Employment"), ("monthly_income", "Monthly Income"),
        ("loan_amount", "Required Loan"), ("cibil", "CIBIL"), ("existing_emi", "Existing EMI"),
        ("location", "Property Location")]
EXTRA = [("customer_name", "Customer Name"), ("annual_income", "Annual Income"), ("job_location", "Job Location"),
         ("existing_loans", "Existing Loans"), ("age", "Age"), ("property_value", "Property Value"),
         ("agreement_value", "Agreement Value"), ("down_payment", "Down Payment"), ("tenure", "Tenure"),
         ("interest_rate", "Interest Rate / ROI"), ("co_applicant_income", "Co-applicant Income"),
         ("business_details", "Business Details"), ("outstanding_dues", "Outstanding Dues"),
         ("credit_history", "Credit History")]


def label(key: str) -> str:
    """The profile table's label for a field."""
    return dict(CORE + EXTRA, property_location="Property Location").get(key, key.replace("_", " ").title())


def _cell(s: str) -> str:
    return str(s).replace("|", "/").replace("\n", " ").strip()


@dataclass
class Profile:
    question: str
    fields: Dict[str, str]
    other: List[str] = field(default_factory=list)
    calc: Dict[str, object] = field(default_factory=dict)
    lenders: List[str] = field(default_factory=list)     # a follow-up asked about these lenders only
    # set by a follow-up (app/rag/followup.py):
    relation: str = ""                                    # how the message related to the conversation
    exclude: List[str] = field(default_factory=list)     # lenders already shown for this customer
    hypothetical: bool = False                            # a what-if scenario, not the customer's recorded details
    scenario: str = ""                                    # what the scenario changed
    explain: List[str] = field(default_factory=list)     # "why not <lender>?"
    shown_before: List[str] = field(default_factory=list)  # lenders of the previous answer's bankers table
    base_id: Optional[str] = None                         # the stored recommendation this one continues
    seen_before: List[str] = field(default_factory=list)  # lenders shown for this customer before this answer
    eligibility: List[dict] = field(default_factory=list)  # per-lender outcomes (app/rag/lender_eligibility.py)

    @property
    def loan_type(self) -> str:
        return self.fields.get("loan_type") or ""

    @property
    def city(self) -> str:
        """The most relevant location the user gave: the property's, else the job's."""
        return self.fields.get("property_location") or self.fields.get("job_location") or ""

    @property
    def search(self) -> str:
        """The focused knowledge-base query for the product's policy (bankers come from bankers.py)."""
        return " ".join(x for x in [*self.lenders, self.loan_type, self.city, "eligibility policy"] if x)

    def profile_table(self) -> str:
        vals = {**self.fields, "location": self.fields.get("property_location") or ""}
        rows = [(label, vals.get(k) or NOT_PROVIDED) for k, label in CORE]
        rows += [(label, self.fields[k]) for k, label in EXTRA if self.fields.get(k)]
        rows += [("Other", "; ".join(self.other))] if self.other else []
        return "\n".join(["| Field | Details |", "|---|---|"] + [f"| {a} | {_cell(b)} |" for a, b in rows])

    def missing_for_bankers(self) -> List[str]:
        """What must be known before bankers can be matched (they are listed per loan type and city)."""
        out = []
        if not self.loan_type:
            out.append("the loan type (for example Home Loan, LAP, Personal Loan, Business Loan)")
        if not self.city:
            out.append("the city where the property is or the loan is needed")
        return out

    def ask_answer(self) -> str:
        """The app's own reply when the bankers cannot be matched yet: what was understood, the eligibility it
        could calculate, and the missing details. Nothing is guessed and no model is asked."""
        calc = self.calc.get("table") or f"Eligibility was not calculated: {self.calc.get('reason', '')}."
        missing = "\n".join(f"- {m}" for m in self.missing_for_bankers())
        return (f"I can list the bankers to approach once I know a little more.\n\n### Customer Profile\n"
                f"{self.profile_table()}\n\n### Eligibility Snapshot\n{calc}\n\n### Needed to list bankers\n{missing}\n\n"
                "Reply with these details and I'll match the bankers from the latest RM lists.")

    def ranking_basis(self) -> str:
        from app.rag.policies import programs_for
        if self.loan_type and programs_for(self.loan_type):
            return "ranked by RM availability; check each lender's program status in a full recommendation"
        return ("ranked by RM availability in the knowledge base (newest list first), not by each lender's "
                "eligibility rules, which are not loaded for this loan type")

    def why(self, match=None) -> str:
        """"Why not <lender>?": the rules engine's outcome and where the lender stands in the RM lists. Only
        facts the app established; nothing guessed."""
        from app.rag import lender_eligibility
        out = []
        by = {r["lender"]: r for r in self.eligibility}
        top = [b.lender for b in match.top] if match is not None else []
        for name in self.explain:
            r = by.get(name) or lender_eligibility.evaluate_lender(self.fields, name)
            line = [lender_eligibility.explain(r, self.fields)]
            k = _lender_key(name)
            before = [i for i, l in enumerate(self.shown_before, 1) if _lender_key(l) == k]
            mine = [b for b in (match.top if match is not None else []) if _lender_key(b.lender) == k]
            lt, city = self.loan_type or "loan", self.city or "this city"
            if before:
                line.append(f"{name} is #{before[0]} in the bankers list above.")
            elif mine:
                n = match.total if len(self.explain) == 1 else len(mine)     # the table shows at most 10
                line.append(f"{name} has {n} {lt} RM(s) for {city} in the RM lists (below); it was not in the "
                            f"top 10 because that list is {self.ranking_basis()}.")
            elif match is not None and not match.error:
                line.append(f"No {lt} RM at {name} for {city} appears in the current RM lists.")
            out.append(" ".join(line))
        return "\n\n".join(out)

    def sections(self, match=None) -> List[Tuple[str, str]]:
        out = [("CUSTOMER PROFILE TABLE (built by the app; copy exactly)", self.profile_table()),
               ("ELIGIBILITY SNAPSHOT TABLE (calculated by the app; copy exactly, do not recalculate)",
                str(self.calc.get("table") or "Eligibility was not calculated: " + str(self.calc.get("reason", "")))),
               ("ELIGIBILITY RESULT (calculated by the app)", str(self.calc.get("verdict") or ""))]
        if self.hypothetical:
            out.insert(0, ("HYPOTHETICAL SCENARIO (start the answer with exactly: \"**Hypothetical scenario:** "
                           f"{self.scenario or 'changed details'}. The customer's recorded details are unchanged.\")",
                           self.scenario or "changed details"))
        if self.exclude:
            out.append(("ALREADY SHOWN for this customer (excluded from this search; say the list below has "
                        "additional lenders beyond these)", ", ".join(self.exclude)))
        if match is not None:
            where = " ".join(x for x in [self.city] if x) + (f" at {', '.join(self.lenders)}" if self.lenders else "")
            out.append((f"BANKERS TABLE for {where or 'the location'} (chosen and ranked by the app from the knowledge "
                        "base; copy exactly)", match.table()))
            out.append(("RANKING BASIS (state it in one short line under the bankers table)", self.ranking_basis()))
            out.append(("ALSO CHECK (more lenders in the knowledge base)", ", ".join(match.also) or "(none)"))
        from app.rag import lender_eligibility
        elig = lender_eligibility.table(self.eligibility)
        if elig:
            out.append(("LENDER ELIGIBILITY TABLE (rules engine; copy exactly)", elig))
        if self.explain:
            out.append((f"WHY {', '.join(self.explain).upper()} (established by the app; copy exactly)", self.why(match)))
        return out

    def trace(self) -> dict:
        return {"fields": self.fields, "other": self.other, "location": self.city, "search": self.search,
                "lenders": self.lenders, "relation": self.relation, "exclude": self.exclude,
                "hypothetical": self.hypothetical, "scenario": self.scenario, "explain": self.explain,
                "base_id": self.base_id, "eligibility": self.eligibility,
                "eligibility": {k: v for k, v in self.calc.items() if k not in ("table", "verdict")}}


def extract(question: str) -> Profile:
    from app.rag.llm import get_llm, structured
    from app.rag.metering import purpose
    from app.rag.questionnaire import LOAN_TYPES
    llm = get_llm("fast")
    if llm is None:
        raise RuntimeError("GROQ_API_KEY is not configured, so customer profiles cannot be read.")
    with purpose("intake"):
        out = structured(llm, ExtractedProfile, f"""Convert a loan partner's message into a structured customer profile.
Do not calculate eligibility and do not recommend banks: only extract.

RULES
- Only details the message states. Do not guess, infer or add anything. Leave a field null when it is not stated.
- Copy amounts and numbers as written (e.g. "₹1,45,000", "98 lakh", "845", "8.5%", "20 years").
- loan_type: exactly one of {", ".join(LOAN_TYPES)}, or null. "HL" is Home Loan, "LAP" loan against property,
  "PL" Personal Loan, "BL" Business Loan.
- monthly_income: monthly salary / income as written; annual_income only if the message gives a yearly figure.
- property_location: the city of the property / where the loan is needed (e.g. "Pune"); job_location: where the
  customer works, if stated separately.
- Fill the matching field for each detail: CIBIL / credit score in cibil, EMIs in existing_emi, the loan needed in
  loan_amount, dues in outstanding_dues. A detail stated as "none" or "no" is still stated: write "None".
- customer_name only if the message names the customer. Never extract phone numbers.
- other: a list of any remaining stated details as short phrases.

MESSAGE:
{question}

Return JSON only, with exactly these keys:
{_SHAPE}""")
    fields = {k: str(v).strip() for k, v in out.model_dump().items() if k != "other" and v not in (None, "")}
    if fields.get("loan_type") not in LOAN_TYPES:
        fields.pop("loan_type", None)
    prof = Profile(question=question, fields=fields, other=[str(o).strip() for o in out.other if str(o).strip()][:8])
    prof.calc = compute(prof.fields)
    return prof


# ------------------------------------------------------------------ eligibility (application logic)
def _pct(x: float) -> str:
    return f"{x * 100:.1f}".rstrip("0").rstrip(".") + "%"


emi = eligibility.emi        # kept for callers of profile.emi


def _rate(s: Optional[str]) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)", s or "")
    v = float(m.group(1)) if m else None
    return v if v and 1 <= v <= 30 else None


def _years(s: Optional[str]) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(months?|m\b)?", s or "", re.I)
    if not m:
        return None
    v = float(m.group(1)) / (12 if m.group(2) else 1)
    return v if 1 <= v <= 40 else None


_NONE = re.compile(r"^\s*(nil|none|no|zero|0|no emis?|not any)\b", re.I)


def compute(fields: Dict[str, str]) -> Dict[str, object]:
    """Indicative eligibility: the customer's figures, the customer's rate / tenure when given (else the app's
    defaults for the loan type), and the app's FOIR policy. Nothing missing is assumed; every default is labelled."""
    a = eligibility.ASSUMPTIONS.get(fields.get("loan_type") or "", eligibility.ASSUMPTIONS["default"])
    foir = a["foir"]
    roi, roi_src = (_rate(fields.get("interest_rate")), "customer") if _rate(fields.get("interest_rate")) else (a["roi"], "app default")
    years, yrs_src = (_years(fields.get("tenure")), "customer") if _years(fields.get("tenure")) else (a["years"], "app default")
    # "18 LPA" / "12 lakh per annum" put in monthly_income is still a yearly figure: never read it as monthly.
    inc = eligibility._to_monthly(fields.get("monthly_income"), "monthly")
    if inc is None and eligibility.num(fields.get("annual_income")) is not None:
        inc = eligibility.num(fields.get("annual_income")) / 12
    raw_emi = fields.get("existing_emi")
    existing = 0.0 if raw_emi and _NONE.search(raw_emi) else eligibility.num(raw_emi)
    emi_known = existing is not None
    req = eligibility.num(fields.get("loan_amount"))
    value = eligibility.num(fields.get("property_value")) or eligibility.num(fields.get("agreement_value"))

    out: Dict[str, object] = {"computed": False, "foir": foir, "roi": roi, "roi_source": roi_src, "years": years,
                              "tenure_source": yrs_src, "existing_emi_known": emi_known}
    if inc is None:
        out["reason"] = "monthly income not provided"
        return out
    c = eligibility.calculate(inc, existing, req, foir, roi, years, value)    # the one shared calculation
    capacity, max_loan = c["emi_capacity"], c["max_loan"]
    yrs = f"{years:g} years"
    rows = [("Monthly Income", rupees(inc)),
            ("Existing EMI", rupees(existing) if emi_known else f"{NOT_PROVIDED} (not deducted)"),
            ("FOIR (app policy)", _pct(foir)),
            ("Interest Rate", f"{roi:g}% ({roi_src})"), ("Tenure", f"{yrs} ({yrs_src})"),
            ("EMI Capacity" + ("" if emi_known else " (before existing EMIs)"), rupees(capacity))]
    out.update(computed=True, income=c["income"], existing_emi=c["existing_emi"], emi_capacity=capacity,
               max_loan=max_loan, warnings=c["warnings"])
    if req:
        rows += [("Requested Loan", rupees(req)), ("Requested Loan EMI", rupees(c["emi_requested"]))]
        out.update({k: c[k] for k in ("requested", "emi_requested", "covers", "foir_needed")})
    rows.append(("Indicative Maximum Loan", rupees(max_loan)))
    if "ltv" in c:
        out["ltv"] = c["ltv"]
        rows.append(("LTV (requested loan ÷ property value)", _pct(c["ltv"])))
    out["table"] = "\n".join(["| Metric | Value |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in rows])
    if req:
        out["verdict"] = (f"within indicative eligibility (EMI {rupees(out['emi_requested'])} vs capacity {rupees(capacity)})"
                          if out["covers"] else
                          f"above indicative eligibility by {rupees(req - max_loan)} (EMI {rupees(out['emi_requested'])} "
                          f"vs capacity {rupees(capacity)})")
    else:
        out["verdict"] = f"loan amount not provided; indicative maximum loan {rupees(max_loan)}"
    if not emi_known:
        out["verdict"] += "; existing EMIs were not provided, so none were deducted"
    for w in out["warnings"]:
        out["verdict"] += f"; check: {w}"
    return out


PROFILE_RULES = """Additional rules: the user described a customer and asks which lenders / bankers to approach.
These rules take precedence over rule 5. The app has already extracted the profile, calculated the eligibility
and chosen the bankers; you present them. Answer in exactly this structure (Markdown), and nothing else:

1. One-line summary (no heading): the customer in a few words (employment, income, CIBIL, loan and location, only
   as given) and that the following lenders / RMs are the best matches in the knowledge base. If the BANKERS
   TABLE says no banker was found, say that instead.
2. "### Customer Profile" then the CUSTOMER PROFILE TABLE exactly as given.
3. "### Eligibility Snapshot" then the ELIGIBILITY SNAPSHOT TABLE exactly as given, then one sentence: "Based on
   the application's indicative calculation, the requested loan amount is <the ELIGIBILITY RESULT>. Final approval
   depends on the lender's underwriting and policy."
4. "### Bankers / Lenders to Approach — <location>" then the BANKERS TABLE exactly as given: same rows, order,
   names, numbers, notes and [S#]. Never add, drop, reorder or change a row, and never add a banker. Under it,
   one short italic line with the RANKING BASIS.
5. "### Also Check" then the ALSO CHECK lenders as a short bullet list (names only). Leave this section out when
   it says (none).
6. "### Important Eligibility Point" then 1-2 sentences, starting with "**Important:**": the single most useful
   observation from the ELIGIBILITY RESULT and the profile (e.g. EMI vs capacity, a missing detail, a strong or
   weak CIBIL). Never claim approval is guaranteed.

When these sections are given, also:
- HYPOTHETICAL SCENARIO: start with the exact line it gives, before item 1.
- ALREADY SHOWN: in item 1 say these are additional lenders beyond those already listed; never list an
  already-shown lender again.
- LENDER ELIGIBILITY TABLE: after item 4, "### Lender Eligibility" and the table exactly as given.
- WHY <LENDER>: right after item 1, "### Why <lender>" and the text exactly as given. Never add another reason.

Use only figures from the tables and the profile; do not calculate anything. Do not repeat the profile in prose,
explain loans in general or add advice. Keep the whole answer short (about 400-800 tokens). No sources section."""


def _lender_key(name: str) -> str:
    from app.services.banker_directory import canonical_lender, key
    return key(canonical_lender(name)[0])
