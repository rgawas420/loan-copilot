"""Dated editions of one document (monthly RM lists, payout sheets): the newer edition ranks first and the older
one is labelled in the prompt and on the source card."""
from langchain_core.documents import Document

from app.rag import grounding, retrieval
from app.services import kb_versions
from tests.test_banker_directory import AUG, JUN, _xlsx, folder  # noqa: F401  (fixture)
from app.rag import lookup
from app.services import knowledge_base as kb


def test_family_ignores_the_period_and_numbering():
    assert kb_versions.family("Pan India RM list May -2026.") == kb_versions.family("Pan India RM list Augest 2026")
    assert kb_versions.family("JULY 26 PAYOUT SHEET") == kb_versions.family("AUG  26 PAYOUT SHEET-1") == "payout sheet"
    assert kb_versions.family("Rm List") == kb_versions.family("Rm List Jan-2026.")
    assert kb_versions.family("Slab Book Secured July 2026") != kb_versions.family("Slab Book Unsecured July 2026")


def test_older_edition_is_labelled_in_lookups_and_the_prompt(folder):
    lists = folder / "03 - Banker & RM Lists"
    _xlsx(lists / "Pan India RM list Jun-26.xlsx", JUN)
    _xlsx(lists / "Pan India RM list Aug 2026.xlsx", AUG)
    _xlsx(lists / "Payout Sheet.xlsx", AUG[:1])                         # another document: never compared
    kb.sync()
    jun, aug = "03 - Banker & RM Lists/Pan India RM list Jun-26.xlsx", "03 - Banker & RM Lists/Pan India RM list Aug 2026.xlsx"
    assert kb_versions.older_editions([jun, aug, "03 - Banker & RM Lists/Payout Sheet.xlsx"]) == {jun: "August 2026"}
    assert kb_versions.older_editions([jun]) == {}                      # alone, an older edition is just a document

    docs = lookup.find("hdfc rm list pune", ["HDFC Bank"], []).documents()
    context, refs = grounding.build_context(docs)
    assert "older edition (a newer edition dated August 2026 is also provided)" in context.split("\n\n")[1]
    assert "older edition" not in context.split("\n\n")[0]
    assert refs[0]["edition"] == "August 2026" and not refs[0]["older_edition"] and refs[1]["older_edition"]


def test_a_misspelled_month_is_still_the_newest_edition(folder):
    # The live "Pan India RM list Augest 2026" went undated, so it was labelled older than the June list.
    assert kb_versions.parse_period("Pan India RM list Augest 2026..xlsx").isoformat() == "2026-08-01"
    assert kb_versions.parse_period("Octomber 26 codes").isoformat() == "2026-10-01"
    lists = folder / "03 - Banker & RM Lists"
    _xlsx(lists / "Pan India RM list Jun-26.xlsx", JUN)
    _xlsx(lists / "Pan India RM list Augest 2026.xlsx", AUG)
    kb.sync()
    jun, aug = "03 - Banker & RM Lists/Pan India RM list Jun-26.xlsx", "03 - Banker & RM Lists/Pan India RM list Augest 2026.xlsx"
    assert kb_versions.older_editions([jun, aug]) == {jun: "August 2026"}


def test_versions_registered_without_a_date_are_backfilled(folder):
    _xlsx(folder / "Codes Augest 2026.xlsx", AUG[:1])
    kb.sync()
    from contextlib import closing
    with closing(kb_versions._db()) as db:
        db.execute("UPDATE kb_versions SET effective_from=NULL, label='' WHERE file LIKE '%Augest%'")   # as registered before
        db.commit()
        assert kb_versions.backfill_periods(db) == 1
        assert tuple(db.execute("SELECT effective_from, label FROM kb_versions WHERE file LIKE '%Augest%' AND status='ACTIVE'"
                                ).fetchone()) == ("2026-08-01", "August 2026")
        assert kb_versions.backfill_periods(db) == 0


def test_reranking_demotes_the_older_edition(folder, monkeypatch):
    lists = folder / "03 - Banker & RM Lists"
    _xlsx(lists / "Pan India RM list Jun-26.xlsx", JUN)
    _xlsx(lists / "Pan India RM list Aug 2026.xlsx", AUG)
    kb.sync()
    jun, aug = "03 - Banker & RM Lists/Pan India RM list Jun-26.xlsx", "03 - Banker & RM Lists/Pan India RM list Aug 2026.xlsx"
    same = "HDFC Bank Ajit Pawar 9800000001 Pune home loan"
    cands = {p: retrieval.Candidate(doc=Document(id=p, page_content=same, metadata={"source": p}), key=p, fused=0.1)
             for p in (jun, aug)}
    plan = retrieval.Plan(question="hdfc rm pune", standalone="hdfc rm pune", queries=["hdfc rm pune"])
    retrieval.rerank_and_select(plan, cands)
    assert cands[aug].score > cands[jun].score and cands[jun].newer_edition == "August 2026"
    assert cands[jun].doc.metadata["newer_edition"] == "August 2026"
