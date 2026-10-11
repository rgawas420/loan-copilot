"""Fixes for problems the golden-test judge found (baseline run, 2026-10-10)."""
from datetime import datetime, timedelta, timezone

from app.rag import grounding
from app.services import rag_debug


def test_source_labels_become_citations():
    n = grounding.normalize_citations
    assert n("Rahul 9049000381 [Source: S1]") == "Rahul 9049000381 [S1]"
    assert n("(Sources: S1, S3)") == "[S1][S3]" and n("[sources: S2 and S4]") == "[S2][S4]"
    assert n("【S1†L3】") == "[S1]" and n("Source: S1 alone") == "Source: S1 alone"
    assert n("taken from the banker-match data [ S1 ] and [S2 ]") == "taken from the banker-match data [S1] and [S2]"
    refs = [{"ref": "S1", "source": "a"}, {"ref": "S2", "source": "b"}]
    assert grounding.cited_refs(n("| Pankaj | 9923000545 |\n[Source: S2]"), refs) == [refs[1]]


def test_the_prompt_separates_conditions_from_conflicts():
    p = grounding.SYSTEM_PROMPT
    assert "not a conflict" in p and '"COMING"' in p and "newest month applies" in p
    # the newest-sheet rule made the model drop bankers of the (still active) May RM list: it excludes RM lists
    assert "does not\n   apply to RM / banker lists" in p and "Never add, drop, merge or reorder" in p
    assert "no other payouts" in p and "[Source: S1]" in p


def test_source_cards_carry_the_edition_and_the_documents_warnings(monkeypatch):
    from langchain_core.documents import Document
    monkeypatch.setattr(grounding, "_authority", lambda: {"a.xlsx": {
        "priority": 60, "label": "August 2026", "effective_from": "2026-08-01", "effective_until": "2026-09-30",
        "notes": ["OCR, not verified", "expired 2026-09-30"], "source_label": "Payout sheet", "extraction": "ocr"}})
    _, refs = grounding.build_context([Document(page_content="Saraswat | NEW CAR | 1.5%", metadata={"source": "a.xlsx"}),
                                      Document(page_content="other", metadata={"source": "b.xlsx"})])
    assert refs[0]["edition"] == "August 2026" and refs[0]["warnings"] == ["OCR, not verified", "expired 2026-09-30"]
    assert "warnings" not in refs[1]                                   # no knowledge-base metadata: nothing to warn


def test_file_names_with_underscores_match_the_expected_document():
    assert rag_debug._doc_matches("Pan India RM list Aug", "03/Pan_India_RM_List_Aug_2026_Bank_Wise.xlsx")


def test_banker_passage_says_the_lender_name_was_matched():
    from app.rag.bankers import Banker, Match
    b = Banker.__new__(Banker)
    b.path, b.title = "03/Pan India RM list Aug 2026.xlsx", "Pan India RM list Aug 2026"
    b.row = lambda: "| Bajaj Finserv | Chetan Rathod | 9604454809 |"
    m = Match.__new__(Match)
    m.top, m.lenders_filter, m.loan_type, m.city, m.total = [b], ["Bajaj Finserv"], "Home Loan", "Pune", 63
    text = m.documents()[0].page_content
    assert "matched to Bajaj Finserv" in text and "bankers for the lender asked about" in text


def test_only_stale_runs_are_marked_interrupted():
    rag_debug.init_db()
    now = datetime.now(timezone.utc)
    with rag_debug.closing(rag_debug._conn()) as c:
        ids = [c.execute("INSERT INTO rag_eval_runs(started, trigger, status, heartbeat) VALUES (?,?,?,?)",
                         (start.isoformat(timespec="seconds"), "test", "running",
                          beat.isoformat(timespec="seconds") if beat else None)).lastrowid
               for start, beat in ((now - timedelta(hours=3), now - timedelta(minutes=2)),     # a script's run, alive
                                   (now - timedelta(hours=3), now - timedelta(hours=1)),       # died an hour ago
                                   (now - timedelta(minutes=5), None))]                         # just started
        c.commit()
    rag_debug.init_db()
    with rag_debug.closing(rag_debug._conn()) as c:
        status = [c.execute("SELECT status FROM rag_eval_runs WHERE id=?", (i,)).fetchone()[0] for i in ids]
        c.execute(f"DELETE FROM rag_eval_runs WHERE id IN ({','.join('?' * len(ids))})", ids)
        c.commit()
    assert status == ["running", "interrupted", "running"]
