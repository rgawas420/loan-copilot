from app.rag.questionnaire import (applicable, apply_answer, enough_info, find_question, kind_of, llm_safe_case,
                                   next_question, prune_case, validate)
from app.rag.eligibility import indicative_eligibility, num, num_list

READY_PL = {"loan_type": "Personal Loan", "loan_amount": "5 lakh", "age": "30", "profile": "Salaried", "cibil": "750",
            "overdue": "No", "settled": "No", "existing_emi": "0", "pin": "411001", "net_salary": "80000",
            "employer": "Acme", "current_job_years": "3", "salary_mode": "Bank Transfer"}


def keys(case):
    return [q.key for q in applicable(case)]


def test_first_question_is_loan_type():
    assert next_question({}).key == "loan_type"


def test_conditional_logic_home_loan_salaried():
    k = keys({"loan_type": "Home Loan", "profile": "Salaried"})
    assert "employer" in k and "itr_income" not in k and "hl_purpose" in k


def test_used_car_includes_new_car_questions_but_not_home_loan():
    k = keys({"loan_type": "Used Car"})
    assert "car_make" in k and "mfg_year" in k and "hl_purpose" not in k


def test_bt_module_loads_only_for_bt_purpose():
    assert "bt_lender" not in keys({"loan_type": "Home Loan", "hl_purpose": "New Purchase"})
    assert "bt_lender" in keys({"loan_type": "Home Loan", "hl_purpose": "BT + Top-up"})


def test_gst_turnover_only_when_gst_registered():
    base = {"loan_type": "Home Loan", "profile": "Self-Employed Business"}
    assert "gst_turnover_12m" not in keys({**base, "gst_registered": "No"})
    assert "gst_turnover_12m" in keys({**base, "gst_registered": "Yes"})


def test_prune_drops_stale_answers_and_unknown_keys():
    c = prune_case({"loan_type": "Personal Loan", "hl_purpose": "New Purchase", "employer": "X", "evil": "1"})
    assert "hl_purpose" not in c and "evil" not in c and c["employer"] == "X"


def test_enough_info_gate():
    assert not enough_info({"loan_type": "Personal Loan"})[0]
    assert enough_info(READY_PL)[0]
    assert not enough_info({**READY_PL, "overdue": ""})[0]


def test_critical_questions_are_asked_before_optional_ones():
    q = next_question({"loan_type": "Personal Loan", "loan_amount": "5 lakh"})
    assert q.key == "pin"                     # name / mobile are optional, so they come later
    assert next_question(READY_PL).key not in ("pin", "age", "cibil")


def test_skip_optional_but_not_critical():
    c, err = apply_answer({"loan_type": "Personal Loan"}, "name", skip=True)
    assert err is None and c["_skipped"] == ["name"]
    _, err = apply_answer({"loan_type": "Personal Loan"}, "cibil", skip=True)
    assert err and "cannot be skipped" in err
    c, _ = apply_answer(c, "name", "Ravi")            # answering later clears the skip
    assert "_skipped" not in c


def test_answer_validation():
    case = {"loan_type": "Home Loan"}
    assert apply_answer(case, "cibil", "950")[1]
    assert apply_answer(case, "cibil", "not checked")[0]["cibil"] == "Not Checked"
    assert apply_answer(case, "pin", "12345")[1]
    assert apply_answer(case, "mobile", "+91 98765 43210")[0]["mobile"] == "9876543210"
    assert apply_answer(case, "age", "15")[1]
    assert apply_answer(case, "loan_amount", "abc")[1]
    assert apply_answer(case, "loan_type", "home loan")[0]["loan_type"] == "Home Loan"
    assert apply_answer(case, "profile", "Retired")[1]
    assert apply_answer(case, "hl_purpose", "Resale")[0]["hl_purpose"] == "Resale"


def test_kinds():
    c = {"loan_type": "Working Capital"}
    assert kind_of(find_question(c, "turnover_3y")) == "money_list"
    assert kind_of(find_question(c, "biz_pin")) == "pin"
    assert kind_of(find_question({}, "existing_emi")) == "money"
    assert validate(find_question(c, "turnover_3y"), "40 lakh / 45 lakh")[1] is None


def test_llm_safe_case_removes_personal_and_unknown_fields():
    c = {"name": "A", "mobile": "9", "prop_owner": "B", "employer": "C", "coapplicant": "D", "cibil": "750",
         "injected": "x", "_skipped": ["name"]}
    assert llm_safe_case(c) == {"cibil": "750"}


def test_num_parser():
    assert num("12.5 lakh") == 1_250_000 and num("1.2 cr") == 12_000_000 and num("₹ 1,30,000") == 130000
    assert num("45k") == 45_000 and num("12 LPA") == 1_200_000 and num("3 lacs") == 300_000
    assert num("Rs. 5,00,000") == 500_000 and num("5 credit") == 5 and num("nothing") is None
    assert num_list("40L / 45L / 50L") == [4e6, 4.5e6, 5e6] and num_list("1,30,000") == [130000]


def test_eligibility_reduces_with_existing_emi():
    base = {"loan_type": "Home Loan", "profile": "Salaried", "net_salary": "100000", "loan_amount": "50 lakh"}
    a = indicative_eligibility({**base, "existing_emi": "0"})["indicative_max_loan"]
    b = indicative_eligibility({**base, "existing_emi": "30000"})["indicative_max_loan"]
    assert a > b > 0


def test_eligibility_uses_the_right_income_field_per_loan_type():
    cases = [
        {"loan_type": "New Car", "profile": "Salaried", "income_amt": "1 lakh"},
        {"loan_type": "Working Capital", "profile": "Self-Employed Business", "turnover_3y": "2 cr / 2.5 cr"},
        {"loan_type": "LRD", "profile": "Self-Employed Business", "monthly_rent": "3 lakh"},
        {"loan_type": "Commercial Vehicle", "profile": "Self-Employed Business", "biz_income": "80k"},
        {"loan_type": "Education Loan", "profile": "Salaried", "coapp_income": "12 lakh"},
        {"loan_type": "Business Loan", "profile": "Self-Employed Business", "net_profit": "18 lakh"},
    ]
    for c in cases:
        e = indicative_eligibility({**c, "loan_amount": "10 lakh"})
        assert e["indicative_max_loan"] and e["indicative_max_loan"] > 0, c["loan_type"]
    assert indicative_eligibility({"loan_type": "Working Capital", "turnover_3y": "2 cr / 2.5 cr"})["indicative_max_loan"] == 5_000_000


def test_eligibility_is_none_not_zero_when_income_missing():
    e = indicative_eligibility({"loan_type": "Machinery Loan", "profile": "Self-Employed Business", "loan_amount": "10 lakh"})
    assert e["indicative_max_loan"] is None and e["covers_request"] is None and "not computed" in e["note"]
    assert indicative_eligibility({"loan_type": "Construction Finance"})["method"].startswith("not computed")


def test_annual_income_is_converted_to_monthly():
    a = indicative_eligibility({"loan_type": "New Car", "income_amt": "12 lakh per annum"})["monthly_income_used"]
    assert a == 100_000
