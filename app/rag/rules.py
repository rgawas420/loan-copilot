"""Deterministic rules engine: checks a case against every structured lender program for its loan type.

Each check is pass / fail / deviation / unknown / warn. Program status:
  any fail                 -> Not Suitable
  any deviation or unknown -> Possible - Credit Check Required
  otherwise                -> Strong Match
'warn' (stale or sample policy) is shown as a concern but does not change the status.
The LLM never decides status, lender, contacts or codes; it only explains these results.
"""
from datetime import date
from typing import List, Optional

from app.core.config import get_settings
from app.rag.eligibility import _latest, indicative_eligibility, monthly_income, num
from app.rag.policies import Program, programs_for

PASS, FAIL, DEV, UNKNOWN, WARN = "pass", "fail", "deviation", "unknown", "warn"
STATUS_ORDER = {"Strong Match": 0, "Possible - Credit Check Required": 1, "Not Suitable": 2}
LEGAL_ALIASES = {"GP": "Gram Panchayat"}
LTV_BASE_KEYS = ["agreement_value", "market_value", "valuation", "onroad", "veh_cost", "equip_cost", "invoice_amt"]


def rupees(v) -> str:
    if v is None:
        return "not computed"
    v = round(v)
    s = str(abs(v))
    if len(s) > 3:                                   # Indian grouping: 12,34,56,789
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        s = ",".join(([head] if head else []) + parts) + "," + tail
    return ("-" if v < 0 else "") + "₹" + s


def _yes_no_check(checks, criterion, answer, hard: bool, bad_msg: str):
    if answer == "Yes":
        checks.append((criterion, FAIL if hard else DEV, bad_msg + ("" if hard else " (policy is silent; lender discretion)")))
    elif answer == "No":
        checks.append((criterion, PASS, "None reported"))
    else:
        checks.append((criterion, UNKNOWN, "Not known yet; confirm from the CIBIL report"))


def _min_check(checks, criterion, value, minimum, unit=""):
    if value is None:
        checks.append((criterion, UNKNOWN, f"Not captured (minimum {minimum:g}{unit})"))
    elif value < minimum:
        checks.append((criterion, FAIL, f"{value:g}{unit} is below the minimum {minimum:g}{unit}"))
    else:
        checks.append((criterion, PASS, f"{value:g}{unit} meets the minimum {minimum:g}{unit}"))


def _ltv_limit(p: Program, req: float) -> Optional[float]:
    if p.ltv_slabs:
        for upto, ltv in p.ltv_slabs:
            if upto is None or req <= upto:
                return ltv
    return p.max_ltv


def evaluate(p: Program, case: dict, today: Optional[date] = None) -> dict:
    today = today or date.today()
    checks: List[tuple] = []
    req = num(case.get("loan_amount"), 0) or 0

    if p.profiles:
        prof = case.get("profile")
        checks.append(("Profile", PASS if prof in p.profiles else FAIL,
                       f"{prof} accepted" if prof in p.profiles else f"Program is for {', '.join(p.profiles)} only"))

    if p.age_min is not None or p.age_max is not None:
        age, lo, hi = num(case.get("age")), p.age_min or 0, p.age_max or 200
        if age is None:
            checks.append(("Age", UNKNOWN, "Age not captured"))
        else:
            ok = lo <= age <= hi
            checks.append(("Age", PASS if ok else FAIL, f"Age {age:g} {'within' if ok else 'outside'} {lo:g}-{hi:g}"))

    if p.amount_min or p.amount_max:
        if p.amount_min and req < p.amount_min:
            checks.append(("Loan amount", FAIL, f"{rupees(req)} is below the program minimum {rupees(p.amount_min)}"))
        elif p.amount_max and req > p.amount_max:
            checks.append(("Loan amount", FAIL, f"{rupees(req)} is above the program maximum {rupees(p.amount_max)}"))
        else:
            checks.append(("Loan amount", PASS, f"{rupees(req)} is within the program range"))

    if p.min_cibil:
        raw = str(case.get("cibil") or "")
        score = None if raw.lower().startswith("not") else num(raw)
        if score is None:
            checks.append(("CIBIL", UNKNOWN, f"CIBIL not checked; program needs {p.min_cibil}+. Pull the report first"))
        elif score >= p.min_cibil:
            checks.append(("CIBIL", PASS, f"{score:g} meets {p.min_cibil}+"))
        elif p.cibil_deviation_floor and score >= p.cibil_deviation_floor:
            checks.append(("CIBIL", DEV, f"{score:g} is below {p.min_cibil}; allowed as a deviation down to {p.cibil_deviation_floor}"))
        else:
            checks.append(("CIBIL", FAIL, f"{score:g} is below the minimum {p.cibil_deviation_floor or p.min_cibil}"))

    _yes_no_check(checks, "Current overdue / DPD", case.get("overdue"), p.no_overdue, "Customer reports a current overdue")
    _yes_no_check(checks, "Settled / written-off", case.get("settled"), p.no_settled, "Customer reports a settled or written-off account")

    if p.min_monthly_income:
        inc, _ = monthly_income(case)
        if inc is None:
            checks.append(("Monthly income", UNKNOWN, f"Income not captured (minimum {rupees(p.min_monthly_income)})"))
        else:
            ok = inc >= p.min_monthly_income
            checks.append(("Monthly income", PASS if ok else FAIL,
                           f"{rupees(inc)} {'meets' if ok else 'is below'} the minimum {rupees(p.min_monthly_income)}"))

    if p.min_job_years:
        _min_check(checks, "Years in current job", num(case.get("current_job_years")), p.min_job_years, " yrs")
    if p.min_total_exp_years:
        _min_check(checks, "Total experience", num(case.get("total_exp_years")), p.min_total_exp_years, " yrs")
    if p.min_business_years:
        yrs = num(case.get("business_years"))
        _min_check(checks, "Business vintage", yrs if yrs is not None else num(case.get("practice_years")),
                   p.min_business_years, " yrs")
    if p.min_turnover:
        t = num(case.get("turnover")) or _latest(case.get("turnover_3y")) or num(case.get("gst_turnover_12m"))
        if t is None:
            checks.append(("Turnover", UNKNOWN, f"Turnover not captured (minimum {rupees(p.min_turnover)})"))
        else:
            ok = t >= p.min_turnover
            checks.append(("Turnover", PASS if ok else FAIL,
                           f"{rupees(t)} {'meets' if ok else 'is below'} the minimum {rupees(p.min_turnover)}"))

    if p.salary_modes:
        mode = case.get("salary_mode")
        if not mode:
            checks.append(("Salary mode", UNKNOWN, f"Not captured; program needs {', '.join(p.salary_modes)}"))
        else:
            ok = mode in p.salary_modes
            checks.append(("Salary mode", PASS if ok else FAIL,
                           f"{mode} {'accepted' if ok else 'not accepted'}; program needs {', '.join(p.salary_modes)}"))

    if p.max_unsecured_loans is not None:
        n = num(case.get("unsecured_running"))
        if n is None:
            checks.append(("Unsecured loans running", UNKNOWN, f"Not captured (maximum {p.max_unsecured_loans})"))
        else:
            checks.append(("Unsecured loans running", PASS if n <= p.max_unsecured_loans else FAIL,
                           f"{n:g} running; maximum {p.max_unsecured_loans}"))
    if p.max_enquiries_3m is not None:
        n = num(case.get("enquiries_3m"))
        if n is None:
            checks.append(("Enquiries in 3 months", UNKNOWN, f"Not captured (maximum {p.max_enquiries_3m})"))
        else:
            checks.append(("Enquiries in 3 months", PASS if n <= p.max_enquiries_3m else FAIL,
                           f"{n:g} enquiries; maximum {p.max_enquiries_3m}"))
    if p.no_bounces:
        _yes_no_check(checks, "Bounces", case.get("bounce_overdue") or case.get("bounces_6m"), True,
                      "Customer reports EMI/cheque bounces")

    limit = _ltv_limit(p, req)
    if limit:
        base_key = next((k for k in LTV_BASE_KEYS if num(case.get(k))), None)
        if base_key is None or not req:
            checks.append(("LTV", UNKNOWN, f"Property/asset value not captured (maximum LTV {limit:.0%})"))
        else:
            base = num(case.get(base_key))
            ltv = req / base
            if ltv <= limit:
                checks.append(("LTV", PASS, f"{ltv:.0%} of {rupees(base)} is within the maximum {limit:.0%}"))
            else:
                checks.append(("LTV", FAIL, f"{ltv:.0%} of {rupees(base)} is above the maximum {limit:.0%}; "
                                            f"loan up to {rupees(base * limit)} fits"))

    if p.legal_allowed:
        cat = LEGAL_ALIASES.get(case.get("legal_cat"), case.get("legal_cat"))
        if not cat or cat == "Do Not Know":
            checks.append(("Legal category", UNKNOWN, "Legal category not known; get the title documents checked"))
        elif cat in p.legal_allowed:
            checks.append(("Legal category", PASS, f"{cat} accepted"))
        elif cat in p.legal_case_to_case:
            checks.append(("Legal category", DEV, f"{cat} accepted case-to-case with a legal opinion"))
        else:
            checks.append(("Legal category", FAIL, f"{cat} not accepted (accepted: {', '.join(p.legal_allowed)})"))

    if p.rera_required_under_construction and case.get("possession") == "Under Construction":
        r = case.get("rera")
        checks.append(("RERA", PASS if r == "Yes" else FAIL if r == "No" else UNKNOWN,
                       {"Yes": "RERA registered", "No": "Under-construction property must be RERA registered"}
                       .get(r, "RERA status not known; mandatory for under-construction property")))

    elig = indicative_eligibility(case, p.foir, p.roi, p.tenure_years)
    mx = elig["indicative_max_loan"]
    if mx is None:
        checks.append(("Repayment capacity (FOIR)", UNKNOWN, "Income not captured, so eligibility was not computed"))
    elif not req or mx >= req:
        checks.append(("Repayment capacity (FOIR)", PASS, f"Indicative maximum {rupees(mx)} covers the request"))
    elif mx >= 0.8 * req:
        checks.append(("Repayment capacity (FOIR)", DEV, f"Indicative maximum {rupees(mx)} is slightly below "
                                                         f"{rupees(req)}; reduce the amount or add a co-applicant"))
    else:
        checks.append(("Repayment capacity (FOIR)", FAIL, f"Indicative maximum {rupees(mx)} is well below {rupees(req)}"))

    stale = get_settings().policy_stale_days
    if p.policy_last_verified is None:
        checks.append(("Policy freshness", WARN, "Policy verification date unknown; confirm with the RM"))
    elif (today - p.policy_last_verified).days > stale:
        checks.append(("Policy freshness", WARN, f"Policy last verified {p.policy_last_verified} "
                                                 f"(over {stale} days ago); confirm with the RM"))
    if p.sample:
        checks.append(("Sample data", WARN, "This is a fictitious sample lender; replace with a verified policy"))

    results = {r for _, r, _ in checks}
    status = ("Not Suitable" if FAIL in results else
              "Possible - Credit Check Required" if results & {DEV, UNKNOWN} else "Strong Match")
    return {
        "program_id": p.id, "lender": p.lender, "program": p.name, "status": status,
        "checks": [{"criterion": c, "result": r, "detail": d} for c, r, d in checks],
        "max_loan": mx, "eligibility": elig, "sample": p.sample,
        "banker_rm": p.banker_rm, "mobile": p.mobile, "urban_money_code": p.urban_money_code,
        "policy_last_verified": str(p.policy_last_verified) if p.policy_last_verified else "Unknown",
        "source": p.source,
    }


def _rank_key(r):
    open_items = sum(c["result"] in (DEV, UNKNOWN) for c in r["checks"])
    fails = sum(c["result"] == FAIL for c in r["checks"])
    return STATUS_ORDER[r["status"]], fails, open_items, -(r["max_loan"] or 0)


def match(case: dict, today: Optional[date] = None) -> List[dict]:
    """Evaluate every structured program for the case's loan type, best first."""
    return sorted((evaluate(p, case, today) for p in programs_for(case.get("loan_type"))), key=_rank_key)
