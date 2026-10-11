"""SQLite: user accounts, login sessions, private conversations and the activity log.

Passwords are hashed with scrypt (salted, standard library). Sessions are random tokens held in an HttpOnly
cookie; only their SHA-256 is stored. Every conversation query is scoped to its owner's user id, so one user
can never read, change or delete another user's conversations.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.services import db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
# Verified against when the email is unknown, so a failed login takes the same time either way.
_DUMMY_HASH = None


class AccountError(Exception):
    pass


def _conn():
    c = sqlite3.connect(get_settings().audit_db, timeout=db.BUSY_TIMEOUT_S)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return db.tune(c)


def init_db():
    get_settings().audit_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(_conn()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
            active INTEGER NOT NULL DEFAULT 1, created TEXT, last_login TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created TEXT, expires TEXT);
        CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL,
            data TEXT NOT NULL, search_text TEXT NOT NULL DEFAULT '', created TEXT, updated TEXT);
        CREATE INDEX IF NOT EXISTS conversations_user ON conversations(user_id, updated);
        CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER,
            action TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS sessions_expires ON sessions(expires);
        CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS activity_ts ON activity(ts, action);
        CREATE INDEX IF NOT EXISTS activity_user ON activity(user_id, id);
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(conversations)")}
        if "pinned" not in cols:                 # added after the first release
            c.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if "archived" not in cols:
            c.execute("ALTER TABLE conversations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "google_sub" not in {r["name"] for r in c.execute("PRAGMA table_info(users)")}:
            c.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")      # Google account id, once linked
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_google ON users(google_sub) WHERE google_sub IS NOT NULL")
        c.commit()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, TypeError):
        return False


def _check_password(password: str):
    if len(password) < 8:
        raise AccountError("Use at least 8 characters for the password.")
    if len(password) > 200:
        raise AccountError("The password is too long.")


# Accounts made with Google have no password: this marker never verifies (verify_password needs salt$hash).
NO_PASSWORD = "google$"


def public_user(row) -> dict:
    keys = row.keys()
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"],
            "active": bool(row["active"]), "created": row["created"], "last_login": row["last_login"],
            "google": bool("google_sub" in keys and row["google_sub"]),
            "has_password": row["password_hash"] != NO_PASSWORD}


def _new_role(c, email: str) -> str:
    """Same rule for every sign-up: listed admin emails and the very first account are admins."""
    admins = {e.strip().lower() for e in get_settings().admin_emails.split(",") if e.strip()}
    first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    return "admin" if email in admins or first else "user"


# ------------------------------------------------------------------ users
def register(email: str, name: str, password: str) -> dict:
    email, name = email.strip().lower(), name.strip()
    if not EMAIL_RE.match(email) or len(email) > 200:
        raise AccountError("Enter a valid email address.")
    if not name or len(name) > 80:
        raise AccountError("Enter your name (up to 80 characters).")
    _check_password(password)
    with closing(_conn()) as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise AccountError("An account with this email already exists. Log in instead.")
        role = _new_role(c, email)
        cur = c.execute("INSERT INTO users(email,name,password_hash,role,active,created) VALUES (?,?,?,?,1,?)",
                        (email, name, hash_password(password), role, _now()))
        c.commit()
        row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return public_user(row)


def authenticate(email: str, password: str) -> dict:
    global _DUMMY_HASH
    with closing(_conn()) as c:
        row = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if row is None:
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_hex(8))
        verify_password(password, _DUMMY_HASH)
        raise AccountError("Email or password is incorrect.")
    if not verify_password(password, row["password_hash"]):
        raise AccountError("Email or password is incorrect.")
    if not row["active"]:
        raise AccountError("This account is disabled. Contact your administrator.")
    return public_user(row)


def google_login(sub: str, email: str, name: str, allow_new: bool) -> tuple:
    """(user, created) for a verified Google identity (app/services/google_auth.py). The account is found by its
    Google id; else the account with the same (Google-verified) email gets linked to it, keeping its password,
    chats and role; else, when sign-up is open, a new account without a password is made."""
    email = email.strip().lower()
    with closing(_conn()) as c:
        row = c.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone()
        created = False
        if row is None:
            row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if row is not None:
                if row["google_sub"] and row["google_sub"] != sub:
                    raise AccountError("This email is linked to a different Google account.")
                c.execute("UPDATE users SET google_sub=? WHERE id=?", (sub, row["id"]))
            elif not allow_new:
                raise AccountError("Sign-up is closed. Ask an administrator for an account.")
            elif not EMAIL_RE.match(email) or len(email) > 200:
                raise AccountError("This Google account's email address can't be used.")
            else:
                cur = c.execute("INSERT INTO users(email,name,password_hash,role,active,created,google_sub) "
                                "VALUES (?,?,?,?,1,?,?)",
                                (email, (name or email.split("@")[0])[:80], NO_PASSWORD, _new_role(c, email), _now(), sub))
                created = True
                row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            c.commit()
            row = c.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    if not row["active"]:
        raise AccountError("This account is disabled. Contact your administrator.")
    return public_user(row), created


def get_user(user_id: int):
    with closing(_conn()) as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return public_user(row) if row else None


def change_password(user_id: int, current: str, new: str):
    with closing(_conn()) as c:
        row = c.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
        if row is not None and row["password_hash"] == NO_PASSWORD:
            raise AccountError("This account signs in with Google and has no password. Ask an administrator to set one.")
        if row is None or not verify_password(current, row["password_hash"]):
            raise AccountError("The current password is incorrect.")
        _check_password(new)
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new), user_id))
        c.commit()


def list_users() -> list:
    with closing(_conn()) as c:
        rows = c.execute("""SELECT u.*, (SELECT COUNT(*) FROM conversations v WHERE v.user_id=u.id) AS conversations
                            FROM users u ORDER BY u.created DESC""").fetchall()
    return [{**public_user(r), "conversations": r["conversations"]} for r in rows]


def _admin_count(c) -> int:
    return c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]


def update_user(user_id: int, actor_id: int, role=None, active=None, password=None) -> dict:
    with closing(_conn()) as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise LookupError("User not found.")
        losing_admin = row["role"] == "admin" and row["active"] and (role == "user" or active is False)
        if losing_admin and user_id == actor_id:
            raise AccountError("You cannot remove your own admin access or disable your own account.")
        if losing_admin and _admin_count(c) <= 1:
            raise AccountError("Keep at least one active admin.")
        if role is not None:
            if role not in ("user", "admin"):
                raise AccountError("Role must be user or admin.")
            c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        if active is not None:
            c.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
            if not active:
                c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        if password is not None:
            _check_password(password)
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id))
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        c.commit()
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return public_user(row)


def delete_user(user_id: int, actor_id: int) -> dict:
    if user_id == actor_id:
        raise AccountError("You cannot delete your own account here.")
    with closing(_conn()) as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise LookupError("User not found.")
        if row["role"] == "admin" and row["active"] and _admin_count(c) <= 1:
            raise AccountError("Keep at least one active admin.")
        c.execute("DELETE FROM users WHERE id=?", (user_id,))      # sessions and conversations cascade
        c.commit()
    return public_user(row)


# ------------------------------------------------------------------ sessions
def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=get_settings().session_days)
    with closing(_conn()) as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (now.isoformat(timespec="seconds"),))
        c.execute("INSERT INTO sessions(token_hash,user_id,created,expires) VALUES (?,?,?,?)",
                  (_sha(token), user_id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")))
        c.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), user_id))
        c.commit()
    return token


def session_user(token: str | None):
    """The active user for a session token, or None (unknown, expired or disabled)."""
    if not token:
        return None
    with closing(_conn()) as c:
        row = c.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
                           WHERE s.token_hash=? AND s.expires > ? AND u.active=1""",
                        (_sha(token), _now())).fetchone()
    return public_user(row) if row else None


def end_session(token: str | None):
    if token:
        with closing(_conn()) as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_sha(token),))
            c.commit()


# ------------------------------------------------------------------ conversations (always scoped to user_id)
def _search_text(title: str, data: dict) -> str:
    parts = [title] + [m.get("text", "") for m in data.get("messages", []) if isinstance(m, dict)]
    return " ".join(p for p in parts if isinstance(p, str)).lower()[:200_000]


def _encode(data: dict) -> str:
    raw = json.dumps(data, default=str)
    if len(raw) > get_settings().max_conversation_kb * 1024:
        raise AccountError("This conversation is too long to save. Start a new chat.")
    return raw


def _summary(r) -> dict:
    return {"id": r["id"], "title": r["title"], "created": r["created"], "updated": r["updated"],
            "pinned": bool(r["pinned"]), "archived": bool(r["archived"])}


SUMMARY_COLS = "id,title,created,updated,pinned,archived"


def list_conversations(user_id: int, query: str = "", archived: bool = False) -> list:
    sql = f"SELECT {SUMMARY_COLS} FROM conversations WHERE user_id=? AND archived=?"
    args: list = [user_id, 1 if archived else 0]
    if query.strip():
        sql += " AND search_text LIKE ? ESCAPE '\\'"
        q = query.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append(f"%{q}%")
    with closing(_conn()) as c:
        rows = c.execute(sql + " ORDER BY pinned DESC, updated DESC LIMIT 500", args).fetchall()
    return [_summary(r) for r in rows]


def create_conversation(user_id: int, title: str, data: dict) -> dict:
    cid, now = secrets.token_hex(12), _now()
    title = (title.strip() or "New chat")[:120]
    with closing(_conn()) as c:
        c.execute("INSERT INTO conversations(id,user_id,title,data,search_text,created,updated) VALUES (?,?,?,?,?,?,?)",
                  (cid, user_id, title, _encode(data), _search_text(title, data), now, now))
        c.commit()
    return {"id": cid, "title": title, "created": now, "updated": now, "pinned": False, "archived": False, "data": data}


def owns_conversation(user_id: int, cid: str) -> bool:
    with closing(_conn()) as c:
        return c.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?", (cid, user_id)).fetchone() is not None


def get_conversation(user_id: int, cid: str):
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (cid, user_id)).fetchone()
    return {**_summary(r), "data": json.loads(r["data"])} if r else None


def update_conversation(user_id: int, cid: str, title: str | None = None, data: dict | None = None,
                        pinned: bool | None = None, archived: bool | None = None):
    """Only new messages (data) move a chat up the history; renaming, pinning and archiving do not."""
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM conversations WHERE id=? AND user_id=?", (cid, user_id)).fetchone()
        if r is None:
            return None
        new_title = (title.strip()[:120] or r["title"]) if title is not None else r["title"]
        new_data = data if data is not None else json.loads(r["data"])
        c.execute("""UPDATE conversations SET title=?, data=?, search_text=?, updated=?, pinned=?, archived=?
                     WHERE id=? AND user_id=?""",
                  (new_title, _encode(new_data), _search_text(new_title, new_data),
                   _now() if data is not None else r["updated"],
                   int(pinned) if pinned is not None else r["pinned"],
                   int(archived) if archived is not None else r["archived"], cid, user_id))
        c.commit()
        r = c.execute(f"SELECT {SUMMARY_COLS} FROM conversations WHERE id=?", (cid,)).fetchone()
    return _summary(r)


def delete_conversation(user_id: int, cid: str) -> bool:
    with closing(_conn()) as c:
        n = c.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (cid, user_id)).rowcount
        if n and c.execute("SELECT 1 FROM sqlite_master WHERE name='recommendations'").fetchone():
            c.execute("DELETE FROM recommendations WHERE conversation_id=? AND user_id=?", (cid, user_id))
        c.commit()
    return n > 0


# ------------------------------------------------------------------ activity (no case details or personal data)
def log(user_id, action: str, detail: str = ""):
    with closing(_conn()) as c:
        c.execute("INSERT INTO activity(ts,user_id,action,detail) VALUES (?,?,?,?)",
                  (_now(), user_id, action, detail[:300]))
        c.commit()


def recent_activity(limit: int = 100, user_id: int | None = None) -> list:
    sql = """SELECT a.id, a.ts, a.action, a.detail, a.user_id, u.email, u.name
             FROM activity a LEFT JOIN users u ON u.id = a.user_id"""
    args: list = []
    if user_id is not None:
        sql += " WHERE a.user_id=?"
        args.append(user_id)
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY a.id DESC LIMIT ?", (*args, limit)).fetchall()]


def stats() -> dict:
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    with closing(_conn()) as c:
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "active_users": one("SELECT COUNT(*) FROM users WHERE active=1"),
            "admins": one("SELECT COUNT(*) FROM users WHERE role='admin'"),
            "conversations": one("SELECT COUNT(*) FROM conversations"),
            "logins_24h": one("SELECT COUNT(*) FROM activity WHERE action='login' AND ts > ?", day_ago),
            "recommendations_24h": one("SELECT COUNT(*) FROM activity WHERE action='recommendation' AND ts > ?", day_ago),
            "questions_24h": one("SELECT COUNT(*) FROM activity WHERE action IN ('chat','kb_question') AND ts > ?", day_ago),
            "likes_24h": one("SELECT COUNT(*) FROM activity WHERE action='feedback' AND detail LIKE 'up%' AND ts > ?", day_ago),
            "dislikes_24h": one("SELECT COUNT(*) FROM activity WHERE action='feedback' AND detail LIKE 'down%' AND ts > ?", day_ago),
            "active_users_24h": one("SELECT COUNT(DISTINCT user_id) FROM activity WHERE ts > ?", day_ago),
        }
