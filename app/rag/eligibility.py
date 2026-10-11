"""Indicative eligibility. ASSUMPTIONS are placeholders used only when no lender program supplies its own
FOIR / ROI / tenure (the rules engine passes the lender's values). Not a sanction."""
import re

_UNIT = r"\s*(crores?|cr|lakhs?|lacs?|lpa|l|k)?\b"


def _scale(v: float, unit: str | None) -> float:
    unit = (unit or "").lower()
    if unit.startswith("cr"): return v * 1e7
    if unit.startswith(("lakh", "lac", "lpa")) or unit == "l": return v * 1e5
    if unit == "k": return v * 1e3
    return v


def _clean(x) -> str:
    s = str(x).lower().replace(",", "").replace("₹", " ")
    return re.sub(r"\brs\.?", " ", s).replace("inr", " ")


def num(x, default=None):
    """Parse '₹ 12,50,000', '12.5 lakh', '1.2 cr', '45k', '12 LPA' -> float."""
    if x is None: return default
    m = re.search(r"(-?\d+(?:\.\d+)?)" + _UNIT, _clean(x))
    return _scale(float(m.group(1)), m.group(2)) if m else default


def num_list(x):
    """Parse '40L / 45L / 50L' (oldest to latest) -> [4000000, 4500000, 5000000]."""
    if x is None: return []
    parts = re.split(r"[/;|]|,\s+", str(x))
    return [v for v in (num(p) for p in parts) if v is not None]


def _is_annual(x) -> bool:
    return bool(re.search(r"annual|year|yearly|\bp\.?a\b|per annum|lpa", str(x).lower()))


def _to_monthly(x, default_period):
    v = num(x)
    if v is None: return None
    annual = _is_annual(x) or (default_period == "annual" and not re.search(r"month|\bpm\b", str(x).lower()))
    return v / 12 if annual else v


def _latest(x):
    vals = num_list(x)
    return vals[-1] if vals else None


# Where monthly income comes from, per loan type: ordered (field, default period). First answered wins.
INCOME_SOURCES = {
    "Personal Loan":      [("net_salary", "monthly")],
    "New Car":            [("income_amt", "monthly"), ("net_salary", "monthly")],
    "Used Car":           [("income_amt", "monthly"), ("net_salary", "monthly")],
    "Commercial Vehicle": [("biz_income", "monthly")],
    "Education Loan":     [("coapp_income", "annual")],
    "LRD":                [("monthly_rent", "monthly")],
    "Working Capital":    [("profit_3y", "annual_latest"), ("net_profit", "annual")],
    "Professional Loan":  [("itr_income", "annual")],
}
_SALARIED = [("net_salary", "monthly")]
_SELF = [("itr_income", "annual"), ("net_profit", "annual"), ("profit_3y", "annual_latest")]


def monthly_income(case):
    """Return (monthly income or None, field used)."""
    sources = INCOME_SOURCES.get(case.get("loan_type"))
    if sources is None:
        sources = _SALARIED if case.get("profile") == "Salaried" else _SELF
    for key, period in sources:
        val = case.get(key)
        if val in (None, ""):
            continue
        m = (_latest(val) / 12 if _latest(val) is not None else None) if period == "annual_latest" \
            else _to_monthly(val, period)
        if m is not None:
            return m, key
    return None, None


ASSUMPTIONS = {   # EDIT THESE: indicative only, used when the lender program has no FOIR/ROI/tenure
    "Home Loan":              dict(foir=0.55, roi=8.75, years=25),
    "LAP":                    dict(foir=0.60, roi=10.0, years=15),
    "Personal Loan":          dict(foir=0.50, roi=13.0, years=5),
    "Business Loan":          dict(foir=0.60, roi=16.0, years=4),
    "Professional Loan":      dict(foir=0.60, roi=14.0, years=5),
    "Machinery Loan":         dict(foir=0.60, roi=12.0, years=5),
    "New Car":                dict(foir=0.50, roi=9.5,  years=7),
    "Used Car":               dict(foir=0.50, roi=13.0, years=5),
    "Commercial Vehicle":     dict(foir=0.55, roi=12.0, years=5),
    "Construction Equipment": dict(foir=0.55, roi=12.0, years=4),
    "Education Loan":         dict(foir=0.50, roi=10.5, years=10),
    "LRD":                    dict(foir=0.85, roi=9.5,  years=12),   # share of rent servicing the EMI
    "default":                dict(foir=0.50, roi=11.0, years=7),
}
WC_TURNOVER_SHARE = 0.20      # Working Capital: indicative limit = 20% of latest turnover (placeholder)
NOT_COMPUTED = {"Construction Finance"}   # project-appraisal based; no income formula


def emi_to_principal(emi, roi_pct, years):
    r = roi_pct / 1200; n = years * 12
    return emi * (1 - (1 + r) ** -n) / r


def emi(principal, roi_pct, years):
    r, n = roi_pct / 1200, round(years * 12)
    return principal * r / (1 - (1 + r) ** -n)


MAX_PLAUSIBLE_MONTHLY_INCOME = 50e5     # above ₹50 lakh a month, ask the partner to confirm the figure
TYPICAL_MAX_LTV = 0.90                  # above this, most lenders' LTV caps (75-90%) are exceeded


def calculate(income, existing_emi, requested, foir, roi, years, property_value=None) -> dict:
    """The one FOIR calculation used by every path (questionnaire, chat profile, rules engine).

    income: monthly income (required). existing_emi: monthly EMIs already paid, or None when not given (then
    nothing is deducted, and that is reported, never silently assumed). requested / property_value: optional.
    Returns capacity, maximum loan, the requested loan's EMI and FOIR, LTV, and plausibility warnings."""
    known = existing_emi is not None
    capacity = max(income * foir - (existing_emi or 0), 0)
    max_loan = emi_to_principal(capacity, roi, years) if capacity else 0
    out = {"income": round(income), "existing_emi": round(existing_emi or 0), "existing_emi_known": known,
           "foir": foir, "roi": roi, "years": years, "emi_capacity": round(capacity), "max_loan": round(max_loan),
           "warnings": []}
    if income > MAX_PLAUSIBLE_MONTHLY_INCOME:
        out["warnings"].append(f"monthly income of {_inr(income)} is unusually high; confirm it is monthly, not yearly")
    if requested:
        need = emi(requested, roi, years)
        out.update(requested=round(requested), emi_requested=round(need), covers=max_loan >= requested,
                   foir_needed=round((need + (existing_emi or 0)) / income, 4) if income else None)
        if property_value:
            out["ltv"] = round(requested / property_value, 4)
            if out["ltv"] > TYPICAL_MAX_LTV:
                out["warnings"].append(f"LTV {out['ltv']:.0%} is above the {TYPICAL_MAX_LTV:.0%} most lenders allow")
    return out


def _inr(v) -> str:
    from app.rag.rules import rupees
    return rupees(v)


def indicative_eligibility(case, foir=None, roi=None, years=None):
    lt = case.get("loan_type")
    a = ASSUMPTIONS.get(lt, ASSUMPTIONS["default"])
    foir, roi, years = foir or a["foir"], roi or a["roi"], years or a["years"]
    inc, inc_src = monthly_income(case)
    emi_known = num(case.get("existing_emi")) is not None
    emi_amt = num(case.get("existing_emi"), 0)
    req = num(case.get("loan_amount"), 0)
    out = {"monthly_income_used": round(inc) if inc is not None else None, "income_field": inc_src,
           "existing_emi": round(emi_amt), "existing_emi_known": emi_known, "assumed_FOIR": foir, "assumed_ROI_%": roi,
           "assumed_tenure_yrs": years, "requested": round(req), "method": "FOIR",
           "note": "Indicative only. Not a sanction. Final eligibility is decided by the lender."}
    if not emi_known and lt != "LRD":
        out["note"] = "Existing EMIs were not captured, so none were deducted. " + out["note"]

    max_loan = None
    if lt in NOT_COMPUTED:
        out["method"] = "not computed (project appraisal)"
    elif lt == "Working Capital":
        t = _latest(case.get("turnover_3y")) or num(case.get("gst_turnover_12m"))
        max_loan = t * WC_TURNOVER_SHARE if t else None
        out["method"] = f"{int(WC_TURNOVER_SHARE * 100)}% of latest turnover"
    elif inc is not None:
        c = calculate(inc, 0 if lt == "LRD" else emi_amt, req, foir, roi, years)
        max_loan = c["max_loan"]
        out["warnings"] = c["warnings"]

    out["indicative_max_loan"] = round(max_loan) if max_loan is not None else None
    out["covers_request"] = bool(max_loan >= req) if (req and max_loan is not None) else None
    if max_loan is None and out["method"] == "FOIR":
        out["note"] = "Income for this loan type was not captured, so eligibility was not computed. " + out["note"]
    return out
