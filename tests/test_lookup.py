"""Table lookup (exact rows and counts for list / contact / count questions) and the answer source check rewrite."""
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.rag.chat as chat
import app.rag.retrieval as retrieval
import app.rag.vectorstore as vectorstore
from app.core.config import get_settings
from app.main import app
from app.rag import lookup
from app.services import knowledge_base as kb


class FakeStore:
    def __init__(self):
        self.docs = {}

    def add_documents(self, docs, ids=None):
        self.docs.update(zip(ids, docs))

    def delete(self, ids=None):
        for i in ids:
            self.docs.pop(i, None)


@pytest.fixture
def folder(monkeypatch, tmp_path):
    root = tmp_path / "Urban Money"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "urban_money_dir", root)
    monkeypatch.setattr(get_settings(), "kb_index_db", tmp_path / "kb.db")
    monkeypatch.setattr(get_settings(), "kb_cache_dir", tmp_path / "cache")
    monkeypatch.setattr(vectorstore, "is_pinecone", lambda: True)
    store = FakeStore()
    monkeypatch.setattr(vectorstore, "get_vectorstore", lambda: store)
    from app.services import kb_versions
    kb_versions._invalidate()
    return root


def _xlsx(path: Path, rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "RMs"
    for r in rows:
        ws.append(r)
    wb.save(path)


RM_LIST = [["Bank", "Product", "City", "RM Name", "Mobile"],
           ["Axis Bank", "Personal Loan", "Pune", "Sachin Agarwal", "9800000001"],
           ["HDFC Bank", "Personal Loan", "Pune", "Neha Joshi", "9800000002"],
           ["ICICI Bank", "PL", "Pune", "Amit Kale", "9800000003"],
           ["HDFC Bank", "Home Loan", "Pune", "Ravi Patil", "9800000004"],
           ["Axis Bank", "Personal Loan", "Delhi", "Karan Mehra", "9800000005"],
           ["HDFC Bank", "Business Loan", "Pune", "Sneha Rao", "9800000006"]]


def test_list_question_gets_every_matching_row_and_the_count(folder):
    _xlsx(folder / "Pan India RM List may-2026.xlsx", RM_LIST)
    kb.sync()
    look = lookup.find("list of rm in pune releletd personal loan", [], ["Personal Loan"])
    assert look.total == 3 and look.filters["words"] == ["pune"]          # the typo is not a filter
    rows = "\n".join(look.files[0]["rows"])
    assert all(n in rows for n in ("Sachin Agarwal", "Neha Joshi", "Amit Kale"))          # "PL" counts as personal loan
    assert "Karan Mehra" not in rows and "Ravi Patil" not in rows                         # Delhi, home loan
    text = look.documents()[0].page_content
    assert "TABLE LOOKUP" in text and "Matching rows in this file: 3 (all shown)." in text

    look = lookup.find("HDFC BANK Business Loan rm list in pune", ["HDFC Bank"], ["Business Loan"])
    assert look.total == 1 and "Sneha Rao" in look.files[0]["rows"][0]


def test_count_question_is_counted_in_code(folder):
    _xlsx(folder / "Pan India RM List may-2026.xlsx", RM_LIST)
    kb.sync()
    look = lookup.find("how many records in Pan India RM List may-2026", [], [])
    assert look.count_question and look.total == 6                       # from the file name, every row


def test_product_filter_is_dropped_when_no_row_names_a_product(folder):
    _xlsx(folder / "Bajaj SM contact details.xlsx", [["City", "SM Name", "Mobile"], ["Pune", "Sanket Gaikwad", "9811111111"],
                                                     ["Mumbai", "Rohit Shah", "9822222222"]])
    kb.sync()
    look = lookup.find("Who is the home loan RM for Bajaj Finserv in Pune?", ["Bajaj Finserv"], ["Home Loan"])
    assert look.relaxed == ["product"] and look.total == 1 and "Sanket Gaikwad" in look.files[0]["rows"][0]
    assert "No row names the product" in look.documents()[0].page_content


def test_no_lookup_for_policy_questions_unknown_places_or_removed_files(folder):
    path = folder / "rms.xlsx"
    _xlsx(path, RM_LIST)
    kb.sync()
    assert lookup.find("What is the maximum LTV for HDFC home loan?", ["HDFC Bank"], ["Home Loan"]) is None
    assert lookup.find("rm list in nagpur", [], []) is None                # nothing to filter on: normal search answers
    assert lookup.find("Sachin Agarwal", [], []).total == 1               # a bare name is a row lookup
    path.unlink()
    kb.sync()
    assert lookup.find("Sachin Agarwal", [], []) is None


def test_row_index_is_built_for_files_indexed_before_it_existed(folder):
    _xlsx(folder / "rms.xlsx", RM_LIST)
    kb.sync()
    from contextlib import closing
    with closing(kb._db()) as db:                     # as if synced by a version without the row index
        lookup.remove_rows(db, "rms.xlsx")
        db.execute("DELETE FROM kb_meta WHERE key='rows'")
        db.commit()
    assert lookup.find("Sachin Agarwal", [], []) is None
    r = kb.sync()
    assert r["indexed"] == [] and lookup.find("Sachin Agarwal", [], []).total == 1


# ------------------------------------------------------------------ in the chat
class FakeLLM:
    def __init__(self, pieces, fixed=None):
        self.pieces, self.fixed, self.prompts = pieces, fixed, []

    def stream(self, msgs):
        self.prompts.append("\n".join(m.content for m in msgs))
        for p in self.pieces:
            yield type("Chunk", (), {"content": p})()

    def invoke(self, msgs):
        self.prompts.append("\n".join(m.content for m in msgs))
        return type("Msg", (), {"content": self.fixed})()


def _events(text):
    return [(b.split("\n")[0].removeprefix("event: "), json.loads(b.split("\n", 1)[1].removeprefix("data: ")))
            for b in text.strip().split("\n\n")]


def _client():
    c = TestClient(app)
    c.post("/api/auth/register", json={"email": f"l-{uuid.uuid4().hex[:8]}@example.com", "name": "L",
                                       "password": "correct-horse"})
    return c


def test_chat_answers_a_list_question_from_the_rows(folder, monkeypatch):
    _xlsx(folder / "rms.xlsx", RM_LIST)
    kb.sync()
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: [])     # passage search finds nothing
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: [])
    llm = FakeLLM(["Pune personal loan RMs: Sachin Agarwal, Neha Joshi, Amit Kale [S1]"])
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    ev = _events(_client().post("/api/chat/stream", json={"mode": "kb", "question": "personal loan rm list pune"}).text)
    # A banker question (product + city) is answered from the banker directory built by the sync.
    assert ("status", {"stage": "read", "text": "Matched 3 bankers in Pune"}) in ev
    assert "BANKER MATCH" in llm.prompts[0] and "Neha Joshi" in llm.prompts[0] and "Ravi Patil" not in llm.prompts[0]
    assert not any(k == "verification" for k, _ in ev)

    llm = FakeLLM(["3 rows [S1]"])                       # not a banker question: the exact rows, counted
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    ev = _events(_client().post("/api/chat/stream", json={"mode": "kb", "question": "how many records for pune personal loan"}).text)
    assert ("status", {"stage": "read", "text": "Found 3 matching rows in 1 table"}) in ev
    assert "TABLE LOOKUP" in llm.prompts[0]


def test_answer_failing_the_source_check_is_rewritten_once(folder, monkeypatch):
    _xlsx(folder / "rms.xlsx", RM_LIST)
    kb.sync()
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: [])
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: [])
    llm = FakeLLM(["Sachin Agarwal: 9800000009 [S1]"], fixed="Sachin Agarwal: 9800000001 [S1]")
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    ev = _events(_client().post("/api/chat/stream", json={"mode": "kb", "question": "Sachin Agarwal"}).text)
    assert ("status", {"stage": "check", "text": "Double-checking figures against the sources"}) in ev
    assert ("replace", "Sachin Agarwal: 9800000001 [S1]") in ev
    assert "9800000009" in llm.prompts[1] and not any(k == "verification" for k, _ in ev)

    llm = FakeLLM(["Sachin Agarwal: 9800000009 [S1]"], fixed="Sachin Agarwal: 9800000777 [S1]")   # no better
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    ev = _events(_client().post("/api/chat/stream", json={"mode": "kb", "question": "Sachin Agarwal"}).text)
    assert not any(k == "replace" for k, _ in ev)
    assert ("verification", {"unsupported": ["9800000009"]}) in ev
