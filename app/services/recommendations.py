"""Banker recommendations made in chat, kept as structured records (not only as answer text), so a follow-up such as
"other than above bank" or "why not HDFC?" works from what was actually shown.

One record per customer-profile answer: the customer snapshot, loan type, city, the lenders shown, every lender
shown so far for this customer ("seen", what "other than above" excludes), each lender's eligibility outcome
(app/rag/lender_eligibility.py), the RM lists used and the answer's state. A what-if answer is marked
hypothetical and points at the record it varied (base_id); it never changes that record.

Records belong to one user and one conversation: every read filters by both, so one partner can never reach
another's customers. Without a conversation id (API, golden tests) nothing is stored and follow-ups fall back to
the profile table in the previous answer.
"""
import json
import logging
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import List, Optional

from app.services import accounts

log = logging.getLogger(__name__)
_ready = False


def init_db():
    global _ready
    with closing(accounts._conn()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS recommendations(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            conversation_id TEXT NOT NULL, created TEXT NOT NULL, relation TEXT, base_id TEXT, hypothetical INTEGER,
            question TEXT, loan_type TEXT, city TEXT, profile TEXT, shown TEXT, seen TEXT, evaluations TEXT,
            sources TEXT, state TEXT);
        CREATE INDEX IF NOT EXISTS recommendations_conv ON recommendations(user_id, conversation_id, created);
        """)
        c.commit()
    _ready = True


def _ensure():
    if not _ready:
        init_db()


def _row(r) -> dict:
    d = dict(r)
    for k in ("profile", "shown", "seen", "evaluations", "sources"):
        d[k] = json.loads(d[k] or ("{}" if k == "profile" else "[]"))
    d["hypothetical"] = bool(d["hypothetical"])
    return d


def save(user_id: Optional[int], conversation_id: Optional[str], *, relation: str, question: str, loan_type: str,
         city: str, profile: dict, shown: List[str], seen: List[str], evaluations: List[dict], sources: List[str],
         state: str, base_id: Optional[str] = None, hypothetical: bool = False) -> Optional[str]:
    """Store one recommendation; None when there is no user / conversation to attach it to (or it failed:
    storing never breaks an answer)."""
    if not user_id or not conversation_id:
        return None
    rid = uuid.uuid4().hex
    try:
        _ensure()
        with closing(accounts._conn()) as c:
            c.execute("INSERT INTO recommendations(id, user_id, conversation_id, created, relation, base_id, hypothetical, "
                      "question, loan_type, city, profile, shown, seen, evaluations, sources, state) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, user_id, conversation_id, datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                       relation, base_id, int(hypothetical), question[:2000], loan_type, city,
                       json.dumps(profile, ensure_ascii=False), json.dumps(shown, ensure_ascii=False),
                       json.dumps(seen, ensure_ascii=False), json.dumps(evaluations, ensure_ascii=False, default=str),
                       json.dumps(sources, ensure_ascii=False), state))
            c.commit()
        return rid
    except Exception:
        log.exception("Could not store the recommendation")
        return None


def latest(user_id: Optional[int], conversation_id: Optional[str]) -> Optional[dict]:
    """The conversation's most recent recommendation (this user's only)."""
    if not user_id or not conversation_id:
        return None
    try:
        _ensure()
        with closing(accounts._conn()) as c:
            r = c.execute("SELECT * FROM recommendations WHERE user_id=? AND conversation_id=? "
                          "ORDER BY created DESC LIMIT 1", (user_id, conversation_id)).fetchone()
        return _row(r) if r else None
    except Exception:
        log.exception("Could not read the recommendations")
        return None


def get(rid: str, user_id: int) -> Optional[dict]:
    _ensure()
    with closing(accounts._conn()) as c:
        r = c.execute("SELECT * FROM recommendations WHERE id=? AND user_id=?", (rid, user_id)).fetchone()
    return _row(r) if r else None


def for_conversation(user_id: int, conversation_id: str) -> List[dict]:
    _ensure()
    with closing(accounts._conn()) as c:
        rows = c.execute("SELECT * FROM recommendations WHERE user_id=? AND conversation_id=? ORDER BY created",
                         (user_id, conversation_id)).fetchall()
    return [_row(r) for r in rows]
