"""Structured policy facts: exact values (ROI, payout, fees, LTV, FOIR, minimum CIBIL ...) per lender and product,
with the document, page and dates they come from.

Numbers asked for by a question ("HDFC home loan ROI") are answered from approved rows here first, as a STRUCTURED
FACTS passage the model must copy, and only then from document passages. Rows are imported from a CSV by admins
(or extracted from rate / payout sheets later) and are used only after an admin approves them, so an extraction
mistake never reaches a partner.

CSV columns: lender, product, fact, value, unit, customer_category, effective_from, effective_until, source, page
"""
import csv
import re
import io
import logging
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

from langchain_core.documents import Document

log = logging.getLogger(__name__)
FACTS = {"roi": "Rate of interest", "payout": "Payout", "processing_fee": "Processing fee",
         "max_amount": "Maximum loan amount", "min_amount": "Minimum loan amount", "tenure": "Maximum tenure",
         "ltv": "Maximum LTV", "foir": "Maximum FOIR", "min_cibil": "Minimum CIBIL", "min_income": "Minimum income",
         "age": "Age limits", "documents": "Documents required"}
FACT_FOR_METRIC = {"roi": "roi", "payout": "payout", "processing_fee": "processing_fee", "max_amount": "max_amount",
                   "min_amount": "min_amount", "tenure": "tenure", "ltv": "ltv", "foir": "foir", "cibil": "min_cibil",
                   "min_income": "min_income", "age": "age", "documents": "documents"}
PENDING, APPROVED = "pending", "approved"
MAX_ROWS_SHOWN = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_facts(fact_id INTEGER PRIMARY KEY, lender TEXT NOT NULL, lender_key TEXT NOT NULL,
    product TEXT NOT NULL DEFAULT '', fact TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '',
    customer_category TEXT NOT NULL DEFAULT '', effective_from TEXT, effective_until TEXT,
    source TEXT NOT NULL DEFAULT '', page TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
    created TEXT, by TEXT, approved_by TEXT,
    UNIQUE(lender_key, product, fact, customer_category, effective_from, value));
CREATE INDEX IF NOT EXISTS policy_facts_find ON policy_facts(lender_key, fact, status);
"""


def _db():
    from app.services.knowledge_base import _db
    return _db()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _date(s: str, row: int, col: str) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        raise ValueError(f"Row {row}: {col} must be a date like 2026-08-01, not '{s}'.")


def import_csv(text: str, by: str = "") -> dict:
    """Validate every row first (nothing is saved if one is wrong), then add them as pending. A row identical to
    one already there is skipped."""
    from app.services.banker_directory import canonical_lender, canonical_product, key
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise ValueError("The file has no rows.")
    missing = {"lender", "fact", "value"} - {(c or "").strip().lower() for c in rows[0].keys()}
    if missing:
        raise ValueError(f"Missing column(s): {', '.join(sorted(missing))}.")
    clean = []
    for n, r in enumerate(rows, 2):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        fact = r.get("fact", "").lower().replace(" ", "_")
        if fact not in FACTS:
            raise ValueError(f"Row {n}: fact must be one of {', '.join(FACTS)}, not '{r.get('fact')}'.")
        if not r.get("lender") or not r.get("value"):
            raise ValueError(f"Row {n}: lender and value are required.")
        lender = canonical_lender(r["lender"])[0]
        clean.append((lender, key(lender), canonical_product(r.get("product", "")), fact, r["value"][:300],
                      r.get("unit", "")[:20], r.get("customer_category", "")[:60],
                      _date(r.get("effective_from", ""), n, "effective_from"),
                      _date(r.get("effective_until", ""), n, "effective_until"),
                      r.get("source", "")[:300], r.get("page", "")[:20], PENDING, _now(), by or "system"))
    with closing(_db()) as db:
        before = db.total_changes
        db.executemany("INSERT OR IGNORE INTO policy_facts(lender, lender_key, product, fact, value, unit, "
                       "customer_category, effective_from, effective_until, source, page, status, created, by) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", clean)
        added = db.total_changes - before
        db.commit()
    return {"rows": len(clean), "added": added, "skipped": len(clean) - added}


_PCT = re.compile(r"\d+(?:\.\d+)?\s*%")
_LENDER_COLS = ("bank name", "lender", "lender name", "bank", "nbfc name")
_NOTE_COLS = re.compile(r"remark|t ?& ?c|terms|note|comment|condition", re.I)   # notes, not a product column


def extract_payout_grid(path, rel: str = "", by: str = "system") -> dict:
    """Payout facts from a slab book / payout grid (one row per lender, one column per product, percentages in
    the cells), added as pending for an admin to check. The effective date is the month in the file name."""
    from pathlib import Path
    from app.services.banker_directory import canonical_lender, canonical_product, key
    from app.services.ingestion import extract
    from app.services.kb_versions import parse_period
    path = Path(path)
    period = parse_period(path.name)
    rows = []
    for part in extract(path):
        for line in part.rows:
            segs = line.split(" | ")
            cells = [(k.strip(), v.strip()) for k, v in (c.split(":", 1) for c in segs if ":" in c)]
            lender = next((v for k, v in cells if k.lower() in _LENDER_COLS and v), "")
            first = segs[0].strip()
            if not lender and ":" not in first and re.search(r"[A-Za-z]{3}", first) and "%" not in first:
                lender = first                       # the lender column has no header (first, unlabelled cell)
            if not lender:
                continue
            name = canonical_lender(lender)[0]
            for col, value in cells:
                if col.lower() in _LENDER_COLS or _NOTE_COLS.search(col) or not _PCT.search(value):
                    continue
                product = canonical_product(re.sub(r"\(.*?\)", "", col).strip())
                rows.append((name, key(name), product, "payout", value[:300], "", "",
                             period.isoformat() if period else None, None, rel or path.name, part.label[:20],
                             PENDING, _now(), by))
    with closing(_db()) as db:
        before = db.total_changes
        db.executemany("INSERT OR IGNORE INTO policy_facts(lender, lender_key, product, fact, value, unit, "
                       "customer_category, effective_from, effective_until, source, page, status, created, by) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        added = db.total_changes - before
        db.commit()
    return {"rows": len(rows), "added": added, "skipped": len(rows) - added}


def listing(status: str = "", limit: int = 500) -> List[dict]:
    with closing(_db()) as db:
        q = "SELECT * FROM policy_facts" + (" WHERE status=?" if status else "") + " ORDER BY lender, product, fact LIMIT ?"
        return [dict(r) for r in db.execute(q, ((status, limit) if status else (limit,)))]


def approve(ids: List[int], by: str = "") -> int:
    with closing(_db()) as db:
        n = db.executemany("UPDATE policy_facts SET status=?, approved_by=? WHERE fact_id=? AND status=?",
                           [(APPROVED, by, i, PENDING) for i in ids]).rowcount
        db.commit()
    return n


def delete(ids: List[int]) -> int:
    with closing(_db()) as db:
        n = db.executemany("DELETE FROM policy_facts WHERE fact_id=?", [(i,) for i in ids]).rowcount
        db.commit()
    return n


def current(lenders: List[str], facts: List[str], product: str = "", today: Optional[date] = None) -> List[dict]:
    """Approved rows in force today for these lenders and facts (and the product, or rows naming none)."""
    from app.services.banker_directory import canonical_product, key
    if not lenders or not facts:
        return []
    today = (today or date.today()).isoformat()
    keys = [key(n) for n in lenders]
    sql = (f"SELECT * FROM policy_facts WHERE status=? AND lender_key IN ({','.join('?' * len(keys))}) "
           f"AND fact IN ({','.join('?' * len(facts))}) AND (effective_from IS NULL OR effective_from<=?) "
           "AND (effective_until IS NULL OR effective_until>=?)")
    args: list = [APPROVED, *keys, *facts, today, today]
    if product:
        sql += " AND product IN (?, '')"
        args.append(canonical_product(product))
    with closing(_db()) as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY lender, product, fact, effective_from DESC LIMIT ?",
                                            (*args, MAX_ROWS_SHOWN))]


def for_question(plan) -> Optional[Document]:
    """A STRUCTURED FACTS passage for a question naming a lender and a fact that approved rows hold, else None."""
    facts = list(dict.fromkeys(FACT_FOR_METRIC[m] for m in plan.metrics if m in FACT_FOR_METRIC))
    try:
        rows = current(plan.banks, facts, plan.products[0] if plan.products else "")
    except Exception:
        log.exception("Policy facts unreadable")
        return None
    if not rows:
        return None
    lines = ["Source: Structured policy facts | Approved by admins",
             "Document: Structured policy facts (approved values)",
             "STRUCTURED FACTS: exact values approved by the knowledge-base admins, each with its source document, "
             "page and the dates it applies; only values in force today are listed."]
    for r in rows:
        bits = [f"Lender: {r['lender']}", f"Product: {r['product'] or 'all products'}", f"{FACTS[r['fact']]}: {r['value']}"
                + (f" {r['unit']}" if r["unit"] else "")]
        bits += [f"{k}: {v}" for k, v in (("Customer", r["customer_category"]), ("Effective from", r["effective_from"]),
                                         ("Effective until", r["effective_until"]), ("Document", r["source"]),
                                         ("Page", r["page"])) if v]
        lines.append(" | ".join(bits))
    return Document(id="policy-facts", page_content="\n".join(lines),
                    metadata={"source": "Structured policy facts", "title": "Structured policy facts (approved)",
                              "section": "Approved values", "lookup": True, "corpus": "urban_money"})
