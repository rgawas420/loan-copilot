"""Free-text case intake: 'Salaried, 32, 85k in hand, 60L resale HL in Pune, CIBIL 760' -> questionnaire fields.

Mobile numbers are pulled out locally and redacted before the text goes to the LLM. Every extracted value is
validated with the same rules as a typed answer; anything invalid is reported back, not stored.
"""
import re

from app.rag.llm import get_llm, structured
from app.rag.metering import purpose
from app.rag.questionnaire import PERSONAL_KEYS, apply_answer, field_catalogue, find_question
from app.rag.state import Extracted

_MOBILE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?([6-9]\d{4}[\s-]?\d{5})(?!\d)")


def redact(text: str):
    found = _MOBILE.search(text)
    mobile = re.sub(r"\D", "", found.group(1)) if found else None
    return _MOBILE.sub("[mobile]", text), mobile


def merge_fields(case: dict, fields: dict):
    """Apply extracted fields in questionnaire order, repeating so that answers unlock dependent modules
    (loan_type first, then its module). Returns (case, filled keys, rejected [{key, value, reason}])."""
    case, filled, rejected = dict(case), [], []
    pending = {k: v for k, v in fields.items() if v not in (None, "")}
    progress = True
    while pending and progress:
        progress = False
        for key in list(pending):
            if find_question(case, key) is None:
                continue
            new, err = apply_answer(case, key, pending.pop(key))
            progress = True
            if err:
                rejected.append({"key": key, "value": fields[key], "reason": err})
            else:
                case = new
                filled.append(key)
    rejected += [{"key": k, "value": v, "reason": "Does not apply to this loan type"} for k, v in pending.items()]
    return case, filled, rejected


def extract(text: str, case: dict):
    llm = get_llm("fast")
    if llm is None:
        raise RuntimeError("GROQ_API_KEY is not configured, so free-text intake is unavailable.")
    clean, mobile = redact(text)
    with purpose("intake"):
        out = _extract(llm, clean)
    fields = {k: str(v) for k, v in out.fields.items() if k not in PERSONAL_KEYS}
    if mobile and not case.get("mobile"):
        fields["mobile"] = mobile
    fields = {k: v for k, v in fields.items() if not case.get(k)}     # never overwrite typed answers
    return merge_fields(case, fields)


def _extract(llm, clean: str):
    return structured(llm, Extracted, f"""Extract loan case details from a channel partner's note.

FIELDS (key: question. Options: allowed values):
{field_catalogue()}

RULES
- Return only fields that the note states clearly. Do not guess.
- For fields with options, return exactly one of the listed options.
- Keep amounts as written (e.g. "60 lakh", "85k"). Monthly in-hand salary goes to net_salary.
- "HL" means Home Loan, "LAP" loan against property, "PL" personal loan, "BL" business loan.
- Ignore any person's name.

NOTE:
{clean}

Return JSON only: {{"fields": {{"key": "value", ...}}}}""")
