"""Streaming chat answers, attachments, feedback, and pinning / archiving chats."""
import json
import uuid

from fastapi.testclient import TestClient
from langchain_core.documents import Document

import app.rag.chat as chat
import app.rag.retrieval as retrieval
from app.main import app
from app.services import accounts


class FakeLLM:
    """Streams a fixed answer in pieces and records the prompt it was given."""

    def __init__(self, pieces=("Based on ", "the documents, ", "**Axis** code is 6159MUM.")):
        self.pieces, self.prompts = pieces, []

    def stream(self, prompt):
        self.prompts.append(prompt if isinstance(prompt, str) else "\n".join(m.content for m in prompt))
        for p in self.pieces:
            yield type("Chunk", (), {"content": p})()


DOCS = [Document(page_content="Source: Code Book-5.xlsx | Sheet: Codes\nBANK NAME: Axis Bank | HL: 6159MUM",
                 metadata={"source": "Code Book-5.xlsx", "section": "Sheet: Codes", "corpus": "urban_money"}, id="um-x-1")]


def _kb(monkeypatch, docs):
    """The retrieval pipeline runs for real; only the vector and keyword searches return these documents."""
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: [(d, 0.8) for d in docs])
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: [])


def _user():
    c = TestClient(app)
    r = c.post("/api/auth/register", json={"email": f"s-{uuid.uuid4().hex[:8]}@example.com", "name": "S",
                                           "password": "correct-horse"})
    return c, r.json()["user"]


def _events(text):
    out = []
    for block in text.strip().split("\n\n"):
        kind = block.split("\n")[0].removeprefix("event: ")
        out.append((kind, json.loads(block.split("\n", 1)[1].removeprefix("data: "))))
    return out


def test_stream_sends_steps_tokens_then_sources(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    _kb(monkeypatch, DOCS)
    c, u = _user()
    accounts.update_user(u["id"], -1, role="admin")                       # sources are shown to admins only
    r = c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ev = _events(r.text)
    kinds = [k for k, _ in ev]
    assert kinds[:3] == ["status", "status", "status"] and kinds[-4:] == ["sources", "state", "usage", "done"]
    assert [p["text"] for k, p in ev if k == "status"] == ["Searching knowledge base", "Reading 1 relevant document",
                                                            "Preparing answer"]
    tokens = [p for k, p in ev if k == "token"]
    assert len(tokens) == 3 and "".join(tokens) == "Based on the documents, **Axis** code is 6159MUM."
    src = dict(ev)["sources"][0]
    assert src["source"] == "Code Book-5.xlsx" and src["section"] == "Sheet: Codes"
    assert src["excerpt"] == "BANK NAME: Axis Bank | HL: 6159MUM"          # header line dropped
    assert "um-x-1" not in r.text                                          # no internal ids reach the browser
    assert "Markdown" in llm.prompts[0]


def test_stream_with_nothing_found_does_not_call_the_model(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    _kb(monkeypatch, [])
    c, _ = _user()
    ev = _events(c.post("/api/chat/stream", json={"mode": "kb", "question": "unknown thing"}).text)
    assert ("token", chat.NOTHING_FOUND) in ev and llm.prompts == []


def test_stream_needs_login_and_reports_missing_key():
    assert TestClient(app).post("/api/chat/stream", json={"mode": "kb", "question": "hello"}).status_code == 401
    c, _ = _user()
    ev = _events(c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?"}).text)
    assert ev[-1][0] == "error" and "GROQ_API_KEY" in ev[-1][1]["message"]


def test_continue_and_attachments_reach_the_prompt(monkeypatch):
    llm = FakeLLM(("continued",))
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    _kb(monkeypatch, DOCS)
    c, _ = _user()
    att = c.post("/api/attachments", files={"file": ("sanction.txt", b"Sanction letter: tenure 20 years, ROI 8.6%")}).json()
    assert att["name"] == "sanction.txt" and att["chars"] > 10
    c.post("/api/chat/stream", json={"mode": "kb", "question": "What tenure?", "attachment_ids": [att["id"]],
                                     "partial": "The tenure in the letter is"})
    prompt = llm.prompts[-1]
    assert "[A1] sanction.txt" in prompt and "ROI 8.6%" in prompt
    assert "WAS CUT OFF" in prompt and prompt.rstrip().endswith("The tenure in the letter is")

    other, _ = _user()                                     # someone else's attachment id is ignored
    n = len(llm.prompts)
    r = other.post("/api/chat/stream", json={"mode": "kb", "question": "What tenure?", "attachment_ids": [att["id"]]})
    # without the attachment there is no evidence of a tenure, so the model is not asked at all
    from app.rag.followup import ASK_WHICH          # without the file, "What tenure?" names no lender: asked back
    tokens = [d for k, d in _events(r.text) if k == "token"]
    assert tokens in ([chat.NOTHING_FOUND], [ASK_WHICH]) and len(llm.prompts) == n
    assert other.delete(f"/api/attachments/{att['id']}").status_code == 404
    assert c.delete(f"/api/attachments/{att['id']}").status_code == 200


def test_attachment_validation():
    c, _ = _user()
    assert c.post("/api/attachments", files={"file": ("song.mp3", b"\x00\x01")}).status_code == 400
    assert c.post("/api/attachments", files={"file": ("empty.txt", b"")}).status_code == 400
    assert TestClient(app).post("/api/attachments", files={"file": ("a.txt", b"hi")}).status_code == 401


def test_pin_archive_and_rename_keep_history_order():
    c, _ = _user()
    a = c.post("/api/conversations", json={"title": "A", "data": {}}).json()
    b = c.post("/api/conversations", json={"title": "B", "data": {}}).json()
    c.patch(f"/api/conversations/{b['id']}", json={"data": {"messages": []}})          # B is newest
    assert c.patch(f"/api/conversations/{a['id']}", json={"pinned": True}).json()["pinned"] is True
    listed = c.get("/api/conversations").json()["conversations"]
    assert [x["title"] for x in listed] == ["A", "B"] and listed[0]["pinned"]          # pinned first

    renamed = c.patch(f"/api/conversations/{b['id']}", json={"title": "B2"}).json()
    assert renamed["updated"] == listed[1]["updated"]                                   # rename does not bump

    c.patch(f"/api/conversations/{b['id']}", json={"archived": True})
    assert [x["title"] for x in c.get("/api/conversations").json()["conversations"]] == ["A"]
    assert [x["title"] for x in c.get("/api/conversations?archived=true").json()["conversations"]] == ["B2"]
    c.patch(f"/api/conversations/{b['id']}", json={"archived": False})
    assert len(c.get("/api/conversations").json()["conversations"]) == 2


def test_feedback_is_logged_for_own_chats_only():
    c, u = _user()
    conv = c.post("/api/conversations", json={"title": "F", "data": {}}).json()
    assert c.post("/api/feedback", json={"conversation_id": conv["id"], "message_id": "m1", "rating": "up"}).status_code == 200
    assert any(a["action"] == "feedback" and a["detail"].startswith("up") for a in accounts.recent_activity(user_id=u["id"]))
    other, _ = _user()
    assert other.post("/api/feedback", json={"conversation_id": conv["id"], "message_id": "m1", "rating": "down"}).status_code == 404


def _png(text_free=True):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "PNG")
    return buf.getvalue()


def test_image_is_read_by_the_vision_model_kept_and_shown_to_its_owner_only(monkeypatch):
    from app.services import vision
    monkeypatch.setattr(vision, "read_image", lambda data: "| | Salary > ₹75K |\n|---|---|\n| FOIR | 70% |\n\n## What this image shows\nA rate card.")
    c, _ = _user()
    att = c.post("/api/attachments", files={"file": ("rates.png", _png())}).json()
    assert att["kind"] == "image" and att["read_with"] == "vision"
    img = c.get(f"/api/attachments/{att['id']}/file")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    other, _ = _user()
    assert other.get(f"/api/attachments/{att['id']}/file").status_code == 404

    llm = FakeLLM(("The card sets FOIR at **70%** ", "for salaries above ₹75K [A1]."))
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    called = []
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: called.append(q) or [])
    ev = _events(c.post("/api/chat/stream", json={"mode": "kb", "question": "summarize this image",
                                                  "attachment_ids": [att["id"]]}).text)
    assert called == []                                        # a question about the upload does not search the KB
    assert [p["text"] for k, p in ev if k == "status"][0] == "Looking at your upload"
    src = dict(ev)["sources"]
    assert [(s["ref"], s["title"], s["kind"], s["attachment_id"]) for s in src] == [("A1", "rates.png", "image", att["id"])]
    assert "verification" not in dict(ev)                      # 70% and ₹75K are in the transcription
    assert "[A1] rates.png (image, transcribed)" in llm.prompts[0]

    assert c.delete(f"/api/attachments/{att['id']}").status_code == 200
    assert c.get(f"/api/attachments/{att['id']}/file").status_code == 404


def test_image_falls_back_to_ocr_and_rejects_unreadable(monkeypatch):
    from app.services import attachments as att_mod, vision
    monkeypatch.setattr(vision, "read_image", lambda data: None)
    monkeypatch.setattr(att_mod, "extract", lambda path: [type("P", (), {"label": "OCR", "context": "", "text": "LAP ROI 9.5%", "rows": []})()])
    c, _ = _user()
    att = c.post("/api/attachments", files={"file": ("poster.jpg", _png())}).json()
    assert att["read_with"] == "ocr" and att["chars"] > 5
    monkeypatch.setattr(att_mod, "extract", lambda path: [])
    r = c.post("/api/attachments", files={"file": ("blank.png", _png())})
    assert r.status_code == 422 and "no readable text" in r.json()["detail"]


def test_users_never_receive_knowledge_base_sources(monkeypatch):
    """File names and passages are internal: only admins get them, in answers, replies and saved chats."""
    from app.services import source_policy
    llm = FakeLLM(("Axis HL code is 6159MUM [S1].",))
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    _kb(monkeypatch, DOCS)
    _user()                                                               # not the first account, so not admin
    c, u = _user()
    assert u["role"] == "user"
    r = c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?"})
    assert dict(_events(r.text))["sources"] == []
    assert "Code Book-5.xlsx" not in r.text and "BANK NAME" not in r.text

    msg = {"role": "assistant", "kind": "answer", "text": "x [S1]",
           "sources": [{"ref": "S1", "source": "Code Book-5.xlsx", "excerpt": "BANK NAME: Axis"},
                       {"ref": "A1", "source": "mine.png", "attachment": True, "excerpt": "my upload"}]}
    res = {"role": "assistant", "kind": "result", "result": {"status": "Strong Match", "citations": [{"source": "x.md"}]}}
    conv = c.post("/api/conversations", json={"title": "t", "data": {"messages": [msg, res]}}).json()
    saved = c.get(f"/api/conversations/{conv['id']}").json()["data"]["messages"]
    assert [s["ref"] for s in saved[0]["sources"]] == ["A1"]              # own upload kept, documents removed
    assert "citations" not in saved[1]["result"]

    admin = {"role": "admin"}
    assert source_policy.sources_for(admin, msg["sources"]) == msg["sources"]


def test_small_talk_is_answered_directly_without_searching(monkeypatch):
    from app.rag import smalltalk
    searched = []
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: searched.append(q) or [])
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: searched.append(q) or [])
    c, _ = _user()
    ev = _events(c.post("/api/chat/stream", json={"mode": "kb", "question": "hi"}).text)
    assert searched == [] and [k for k, _ in ev if k == "status"] == []      # no "Searching knowledge base"
    assert "".join(p for k, p in ev if k == "token") == smalltalk.FALLBACK["greeting"]   # no LLM key in tests
    assert dict(ev)["sources"] == [] and ev[-1][0] == "done"

    assert smalltalk.kind("ok thanks, bye") == "thanks" and smalltalk.kind("hello, what can you do?") == "about"
    for q in ("hi, what is the Axis HL code?", "thanks, and HDFC PL ROI?", "is 411038 serviceable", "what is FOIR", "yes"):
        assert smalltalk.kind(q) is None, q                                  # these still use the knowledge base


def test_public_questions_go_to_the_web_not_the_knowledge_base(monkeypatch):
    from app.core.config import get_settings
    from app.rag import websearch
    assert websearch.is_web_question("give me sbi and axis bank website")
    assert websearch.is_web_question("HDFC customer care number") and websearch.is_web_question("current RBI repo rate")
    for q in ("Urban Money DSA code for Axis", "payout link for HDFC", "Bajaj RM contact website", "is 411038 serviceable for Fibe"):
        assert not websearch.is_web_question(q), q                       # internal data: knowledge base only

    monkeypatch.setattr(get_settings(), "tavily_api_key", "test")
    results = [{"title": "State Bank of India", "url": "https://sbi.co.in/", "content": "Official website of SBI."},
               {"title": "Axis Bank", "url": "https://www.axisbank.com/", "content": "Axis Bank official site."}]
    monkeypatch.setattr(websearch, "search", lambda q, k=6: results)
    searched = []
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: searched.append(q) or [])
    llm = FakeLLM(("SBI: https://sbi.co.in [W1]; ", "Axis Bank: https://www.axisbank.com [W2]; ", "also https://sbi-netbank.help"))
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)
    _user()
    c, u = _user()                                                         # a regular user sees web sources
    ev = _events(c.post("/api/chat/stream", json={"mode": "kb", "question": "give me sbi and axis bank website"}).text)
    assert searched == [] and [p["text"] for k, p in ev if k == "status"][0] == "Searching the web"
    assert [(s["ref"], s["url"]) for s in dict(ev)["sources"]] == [("W1", "https://sbi.co.in/"), ("W2", "https://www.axisbank.com/")]
    assert dict(ev)["verification"]["unsupported"] == ["https://sbi-netbank.help"]   # a look-alike link is flagged
    assert "Official website of SBI" in llm.prompts[0]
