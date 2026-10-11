"""A question naming a bank also searches that bank's documents directly, so they cannot be crowded out of the
candidates by other banks' similar text; the recommendation workflow uses the same retrieval pipeline."""
from app.rag import retrieval
from app.rag import workflow as wf
from app.services import knowledge_base as kb
from tests.test_banker_directory import folder  # noqa: F401  (fixture)


def test_bank_documents_are_searched_directly(folder, monkeypatch):
    for i in range(30):
        (folder / f"Lender {i:02d} home loan policy.md").write_text(
            f"Lender {i:02d} home loan eligibility policy: minimum income, maximum tenure, home loan eligibility.",
            encoding="utf-8")
    (folder / "HDFC Bank home loan policy.md").write_text("HDFC Bank home loan eligibility: salaried, CIBIL 750.",
                                                          encoding="utf-8")
    kb.sync()
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: [])
    plan = retrieval.Plan(question="hdfc home loan eligibility policy", standalone="hdfc home loan eligibility policy",
                          queries=["home loan eligibility policy"], banks=["HDFC Bank"])
    found = {c.doc.metadata["source"] for c in retrieval.gather(plan, 5).values()}
    assert "HDFC Bank home loan policy.md" in found
    unscoped = {d.metadata["source"] for d in kb.lexical_search("home loan eligibility policy", 5)}
    assert "HDFC Bank home loan policy.md" not in unscoped                   # what the bank filter used to see


def test_workflow_search_uses_the_retrieval_pipeline(monkeypatch):
    seen = {}

    def fake(q, history=None, loan_type=None, use_llm=True, plan=None):
        seen.update(q=q, loan_type=loan_type, use_llm=use_llm)
        return type("R", (), {"docs": []})()

    monkeypatch.setattr(retrieval, "retrieve", fake)
    assert wf.search("Home Loan policy eligibility", "Home Loan", ["Sample Lender A"]) == []
    assert seen == {"q": "Home Loan policy eligibility Sample Lender A", "loan_type": "Home Loan", "use_llm": False}
