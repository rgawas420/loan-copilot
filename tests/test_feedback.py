"""Partner feedback on answers: linked to the answer's trace, reviewed by admins, turned into golden tests."""
import app.rag.chat as chat
from app.services import accounts, rag_debug
from tests.test_chat_stream import DOCS, FakeLLM, _kb, _user


def _chat_with_answer(monkeypatch):
    """A saved chat whose answer was streamed for message a1 (so the trace is linked to it)."""
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": FakeLLM())
    _kb(monkeypatch, DOCS)
    c, u = _user()
    conv = c.post("/api/conversations", json={"title": "F", "data": {}}).json()
    r = c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?",
                                         "conversation_id": conv["id"], "message_id": "a1"})
    assert r.status_code == 200
    data = {"messages": [{"id": "q0", "role": "user", "text": "Hi"}, {"id": "a0", "role": "assistant", "text": "Hello!"},
                         {"id": "q1", "role": "user", "text": "Axis home loan code?"},
                         {"id": "a1", "role": "assistant", "text": "Based on the documents, **Axis** code is 6159MUM."}]}
    assert c.patch(f"/api/conversations/{conv['id']}", json={"data": data}).status_code == 200
    return c, u, conv


def test_a_dislike_keeps_the_answer_its_trace_and_the_reason(monkeypatch):
    c, u, conv = _chat_with_answer(monkeypatch)
    t = rag_debug.trace_for_message(u["id"], conv["id"], "a1")
    assert t and t["question"] == "Axis home loan code?"

    r = c.post("/api/feedback", json={"conversation_id": conv["id"], "message_id": "a1", "rating": "down",
                                      "reason": "wrong_figure", "comment": "Code is 6159MUM2 now"})
    assert r.status_code == 200
    admin, au = _user()
    accounts.update_user(au["id"], -1, role="admin")
    items = admin.get("/api/admin/rag/feedback?status=new").json()["items"]
    f = next(x for x in items if x["conversation_id"] == conv["id"])
    assert f["rating"] == "down" and f["reason"] == "wrong_figure" and f["comment"] == "Code is 6159MUM2 now"
    assert f["question"] == "Axis home loan code?" and "6159MUM" in f["answer"] and f["trace_id"] == t["id"]
    assert f["history"] == "Hi" and f["reason_label"].startswith("Wrong figure")

    # changing the mind: a like clears the reason; clearing the rating removes an unreviewed item
    c.post("/api/feedback", json={"conversation_id": conv["id"], "message_id": "a1", "rating": "up"})
    f = next(x for x in admin.get("/api/admin/rag/feedback").json()["items"] if x["conversation_id"] == conv["id"])
    assert f["rating"] == "up" and f["reason"] is None and f["comment"] == ""
    c.post("/api/feedback", json={"conversation_id": conv["id"], "message_id": "a1", "rating": None})
    assert not [x for x in admin.get("/api/admin/rag/feedback").json()["items"] if x["conversation_id"] == conv["id"]]

    assert c.post("/api/feedback", json={"conversation_id": conv["id"], "message_id": "a1", "rating": "down",
                                         "reason": "nonsense"}).status_code == 400


def test_review_and_turn_a_mistake_into_a_golden_test(monkeypatch):
    c, u, conv = _chat_with_answer(monkeypatch)
    c.post("/api/feedback", json={"conversation_id": conv["id"], "message_id": "a1", "rating": "down",
                                  "reason": "wrong_figure"})
    admin, au = _user()
    assert admin.get("/api/admin/rag/feedback").status_code == 403                 # admins only
    accounts.update_user(au["id"], -1, role="admin")
    fid = next(x["id"] for x in admin.get("/api/admin/rag/feedback").json()["items"] if x["conversation_id"] == conv["id"])

    r = admin.put(f"/api/admin/rag/feedback/{fid}", json={"status": "confirmed", "note": "Code changed in Sept list"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["reviewer"] == "S"
    g = admin.post(f"/api/admin/rag/feedback/{fid}/golden", json={
        "question": "Axis home loan code?", "reference_answer": "Axis Bank home loan code: 6159MUM2",
        "expected_answer": "6159MUM2", "category": "code"}).json()
    assert g["reference_answer"] == "Axis Bank home loan code: 6159MUM2" and g["history"] == "Hi"
    assert g["approved_by"] == "S" and f"from feedback #{fid}" in g["notes"]
    f = next(x for x in admin.get("/api/admin/rag/feedback?status=golden").json()["items"] if x["id"] == fid)
    assert f["golden_id"] == g["id"] and f["review_note"] == "Code changed in Sept list"
    assert admin.get("/api/admin/rag/feedback").json()["counts"]["golden"]["down"] >= 1
    assert admin.put("/api/admin/rag/feedback/999999", json={"status": "dismissed"}).status_code == 404
    rag_debug.golden_delete(g["id"])
