"""Aliases admins add for banks, products and cities ("BAJAJ HFL" -> Bajaj Finserv, "HLUC" -> Home Loan,
"Bengaluru" -> Bangalore), on top of the built-in tables in app/rag/entities.py and app/services/banker_directory.py.

They are used everywhere names are recognised: questions, document tagging, the banker directory and metadata
filters. A change rebuilds the banker directory in the background; until it is done, banker matching reads the
row index (the directory's fingerprint includes the aliases, so it is never used out of date).
"""
import logging
import threading
from contextlib import closing
from datetime import datetime, timezone
from typing import Dict, List

log = logging.getLogger(__name__)
KINDS = ("bank", "product", "city")
SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_aliases(kind TEXT NOT NULL CHECK(kind IN ('bank', 'product', 'city')),
    alias TEXT NOT NULL, canonical TEXT NOT NULL, created TEXT, by TEXT, PRIMARY KEY(kind, alias));
"""


def _db():
    from app.services.knowledge_base import _db
    return _db()


def _clean(s: str) -> str:
    return " ".join((s or "").split()).strip()


def apply(rows: List[dict]):
    """Push the aliases into the matchers and drop every cached normalisation."""
    from app.rag import entities
    from app.services import banker_directory as bd
    tables: Dict[str, Dict[str, List[str]]] = {"bank": {}, "product": {}}
    cities: Dict[str, str] = {}
    for r in rows:
        if r["kind"] == "city":
            cities[r["alias"]] = r["canonical"]
        else:
            tables[r["kind"]].setdefault(r["canonical"], []).append(r["alias"])
    entities.set_admin_aliases("bank", tables["bank"])
    entities.set_admin_aliases("product", tables["product"])
    bd.ADMIN_CITY_ALIASES.clear()
    bd.ADMIN_CITY_ALIASES.update(cities)
    for fn in (bd.canonical_lender, bd.canonical_product, bd.canonical_city):
        fn.cache_clear()
    bd._cities.clear()
    bd._city_names.clear()


def listing() -> List[dict]:
    with closing(_db()) as db:
        return [dict(r) for r in db.execute("SELECT kind, alias, canonical, created, by FROM kb_aliases "
                                            "ORDER BY kind, canonical, alias")]


def load() -> int:
    rows = listing()
    apply(rows)
    return len(rows)


def _changed(by: str, detail: str):
    from app.services import banker_directory, kb_audit
    kb_audit.record("alias", by, detail=detail)
    load()
    threading.Thread(target=banker_directory.rebuild_quietly, daemon=True, name="banker-directory").start()


def add(kind: str, alias: str, canonical: str, by: str = "") -> dict:
    """Map alias to canonical. A bank / product alias that already belongs to another built-in name is refused
    (it would make the same words mean two lenders)."""
    from app.rag import entities
    kind, alias, canonical = (kind or "").strip().lower(), _clean(alias).lower(), _clean(canonical)
    if kind not in KINDS:
        raise ValueError("Kind must be bank, product or city.")
    if not alias or not canonical or len(alias) > 80 or len(canonical) > 80:
        raise ValueError("Enter an alias and the name it stands for (at most 80 characters each).")
    if len(alias) < 2:
        raise ValueError("An alias needs at least 2 characters.")
    if kind in ("bank", "product"):
        builtin = entities.BANKS if kind == "bank" else entities.PRODUCTS
        owner = next((c for c, al in builtin.items() if entities._norm(alias) in al), None)
        if owner and owner != canonical:
            raise ValueError(f"'{alias}' already means {owner}.")
    with closing(_db()) as db:
        db.execute("INSERT OR REPLACE INTO kb_aliases VALUES (?,?,?,?,?)",
                   (kind, alias, canonical, datetime.now(timezone.utc).isoformat(timespec="seconds"), by or "system"))
        db.commit()
    _changed(by, f"{kind} alias '{alias}' -> {canonical}")
    return {"kind": kind, "alias": alias, "canonical": canonical}


def remove(kind: str, alias: str, by: str = "") -> bool:
    with closing(_db()) as db:
        n = db.execute("DELETE FROM kb_aliases WHERE kind=? AND alias=?", ((kind or "").lower(), _clean(alias).lower())).rowcount
        db.commit()
    if n:
        _changed(by, f"removed {kind} alias '{alias}'")
    return bool(n)
