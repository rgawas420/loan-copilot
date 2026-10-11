import app.rag.intake as intake
from app.rag.state import Extracted


def test_mobile_is_redacted_before_the_llm():
    text, mobile = intake.redact("Ravi, mob +91 98765-43210, salaried 85k")
    assert mobile == "9876543210" and "98765" not in text and "[mobile]" in text


def test_merge_applies_loan_type_first_and_rejects_invalid_values():
    fields = {"hl_purpose": "Resale", "cibil": "1200", "loan_type": "Home Loan", "net_salary": "85k",
              "profile": "Salaried", "turnover_3y": "1 cr"}
    case, filled, rejected = intake.merge_fields({}, fields)
    assert case["loan_type"] == "Home Loan" and case["hl_purpose"] == "Resale" and case["net_salary"] == "85k"
    assert {r["key"] for r in rejected} == {"cibil", "turnover_3y"}
    assert "cibil" not in case and set(filled) == {"loan_type", "hl_purpose", "net_salary", "profile"}


def test_extract_never_overwrites_typed_answers_or_takes_names(monkeypatch):
    class LLM:
        prompt = ""

        def with_structured_output(self, schema, **kw):
            outer = self

            class R:
                def with_retry(self, **kw): return self

                def invoke(self, p):
                    LLM.prompt = p
                    return Extracted(fields={"loan_type": "Personal Loan", "age": "40", "name": "Ravi",
                                             "employer": "Acme", "net_salary": "60000"})
            return R()

    monkeypatch.setattr(intake, "get_llm", lambda kind="fast": LLM())
    case, filled, _ = intake.extract("PL for Ravi 9876543210, 40 yrs, 60k salary", {"age": "35"})
    assert case["age"] == "35" and "name" not in case and "employer" not in case
    assert case["mobile"] == "9876543210" and "9876543210" not in LLM.prompt
