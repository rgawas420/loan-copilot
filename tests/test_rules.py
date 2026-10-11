from datetime import date

from app.rag.rules import match, rupees

TODAY = date(2026, 10, 4)
HL = {"loan_type": "Home Loan", "loan_amount": "40 lakh", "age": "32", "profile": "Salaried", "cibil": "760",
      "overdue": "No", "settled": "No", "existing_emi": "0", "pin": "411001", "hl_purpose": "Resale",
      "agreement_value": "60 lakh", "prop_pin": "411038", "possession": "Ready Possession", "legal_cat": "NA",
      "net_salary": "85000", "current_job_years": "3"}


def by_lender(case, today=TODAY):
    return {r["lender"]: r for r in match(case, today)}


def check(r, criterion):
    return next(c for c in r["checks"] if c["criterion"] == criterion)


def test_salaried_home_loan_is_strong_with_lender_a_and_not_b():
    m = match(HL, TODAY)
    assert m[0]["lender"] == "Sample Lender A" and m[0]["status"] == "Strong Match"
    assert by_lender(HL)["Sample Lender B"]["status"] == "Not Suitable"
    assert m[0]["urban_money_code"] == "UM-SAMPLE-A01"


def test_cibil_in_deviation_band_is_possible():
    r = by_lender({**HL, "cibil": "710"})["Sample Lender A"]
    assert r["status"] == "Possible - Credit Check Required" and check(r, "CIBIL")["result"] == "deviation"
    assert by_lender({**HL, "cibil": "690"})["Sample Lender A"]["status"] == "Not Suitable"
    assert check(by_lender({**HL, "cibil": "Not Checked"})["Sample Lender A"], "CIBIL")["result"] == "unknown"


def test_legal_category_and_ltv():
    assert check(by_lender({**HL, "legal_cat": "Gunthewari"})["Sample Lender A"], "Legal category")["result"] == "fail"
    se = {**HL, "profile": "Self-Employed Business", "itr_income": "24 lakh", "business_years": "5",
          "legal_cat": "Gunthewari"}
    assert check(by_lender(se)["Sample Lender B"], "Legal category")["result"] == "deviation"
    r = by_lender({**HL, "loan_amount": "55 lakh"})["Sample Lender A"]
    assert check(r, "LTV")["result"] == "fail" and "45,00,000" in check(r, "LTV")["detail"]


def test_ltv_slab_uses_loan_amount():
    r = by_lender({**HL, "loan_amount": "24 lakh", "agreement_value": "30 lakh"})["Sample Lender A"]
    assert check(r, "LTV")["result"] == "pass"           # 80% allowed up to 30 lakh


def test_under_construction_needs_rera():
    r = by_lender({**HL, "possession": "Under Construction", "rera": "No"})["Sample Lender A"]
    assert check(r, "RERA")["result"] == "fail"


def test_personal_loan_cash_salary_and_overdue_fail():
    pl = {"loan_type": "Personal Loan", "loan_amount": "5 lakh", "age": "30", "profile": "Salaried", "cibil": "780",
          "overdue": "No", "settled": "No", "existing_emi": "0", "net_salary": "60000", "current_job_years": "2",
          "total_exp_years": "5", "salary_mode": "Bank Transfer"}
    assert match(pl, TODAY)[0]["status"] == "Strong Match"
    assert match({**pl, "salary_mode": "Cash"}, TODAY)[0]["status"] == "Not Suitable"
    assert match({**pl, "overdue": "Yes"}, TODAY)[0]["status"] == "Not Suitable"
    assert match({**pl, "overdue": "Do Not Know"}, TODAY)[0]["status"] == "Possible - Credit Check Required"


def test_foir_shortfall_is_deviation_then_fail():
    base = {**HL, "loan_amount": "40 lakh", "agreement_value": "80 lakh"}
    assert check(by_lender({**base, "net_salary": "52000"})["Sample Lender A"], "Repayment capacity (FOIR)")["result"] == "deviation"
    assert check(by_lender({**base, "net_salary": "42000", "existing_emi": "10000"})["Sample Lender A"],
                 "Repayment capacity (FOIR)")["result"] == "fail"


def test_stale_policy_is_a_warning_not_a_status_change():
    r = by_lender(HL, today=date(2027, 6, 1))["Sample Lender A"]
    assert check(r, "Policy freshness")["result"] == "warn" and r["status"] == "Strong Match"


def test_no_structured_programs_for_other_loan_types():
    assert match({"loan_type": "Education Loan"}) == []


def test_rupees_indian_grouping():
    assert rupees(12345678) == "₹1,23,45,678" and rupees(1000) == "₹1,000" and rupees(None) == "not computed"
