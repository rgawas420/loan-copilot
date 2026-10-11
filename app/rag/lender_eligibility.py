"""Per-lender eligibility for a chat customer profile, from the deterministic rules engine (app/rag/rules.py) and
the structured lender programs (data/policies/*.yaml). The model never decides it.

Each lender gets exactly one outcome:
  eligible             every loaded rule passes
  credit_check         no rule fails, but a deviation needs the credit team's approval
  not_eligible         a verified rule fails (the failed checks are the reasons)
  missing_input        no rule fails, but a rule needs a detail the partner has not given
  policy_unavailable   no structured program for this lender and loan type: nothing can be concluded

"policy_unavailable" is never presented as a rejection: the app only lacks that lender's rules.
"""
import re
from typing import Dict, List, Optional

from app.rag.policies import programs_for
from app.rag.rules import DEV, FAIL, UNKNOWN, evaluate

ELIGIBLE, CREDIT_CHECK, NOT_ELIGIBLE, MISSING, NO_POLICY = (
    "eligible", "credit_check", "not_eligible", "missing_input", "policy_unavailable")
LABELS = {ELIGIBLE: "Eligible under the loaded rules", CREDIT_CHECK: "Possible - credit check required",
          NOT_ELIGIBLE: "Not eligible under a loaded rule", MISSING: "Cannot tell yet - a detail is missing",
          NO_POLICY: "Cannot tell - this lender's rules are not loaded"}


def _key(name: str) -> str:
    from app.services.banker_directory import canonical_lender, key
    return key(canonical_lender(name)[0])


def _profile_type(employment: str) -> Optional[str]:
    e = (employment or "").lower()
    if re.search(r"salar|employ|job|service", e) and "self" not in e:
        return "Salaried"
    if re.search(r"doctor|\bca\b|professional|architect|lawyer|consultant", e):
        return "Self-Employed Professional"
    if re.search(r"self|business|proprietor|trader|shop|owner", e):
        return "Self-Employed Business"
    return None


def case_from_profile(fields: Dict[str, str]) -> dict:
    """The chat profile in the rules engine's case keys. Only what the partner gave; nothing is assumed."""
    from app.rag.eligibility import num
    kind = _profile_type(fields.get("employment", ""))
    case = {"loan_type": fields.get("loan_type"), "profile": kind, "cibil": fields.get("cibil"),
            "age": fields.get("age"), "loan_amount": num(fields.get("loan_amount")),
            "existing_emi": fields.get("existing_emi"),
            "property_value": fields.get("property_value") or fields.get("agreement_value"),
            "agreement_value": fields.get("agreement_value")}
    income = fields.get("monthly_income")
    if income:                      # the rules read salary or ITR income depending on the profile
        if kind == "Salaried" or kind is None:
            case["net_salary"] = income
        if kind != "Salaried":
            annual = num(fields.get("annual_income"))
            case["itr_income"] = annual if annual is not None else (num(income) or 0) * 12 or None
    return {k: v for k, v in case.items() if v not in (None, "")}


def evaluate_lender(fields: Dict[str, str], lender: str) -> dict:
    """The outcome for one lender (see the module docstring), with the checks behind it."""
    lt = fields.get("loan_type") or ""
    progs = [p for p in programs_for(lt) if _key(p.lender) == _key(lender)] if lt else []
    if not progs:
        return {"lender": lender, "loan_type": lt, "outcome": NO_POLICY, "label": LABELS[NO_POLICY], "programs": []}
    case = case_from_profile(fields)
    results = [evaluate(p, case) for p in progs]
    best = min(results, key=lambda r: (r["status"] == "Not Suitable",
                                       sum(c["result"] in (DEV, UNKNOWN) for c in r["checks"])))
    checks = best["checks"]
    if best["status"] == "Not Suitable":
        outcome = NOT_ELIGIBLE
    elif any(c["result"] == UNKNOWN for c in checks):
        outcome = MISSING
    elif any(c["result"] == DEV for c in checks):
        outcome = CREDIT_CHECK
    else:
        outcome = ELIGIBLE
    return {"lender": lender, "loan_type": lt, "outcome": outcome, "label": LABELS[outcome],
            "program": best["program"], "sample": best["sample"], "source": best["source"],
            "policy_last_verified": best["policy_last_verified"],
            "failed": [f"{c['criterion']}: {c['detail']}" for c in checks if c["result"] == FAIL],
            "missing": [f"{c['criterion']}: {c['detail']}" for c in checks if c["result"] == UNKNOWN],
            "deviations": [f"{c['criterion']}: {c['detail']}" for c in checks if c["result"] == DEV],
            "programs": [r["program"] for r in results]}


def evaluate_all(fields: Dict[str, str], lenders: List[str]) -> List[dict]:
    return [evaluate_lender(fields, l) for l in dict.fromkeys(lenders)]


def any_rules(loan_type: str) -> bool:
    return bool(loan_type and programs_for(loan_type))


def table(results: List[dict]) -> str:
    """Markdown table of the outcomes, for lenders with rules (the others are summarised in one line)."""
    rows = [r for r in results if r["outcome"] != NO_POLICY]
    if not rows:
        return ""
    out = ["| Lender | Eligibility (rules engine) | Reason |", "|---|---|---|"]
    for r in rows:
        why = "; ".join(r.get("failed") or r.get("missing") or r.get("deviations") or []) or "All loaded rules pass"
        out.append(f"| {r['lender']} | {r['label']} | {why.replace('|', '/')} |")
    return "\n".join(out)


def explain(r: dict, fields: Dict[str, str]) -> str:
    """One lender's outcome in plain words, only from the rule results (never a guessed reason)."""
    lt = r.get("loan_type") or "this loan"
    name = r["lender"]
    if r["outcome"] == NO_POLICY:
        return (f"I can't verify whether {name} would approve this customer: {name}'s {lt} eligibility rules are not "
                "loaded in the app, so no rule was checked and no rejection reason can be given.")
    if r["outcome"] == NOT_ELIGIBLE:
        return f"{name} ({r['program']}) does not fit under its loaded rules: " + "; ".join(r["failed"]) + "."
    if r["outcome"] == MISSING:
        return f"{name} ({r['program']}) can't be decided yet. Missing: " + "; ".join(r["missing"]) + "."
    if r["outcome"] == CREDIT_CHECK:
        return f"{name} ({r['program']}) is possible with a credit check: " + "; ".join(r["deviations"]) + "."
    return f"{name} ({r['program']}) passes all of its loaded rules for this customer."


def coverage(min_rms: int = 1, all_products: bool = False) -> dict:
    """For admins / the credit team: for each loan type in the RM lists, which lenders have RMs but no structured
    eligibility rules (so the app can't say whether they suit a customer), and which loaded rules are samples or
    overdue for verification."""
    from contextlib import closing
    from datetime import date

    from app.core.config import get_settings
    from app.rag.policies import load_programs
    from app.services import banker_directory
    from app.services.knowledge_base import _db
    progs = load_programs()
    have = {(p.loan_type, _key(p.lender)): p for p in progs}
    stale_days = get_settings().policy_stale_days
    rows = []
    with closing(_db()) as db:
        banker_directory.ensure_schema(db)
        found = db.execute(
            "SELECT c.product, l.name, COUNT(DISTINCT b.banker_id), COUNT(DISTINCT c.city_key) FROM dir_coverage c "
            "JOIN dir_bankers b ON b.banker_id = c.banker_id JOIN dir_lenders l ON l.lender_id = b.lender_id "
            "WHERE c.product != '' GROUP BY c.product, l.lender_id HAVING COUNT(DISTINCT b.banker_id) >= ? "
            "ORDER BY c.product, COUNT(DISTINCT b.banker_id) DESC", (min_rms,)).fetchall()
    from app.rag.questionnaire import LOAN_TYPES
    for product, lender, rms, cities in found:
        if not all_products and product not in LOAN_TYPES:      # odd product names in the RM sheets
            continue
        p = have.get((product, _key(lender)))
        if p is None:
            status = "missing"
        elif p.sample:
            status = "sample"
        elif p.policy_last_verified is None or (date.today() - p.policy_last_verified).days > stale_days:
            status = "stale"
        else:
            status = "loaded"
        rows.append({"loan_type": product, "lender": lender, "rms": rms, "cities": cities, "rules": status,
                     "program": p.name if p else "", "verified": str(p.policy_last_verified) if p and p.policy_last_verified else ""})
    totals = {}
    for r in rows:
        t = totals.setdefault(r["loan_type"], {"lenders": 0, "missing": 0, "sample": 0, "stale": 0, "loaded": 0})
        t["lenders"] += 1
        t[r["rules"]] += 1
    return {"programs_loaded": len(progs), "totals": totals, "lenders": rows}
