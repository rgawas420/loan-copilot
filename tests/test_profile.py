"""Customer -> banker recommendations: detection, the app's eligibility figures and tables, and the answer path
(app-chosen bankers, focused policy query, format rules, never a bare "not found")."""
from langchain_core.documents import Document

import pytest
import app.rag.bankers as bankers
import app.rag.chat as chat
import app.rag.profile as profile
import app.rag.retrieval as retrieval

PROFILE_Q = ("The customer is a salaried individual with a monthly salary of ₹1,45,000. He is looking for a home "
             "loan of ₹98,00,000 for a property in Pune. CIBIL Score: 845. Existing EMIs: None. "
             "Give banker list in Pune")
FIELDS = {"loan_type": "Home Loan", "employment": "Salaried", "monthly_income": "₹1,45,000",
          "loan_amount": "₹98,00,000", "cibil": "845", "existing_emi": "None", "property_location": "Pune"}


def test_detects_a_described_customer_who_needs_lenders():
    assert profile.detect(PROFILE_Q)
    assert profile.detect("Salaried, 32, 85k in hand, wants 40 lakh HL in Pune, CIBIL 760, no EMIs. Which banks should I approach?")


def test_ordinary_questions_are_not_profiles():
    for q in ["home loan RM list Pune", "who is the RM for Bajaj in Pune", "explain payout capping and clawback rules",
              "What is the minimum CIBIL score and salary for HDFC home loan?", "give me name prime minister of india"]:
        assert not profile.detect(q), q


def test_eligibility_is_computed_by_the_app():
    c = profile.compute(FIELDS)
    assert c["computed"] and c["existing_emi"] == 0 and c["existing_emi_known"]
    assert c["emi_capacity"] == 79750                       # 55% FOIR (app policy for home loans)
    assert c["emi_requested"] == 80570                      # ₹98 lakh at the app default 8.75% for 25 years
    assert c["covers"] is False and c["roi_source"] == "app default"
    assert "| EMI Capacity | ₹79,750 |" in c["table"] and "| Requested Loan EMI | ₹80,570 |" in c["table"]
    assert c["verdict"].startswith("above indicative eligibility by ₹99,749")


def test_the_customers_rate_and_tenure_are_used_when_given():
    c = profile.compute({**FIELDS, "interest_rate": "8.5%", "tenure": "30 years"})
    assert (c["roi"], c["roi_source"], c["years"], c["tenure_source"]) == (8.5, "customer", 30, "customer")
    assert "8.5% (customer)" in c["table"] and "30 years (customer)" in c["table"]


def test_a_missing_existing_emi_is_not_assumed():
    c = profile.compute({k: v for k, v in FIELDS.items() if k != "existing_emi"})
    assert not c["existing_emi_known"]
    assert "Not provided (not deducted)" in c["table"] and "EMI Capacity (before existing EMIs)" in c["table"]
    assert "existing EMIs were not provided" in c["verdict"]


def test_ltv_needs_a_property_value():
    assert "ltv" not in profile.compute(FIELDS)
    c = profile.compute({**FIELDS, "property_value": "1.25 crore"})
    assert c["ltv"] == 0.784 and "78.4%" in c["table"]


def test_eligibility_needs_income():
    c = profile.compute({"loan_type": "Home Loan", "loan_amount": "50 lakh"})
    assert c["computed"] is False and c["reason"] == "monthly income not provided"


def test_profile_table_shows_core_fields_and_only_given_extras():
    t = profile.Profile(question="", fields={"loan_type": "Home Loan", "property_location": "Pune", "age": "32"}).profile_table()
    assert "| Monthly Income | Not provided |" in t and "| Property Location | Pune |" in t and "| Age | 32 |" in t
    assert "Down Payment" not in t


def test_location_is_the_property_else_the_job():
    assert profile.Profile(question="", fields={"job_location": "Mumbai", "property_location": "Pune"}).city == "Pune"
    assert profile.Profile(question="", fields={"job_location": "Mumbai"}).city == "Mumbai"


def test_extraction_accepts_a_string_for_other():
    assert profile.ExtractedProfile(other="age 32; 5 years in job", cibil=845).model_dump()["other"] == ["age 32", "5 years in job"]


def _profile():
    p = profile.Profile(question=PROFILE_Q, fields=dict(FIELDS))
    p.calc = profile.compute(p.fields)
    return p


AUG = "03 - Banker & RM Lists/Pan India RM List Aug 2026.xlsx"


def _match(rows):
    return bankers.rank(bankers.Match(loan_type="Home Loan", city="Pune"), rows, {AUG: {"title": "Pan India RM List Aug 2026"}}, {})


def test_profile_uses_the_app_bankers_and_a_focused_policy_query(monkeypatch):
    queries = []
    policy = Document(page_content="Source: HL policy.pdf\nHome loan FOIR up to 60%.", metadata={"source": "HL policy.pdf",
                                                                                                "corpus": "urban_money"}, id="p1")
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt: queries.append(q) or [(policy, 0.8)])
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: [])
    monkeypatch.setattr(bankers, "find", lambda lt, city: _match([
        (AUG, "Lender Name: Axis Bank | Type of Loan: Home Loan | City*: Pune | Bank/ NBFC RM Name: Ajit | "
              "Bank/ NBFC RM Contact No: 8983240885")]))
    prep = chat.Prepared("kb", PROFILE_Q, {}, {}, None, [], [], "", use_llm=False, profile=_profile())
    assert queries and all("Pune" in q and "Home Loan" in q and "1,45,000" not in q for q in queries)   # never the paragraph
    prompt = "\n".join(m.content for m in prep.messages)
    assert "| 1 | Axis Bank | Ajit [S1] | 8983240885 | Strong |" in prompt                    # the bankers' list is S1
    assert "### Eligibility Snapshot" in prompt and "| EMI Capacity | ₹79,750 |" in prompt
    assert "| Monthly Income | ₹1,45,000 |" in prompt and PROFILE_Q in prompt
    assert prep.refs[0]["source"] == AUG
    # the app's figures and the bankers' numbers pass the source check
    assert prep.finish("EMI capacity ₹79,750; Ajit 8983240885 [S1]")["unsupported_figures"] == []


def test_profile_without_bankers_is_still_answered(monkeypatch):
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt: [])
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: [])
    monkeypatch.setattr(bankers, "find", lambda lt, city: _match([]))
    prep = chat.Prepared("kb", PROFILE_Q, {}, {}, None, [], [], "", use_llm=False, profile=_profile())
    assert not prep.not_found
    assert bankers.NOT_FOUND in "\n".join(m.content for m in prep.messages)


def test_profile_without_a_city_asks_for_it_and_searches_nothing(monkeypatch):
    monkeypatch.setattr(retrieval, "vector_hits", lambda *a, **k: pytest.fail("searched"))
    monkeypatch.setattr(bankers, "find", lambda *a, **k: pytest.fail("bankers matched without a city"))
    p = profile.Profile(question=PROFILE_Q, fields={k: v for k, v in FIELDS.items() if k != "property_location"})
    p.calc = profile.compute(p.fields)
    prep = chat.Prepared("kb", PROFILE_Q, {}, {}, None, [], [], "", use_llm=False, profile=p)
    assert prep.direct.startswith("I can list the bankers") and "the city where the property is" in prep.direct
    assert "| EMI Capacity | ₹79,750 |" in prep.direct and "the loan type" not in prep.direct
    assert prep.finish(prep.direct) == {"sources": [], "unsupported_figures": [], "contact_mismatches": [],
                                        "invalid_citations": [], "not_found": False}


def test_bankers_table_states_how_it_was_ranked(monkeypatch):
    key = "RANKING BASIS (state it in one short line under the bankers table)"
    assert "check each lender's program status" in dict(_profile().sections(_match([])))[key]   # sample HL policies
    import app.rag.policies as policies
    monkeypatch.setattr(policies, "programs_for", lambda lt: [])
    assert "not by each lender's eligibility rules" in dict(_profile().sections(_match([])))[key]


def test_implausible_income_is_flagged_not_used_silently():
    c = profile.compute({"loan_type": "Home Loan", "monthly_income": "₹90,00,000", "loan_amount": "1 crore"})
    assert c["warnings"] and "unusually high" in c["verdict"]
