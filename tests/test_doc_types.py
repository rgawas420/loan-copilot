"""Document type (never a product) inferred from the name, then the text; used for source authority."""
from app.rag.entities import infer_doc_type
from app.services import kb_versions


def test_type_from_name_then_text():
    assert infer_doc_type("AUG  26 PAYOUT SHEET.xls") == "Payout"
    assert infer_doc_type("Pan India RM list Aug 2026.xlsx") == "Contacts"
    assert infer_doc_type("FAQs Flexi BL.pdf") == "FAQ"
    assert infer_doc_type("IMG-20260309-WA0012.jpg", "HL payout 0.65% ... LAP payout 0.90% ... payout on disbursal") == "Payout"
    assert infer_doc_type("file_1.png", "Min CIBIL 700. FOIR up to 60%. LTV 80%. Eligibility: salaried") == "Eligibility"
    phones = " ".join(f"Name {i}: 98000000{i:02d}" for i in range(6))
    assert infer_doc_type("scan.png", phones) == "Contacts"
    assert infer_doc_type("Home Loan.pdf", "welcome") == "General"          # a product name is not a type


def test_authority_uses_the_type_not_a_product_category():
    doc = {"category": "Home Loan", "doc_type": "Policy", "priority": None}
    assert kb_versions.authority_of(doc)["source_type"] == "OFFICIAL_POLICY"
    assert kb_versions.doc_type_of({"category": "Payout"}) == "Payout"
    assert kb_versions.doc_type_of({"category": "Home Loan"}) == "General"
