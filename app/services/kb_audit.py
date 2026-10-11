"""Knowledge-base audit log: every change to what the copilot knows, who made it and when.

Admin actions (upload, activate, reject, archive, restore, delete, rollback, merge, verify, metadata and priority
changes) are recorded by the code that performs them; the sync records what it processed or removed (actor
"system" for folder changes found at startup). Kept in the knowledge-base index database, next to the documents
and versions it describes; entries are never edited or deleted by the app.
"""
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, path TEXT,
    doc_id TEXT, version INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS kb_audit_doc ON kb_audit(doc_id);
"""
ACTIONS = {
    "upload": "Uploaded", "add": "Added as a new document", "activate": "Activated a new version",
    "reject": "Rejected an upload", "process": "Processed", "remove": "Removed from the knowledge base",
    "archive": "Archived", "restore": "Restored", "delete": "Deleted", "rollback": "Rolled back",
    "merge": "Merged duplicates", "verify": "Verified text", "unverify": "Removed verified text",
    "metadata": "Changed metadata", "authority": "Changed source priority",
    "duplicate": "Skipped a duplicate file",
    "alias": "Changed a name alias",
    "facts": "Changed policy facts",
}


def _db():
    from app.services.knowledge_base import _db
    return _db()


def record(action: str, actor: str = "", path: str = "", doc_id: Optional[str] = None, version: Optional[int] = None,
           detail: str = "", db=None):
    """Add one entry. Pass `db` when the caller holds an open write transaction on the index database."""
    if db is None:
        with closing(_db()) as own:
            record(action, actor, path, doc_id, version, detail, own)
            own.commit()
        return
    if doc_id is None and path:
        r = db.execute("SELECT doc_id FROM kb_docs WHERE path=?", (path,)).fetchone()
        doc_id = r[0] if r else None
    db.execute("INSERT INTO kb_audit(ts, actor, action, path, doc_id, version, detail) VALUES (?,?,?,?,?,?,?)",
               (datetime.now(timezone.utc).isoformat(timespec="seconds"), actor or "system", action, path or "",
                doc_id, version, (detail or "")[:1000]))


def listing(limit: int = 100, offset: int = 0, q: str = "", action: str = "", doc_id: str = "") -> dict:
    where, args = [], []
    if action:
        where.append("action=?")
        args.append(action)
    if doc_id:
        where.append("doc_id=?")
        args.append(doc_id)
    if q:
        where.append("(path LIKE ? OR detail LIKE ? OR actor LIKE ?)")
        args += [f"%{q}%"] * 3
    sql = (" WHERE " + " AND ".join(where)) if where else ""
    with closing(_db()) as db:
        total = db.execute(f"SELECT COUNT(*) FROM kb_audit{sql}", args).fetchone()[0]
        rows = db.execute(f"SELECT * FROM kb_audit{sql} ORDER BY id DESC LIMIT ? OFFSET ?",
                          (*args, max(1, min(limit, 500)), max(0, offset))).fetchall()
    return {"total": total, "events": [{**dict(r), "label": ACTIONS.get(r["action"], r["action"])} for r in rows],
            "actions": ACTIONS}
