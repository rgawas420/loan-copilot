"""SQLite: audit log, saved cases and the uploaded-policy registry.

The audit log stores NO personal data (no name / mobile / owner names).
Saved cases do hold personal data, so each one is protected by a random access token: only its SHA-256
is stored, and loading or overwriting a case needs the token.
"""
import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app.core.config import get_settings
from app.services import db


class CaseAccessError(Exception):
    pass


def _conn():
    c = sqlite3.connect(get_settings().audit_db, timeout=db.BUSY_TIMEOUT_S)
    c.row_factory = sqlite3.Row
    return db.tune(c)


def init_db():
    get_settings().audit_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(_conn()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, case_id TEXT, loan_type TEXT,
            status TEXT, source TEXT, trace TEXT);
        CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, data TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS policy_docs(sha TEXT PRIMARY KEY, filename TEXT, ext TEXT, lender TEXT,
            loan_type TEXT, chunks INTEGER, ts TEXT);
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(cases)")}
        if "token_hash" not in cols:            # migrate databases created by the first version
            c.execute("ALTER TABLE cases ADD COLUMN token_hash TEXT")
        c.commit()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ audit
def log_recommendation(case_id, loan_type, status, source, trace):
    with closing(_conn()) as c:
        c.execute("INSERT INTO audit(ts,case_id,loan_type,status,source,trace) VALUES (?,?,?,?,?,?)",
                  (_now(), case_id, loan_type, status, source, json.dumps(trace)))
        c.commit()


def recent(limit=50):
    with closing(_conn()) as c:
        rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "trace": json.loads(r["trace"] or "[]")} for r in rows]


# ------------------------------------------------------------------ saved cases
def save_case(case: dict, case_id: str | None = None, token: str | None = None):
    """Create a case (returns a new id + token) or overwrite one the caller holds the token for."""
    with closing(_conn()) as c:
        if case_id:
            r = c.execute("SELECT token_hash FROM cases WHERE case_id=?", (case_id,)).fetchone()
            if r is None or not r["token_hash"] or not token or \
                    not secrets.compare_digest(r["token_hash"], _hash(token)):
                raise CaseAccessError("This saved case cannot be updated without its access link.")
            c.execute("UPDATE cases SET data=?, updated=? WHERE case_id=?", (json.dumps(case), _now(), case_id))
        else:
            case_id, token = secrets.token_hex(8), secrets.token_urlsafe(18)
            c.execute("INSERT INTO cases(case_id,data,updated,token_hash) VALUES (?,?,?,?)",
                      (case_id, json.dumps(case), _now(), _hash(token)))
        c.commit()
    return case_id, token


def load_case(case_id: str, token: str | None):
    with closing(_conn()) as c:
        r = c.execute("SELECT data, token_hash FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if r is None or not r["token_hash"] or not token or not secrets.compare_digest(r["token_hash"], _hash(token)):
        return None
    return json.loads(r["data"])


# ------------------------------------------------------------------ uploaded policies
def get_policy_doc(sha: str):
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM policy_docs WHERE sha=?", (sha,)).fetchone()
    return dict(r) if r else None


def add_policy_doc(sha, filename, ext, lender, loan_type, chunks):
    with closing(_conn()) as c:
        c.execute("INSERT INTO policy_docs(sha,filename,ext,lender,loan_type,chunks,ts) VALUES (?,?,?,?,?,?,?)",
                  (sha, filename, ext, lender, loan_type, chunks, _now()))
        c.commit()


def delete_policy_doc(sha):
    with closing(_conn()) as c:
        c.execute("DELETE FROM policy_docs WHERE sha=?", (sha,))
        c.commit()


def list_policy_docs():
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute("SELECT * FROM policy_docs ORDER BY ts DESC").fetchall()]
