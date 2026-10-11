"""SQLite connections for the billing and usage services (same database file as accounts)."""
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone

from app.core.config import get_settings


BUSY_TIMEOUT_S = 30


def tune(c: sqlite3.Connection) -> sqlite3.Connection:
    """Settings for many users at once, applied to every connection of every database.

    WAL: readers never wait for a writer and a writer never waits for readers (the default journal blocks every
    read while anything is written, which turns into "database is locked" under concurrent use). It is stored in
    the file, so setting it again is free. synchronous=NORMAL is the recommended pairing: a commit is never
    corrupted, only the last commits before a power cut can be lost. The busy timeout makes writers queue for
    the lock instead of failing."""
    c.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA synchronous = NORMAL")
    return c


def connect() -> sqlite3.Connection:
    """Autocommit connection: reads see committed data, and writes go through transaction()."""
    c = sqlite3.connect(get_settings().audit_db, timeout=BUSY_TIMEOUT_S, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return tune(c)


@contextmanager
def read():
    with closing(connect()) as c:
        yield c


@contextmanager
def transaction():
    """BEGIN IMMEDIATE takes the database write lock before the first read, so a check followed by a write (quota
    reservation, webhook de-duplication, subscription changes) is atomic across threads and processes."""
    with closing(connect()) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)
