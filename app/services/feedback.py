"""Partner feedback on answers, and the admins' review queue.

A like / dislike (with a reason for a dislike) is stored with a snapshot of the question and answer and the id of
the answer's trace, so a reviewer sees exactly what the partner saw and how it was produced (Admin > RAG quality >
Feedback). A confirmed mistake becomes a golden test, so the same mistake is caught before it ships again.

status: new -> confirmed (a real mistake) / dismissed (the answer was right) -> golden (added as a golden test)
"""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import List, Optional

from app.core.config import get_settings
from app.services import db

REASONS = {
    "wrong_figure": "Wrong figure (rate, payout, amount, code)",
    "wrong_contact": "Wrong contact or phone number",
    "outdated": "Outdated information",
    "wrong_lender": "Wrong lender or product",
    "not_found_but_exists": "Said not found, but the information exists",
    "incomplete": "Incomplete or missing details",
    "other": "Something else",
}
STATUSES = ("new", "confirmed", "dismissed", "golden")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn():
    c = sqlite3.connect(get_settings().audit_db, timeout=db.BUSY_TIMEOUT_S)
    c.row_factory = sqlite3.Row
    return db.tune(c)


def init_db():
    get_settings().audit_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(_conn()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS answer_feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            conversation_id TEXT NOT NULL, message_id TEXT NOT NULL, rating TEXT NOT NULL, reason TEXT, comment TEXT,
            question TEXT, answer TEXT, history TEXT, trace_id TEXT, status TEXT NOT NULL DEFAULT 'new',
            reviewer TEXT, review_note TEXT, golden_id INTEGER, created TEXT, updated TEXT,
            UNIQUE(user_id, conversation_id, message_id));
        CREATE INDEX IF NOT EXISTS answer_feedback_status ON answer_feedback(status, rating, updated);
        """)
        c.commit()


def _snapshot(user_id: int, conversation: dict, message_id: str) -> dict:
    """The question, answer and earlier user questions of the rated message, and its trace."""
    from app.services import rag_debug
    msgs = (conversation.get("data") or {}).get("messages") or []
    idx = next((i for i, m in enumerate(msgs) if str(m.get("id")) == message_id), None)
    answer = str(msgs[idx].get("text") or "") if idx is not None else ""
    users = [str(m.get("text") or "") for m in (msgs[:idx] if idx is not None else []) if m.get("role") == "user"]
    t = rag_debug.trace_for_message(user_id, conversation["id"], message_id)
    question = (t or {}).get("question") or (users[-1] if users else "")
    earlier = users[:-1] if users and users[-1] == question else users
    return {"question": question[:2000], "answer": (answer or ((t or {}).get("data") or {}).get("answer") or "")[:20000],
            "history": "\n".join(q.replace("\n", " ")[:500] for q in earlier[-4:]), "trace_id": (t or {}).get("id")}


def save(user_id: int, conversation: dict, message_id: str, rating: Optional[str], reason: Optional[str] = None,
         comment: str = "") -> Optional[dict]:
    """Record (or update, or with rating None remove) a partner's rating of one answer."""
    if reason and reason not in REASONS:
        raise ValueError("Unknown reason.")
    cid = conversation["id"]
    with closing(_conn()) as c:
        if rating is None:
            c.execute("DELETE FROM answer_feedback WHERE user_id=? AND conversation_id=? AND message_id=? AND status='new'",
                      (user_id, cid, message_id))
            c.commit()
            return None
        snap = _snapshot(user_id, conversation, message_id)
        reason = reason if rating == "down" else None
        c.execute("""INSERT INTO answer_feedback(user_id, conversation_id, message_id, rating, reason, comment, question,
                         answer, history, trace_id, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(user_id, conversation_id, message_id) DO UPDATE SET rating=excluded.rating,
                         reason=CASE WHEN excluded.rating='down' THEN COALESCE(excluded.reason, answer_feedback.reason) END,
                         comment=CASE WHEN excluded.rating='down'
                                      THEN COALESCE(NULLIF(excluded.comment, ''), answer_feedback.comment) ELSE '' END,
                         question=excluded.question, answer=excluded.answer, history=excluded.history,
                         trace_id=excluded.trace_id, updated=excluded.updated""",
                  (user_id, cid, message_id, rating, reason, (comment or "").strip()[:1000], snap["question"],
                   snap["answer"], snap["history"], snap["trace_id"], _now(), _now()))
        c.commit()
        return dict(c.execute("SELECT * FROM answer_feedback WHERE user_id=? AND conversation_id=? AND message_id=?",
                              (user_id, cid, message_id)).fetchone())


def items(status: str = "", rating: str = "", limit: int = 200) -> List[dict]:
    sql = ("SELECT f.*, u.email FROM answer_feedback f LEFT JOIN users u ON u.id = f.user_id WHERE 1=1")
    args: list = []
    if status:
        sql += " AND f.status=?"
        args.append(status)
    if rating:
        sql += " AND f.rating=?"
        args.append(rating)
    sql += " ORDER BY f.updated DESC LIMIT ?"
    args.append(limit)
    with closing(_conn()) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    for r in rows:
        r["reason_label"] = REASONS.get(r.get("reason") or "", "")
    return rows


def counts() -> dict:
    with closing(_conn()) as c:
        rows = c.execute("SELECT status, rating, COUNT(*) n FROM answer_feedback GROUP BY status, rating").fetchall()
    out = {s: {"up": 0, "down": 0} for s in STATUSES}
    for r in rows:
        out.setdefault(r["status"], {"up": 0, "down": 0})[r["rating"]] = r["n"]
    return out


def get(fid: int) -> Optional[dict]:
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM answer_feedback WHERE id=?", (fid,)).fetchone()
    return dict(r) if r else None


def review(fid: int, status: str, reviewer: str, note: str = "", golden_id: Optional[int] = None) -> dict:
    if status not in STATUSES:
        raise ValueError("Unknown status.")
    with closing(_conn()) as c:
        n = c.execute("UPDATE answer_feedback SET status=?, reviewer=?, review_note=?, golden_id=COALESCE(?, golden_id), "
                      "updated=? WHERE id=?", (status, reviewer, (note or "").strip()[:2000], golden_id, _now(), fid)).rowcount
        c.commit()
    if not n:
        raise LookupError("Feedback not found.")
    return get(fid)


def to_golden(fid: int, item: dict, reviewer: str) -> dict:
    """Add the rated question as a golden test (the reviewer supplies the correct reference answer)."""
    from app.services import rag_debug
    f = get(fid)
    if f is None:
        raise LookupError("Feedback not found.")
    g = rag_debug.golden_save({"question": f["question"], "history": f.get("history") or "", "approved_by": reviewer,
                               **{k: v for k, v in item.items() if v not in (None, "")},
                               "notes": "; ".join(x for x in (f"from feedback #{fid}", item.get("notes") or "") if x)})
    review(fid, "golden", reviewer, f.get("review_note") or "", g["id"])
    return g
