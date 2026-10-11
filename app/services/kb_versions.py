"""Document registry and versioning for the Urban Money knowledge base.

A *document* is one logical source (stable document_id, title, bank, product, category), e.g. "Payout Sheet".
Each distinct content is a *version*, and versions may have different file names:

    Payout Sheet
     ├── v1 — ARCHIVED     215 PLUS APRIL 2026 PAYOUT SHEET.xls   April 2026    kept for audit / rollback
     └── v2 — ACTIVE       AUG 26 PAYOUT SHEET.xls                August 2026   the file in the folder: searched

The document's `path` is the file of its ACTIVE version. The database allows at most one ACTIVE version per
document (a unique index), so two versions can never be searched side by side. Admin uploads that look like a
new version of an existing document wait for an admin decision (app/services/kb_staging.py); files copied into
the folder directly are registered as they are (bulk loading).

A document taken out of the knowledge base (archived to the hidden .archive folder, or deleted) has its last
version marked DISABLED. Versions are created by the sync when it sees new content at a path, so files copied
into the folder by other means are versioned too. When an admin replaces a file through the console, the
previous file's bytes are kept in the hidden ".versions" folder so that version can be re-activated (rollback).
For content replaced outside the console, the extracted text of the old version is still kept (extraction
cache), so it can be previewed but not restored.

Metadata (bank, product, category, title) is inferred from the file name and text when a document is first
seen, and can be corrected by an admin; it is written into every chunk and used for metadata filtering.
"""
import json
import re
import shutil
import threading
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.rag import entities

ACTIVE, ARCHIVED, DISABLED = "ACTIVE", "ARCHIVED", "DISABLED"
DOC_TYPES = ["Policy", "Payout", "Codes", "Rate sheet", "Eligibility", "Product brochure", "Contacts", "Pincode list",
             "Company list", "FAQ", "Reference", "General"]
VERSIONS_DIR = ".versions"
META_FIELDS = ("title", "bank", "product", "category")
_lock = threading.RLock()
_active_cache: Dict[str, str] = {}
_active_loaded = False
_authority_cache: Dict[str, dict] = {}

# Source authority. When documents disagree, the knowledge-base metadata decides which one applies (never the
# model): the higher priority wins. A document's priority defaults from its source type, which defaults from its
# document type; admins can set both.
SOURCE_TYPES = {
    "OFFICIAL_POLICY": ("Official policy", 100), "PAYOUT_SHEET": ("Payout sheet", 90), "RATE_SHEET": ("Rate sheet", 90),
    "CODE_BOOK": ("Code book", 85), "INTERNAL_REFERENCE": ("Internal reference", 70), "REFERENCE": ("Reference", 50),
    "OLD_DOCUMENT": ("Old document", 30), "FAQ": ("FAQ", 20),
}
_SOURCE_FOR_CATEGORY = {"Policy": "OFFICIAL_POLICY", "Eligibility": "OFFICIAL_POLICY", "Payout": "PAYOUT_SHEET",
                        "Rate sheet": "RATE_SHEET", "Codes": "CODE_BOOK", "Contacts": "INTERNAL_REFERENCE",
                        "Pincode list": "INTERNAL_REFERENCE", "Company list": "INTERNAL_REFERENCE", "FAQ": "FAQ"}
OCR_PENALTY = 20                     # text read by OCR and not verified by an admin
EXPIRED_PENALTY = 30                 # the version's effective-until date has passed


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db():
    from app.services.knowledge_base import _db as kb_db      # one SQLite file holds the whole KB index
    return kb_db()


def _invalidate():
    global _active_loaded
    with _lock:
        _active_loaded = False
        _authority_cache.clear()


def default_source(category: str) -> str:
    return _SOURCE_FOR_CATEGORY.get(category or "", "REFERENCE")


def doc_type_of(doc: dict) -> str:
    """The document's type (Payout, Policy, Contacts, ...): the inferred / admin-set doc_type, else its category
    when that is a type rather than a product."""
    if doc.get("doc_type"):
        return doc["doc_type"]
    return doc.get("category") if doc.get("category") in DOC_TYPES else "General"


def backfill_doc_types(db):
    """Fill doc_type for documents registered before it existed: from the file name and the cached text of the
    active version (no file is re-read and nothing is re-indexed)."""
    rows = db.execute("SELECT d.path, d.category, v.sha FROM kb_docs d LEFT JOIN kb_versions v ON v.doc_id=d.doc_id "
                      "AND v.status=? WHERE d.doc_type IS NULL", (ACTIVE,)).fetchall()
    if not rows:
        return 0
    for path, category, sha in rows:
        if category in DOC_TYPES and category != "General":
            t = category
        else:
            parts = cached_parts(sha, verified=False) if sha else None
            sample = "\n".join(p.text or "\n".join([p.context, *p.rows[:20]]) for p in (parts or [])[:3])
            t = entities.infer_doc_type(path, sample)
        db.execute("UPDATE kb_docs SET doc_type=? WHERE path=?", (t, path))
    db.commit()
    _invalidate()
    return len(rows)


def backfill_periods(db):
    """Date versions whose file name names a month the parser did not know when they were registered
    (e.g. 'Augest 2026'). Only versions without a date and without an admin-set label are touched."""
    rows = db.execute("SELECT doc_id, version, file FROM kb_versions WHERE effective_from IS NULL "
                      "AND COALESCE(label, '')=''").fetchall()
    n = 0
    for doc_id, version, file in rows:
        period = parse_period(file or "")
        if period:
            db.execute("UPDATE kb_versions SET effective_from=?, label=? WHERE doc_id=? AND version=?",
                       (period.isoformat(), period_label(period), doc_id, version))
            n += 1
    if n:
        db.commit()
        _invalidate()
    return n


def authority_of(doc: dict, version: Optional[dict] = None, today: Optional[date] = None) -> dict:
    """{source_type, source_label, base, priority, reasons}: the priority used when documents disagree."""
    st = doc.get("source_type") or default_source(doc_type_of(doc))
    label, default = SOURCE_TYPES.get(st, SOURCE_TYPES["REFERENCE"])
    base = doc["priority"] if doc.get("priority") is not None else default
    v = version or {}
    prio, notes = base, []
    if v.get("extraction") == "ocr":
        prio -= OCR_PENALTY
        notes.append("OCR, not verified")
    until = v.get("effective_until")
    if until and until < (today or date.today()).isoformat():
        prio -= EXPIRED_PENALTY
        notes.append(f"expired {until}")
    return {"source_type": st, "source_label": label, "base": base, "priority": max(0, prio), "notes": notes,
            "custom": doc.get("priority") is not None}


def authority_map() -> Dict[str, dict]:
    """path -> authority of its ACTIVE version, plus its dates and how its text was obtained (cached)."""
    with _lock:
        if _authority_cache:
            return _authority_cache
        with closing(_db()) as db:
            rows = db.execute("SELECT d.*, v.extraction, v.effective_from, v.effective_until, v.label FROM kb_docs d "
                              "JOIN kb_versions v ON v.doc_id=d.doc_id AND v.status=?", (ACTIVE,)).fetchall()
        for r in rows:
            r = dict(r)
            _authority_cache[r["path"]] = {**authority_of(r, r), "category": r["category"] or "", "title": r["title"] or "",
                                           "doc_type": doc_type_of(r),
                                           "extraction": r["extraction"] or "", "label": r["label"] or "",
                                           "effective_from": r["effective_from"], "effective_until": r["effective_until"]}
        _authority_cache.setdefault("", {})              # loaded, even when empty
        return _authority_cache


def set_authority(rel: str, source_type: Optional[str], priority: Optional[int]) -> dict:
    """Admin choice of source type and priority (None = the default for the document type)."""
    if source_type and source_type not in SOURCE_TYPES:
        raise ValueError("Unknown source type.")
    if priority is not None and not 0 <= int(priority) <= 100:
        raise ValueError("Priority must be between 0 and 100.")
    with _lock, closing(_db()) as db:
        if db.execute("UPDATE kb_docs SET source_type=?, priority=?, updated=? WHERE path=?",
                      (source_type or None, priority, _now(), rel)).rowcount == 0:
            raise LookupError("This document has not been processed yet.")
        db.commit()
    _invalidate()
    return get_doc(rel)


# ------------------------------------------------------------------ metadata inference
def _title(rel: str) -> str:
    stem = Path(rel).stem
    stem = stem.replace("_", " ").replace("  ", " ")
    return " ".join(stem.split()).strip(" -.") or Path(rel).name


def infer(rel: str, sample: str = "") -> dict:
    """Title, bank, product and category from the file name, falling back to the start of the text.
    A file that names several banks (code books, RM lists) gets no bank: it is not about one bank."""
    name = Path(rel).name
    banks = entities.find_banks(name)
    if not banks and sample:
        found = entities.find_banks(sample[:3000])
        banks = found if len(found) == 1 else []
    products = entities.find_products(name)
    if not products and sample:
        found = entities.find_products(sample[:3000])
        products = found if len(found) == 1 else []
    return {"title": _title(rel), "bank": banks[0] if len(banks) == 1 else "",
            "product": products[0] if len(products) == 1 else "",
            "category": entities.infer_category(name, products)}


# ------------------------------------------------------------------ periods ("AUG 26", "APRIL 2026", "20260914")
_MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
           "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
           "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
           # misspellings seen in file names: "Pan India RM list Augest 2026" went undated, so it was ranked and
           # labelled as older than the June list
           "janaury": 1, "febuary": 2, "feburary": 2, "augest": 8, "agust": 8, "septmber": 9, "octomber": 10,
           "novmber": 11, "decmber": 12}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_PERIOD = [re.compile(rf"(?<![a-z])({_MONTH_RE})[\s_.'-]*((?:19|20)\d{{2}}|\d{{2}})(?!\d)"),
           re.compile(rf"(?<!\d)((?:19|20)\d{{2}})[\s_.-]*({_MONTH_RE})(?![a-z])")]
_YMD = re.compile(r"(?<!\d)(20\d{2})[-_]?(0[1-9]|1[0-2])[-_]?([0-3]\d)(?!\d)")


def parse_period(name: str) -> Optional[date]:
    """The month a file name refers to (first day of it), e.g. 'AUG 26 PAYOUT SHEET' -> 2026-08-01."""
    t = Path(name).stem.lower()
    m = _PERIOD[0].search(t)
    if m:
        y = int(m.group(2))
        return date(y + 2000 if y < 100 else y, _MONTHS[m.group(1)], 1)
    m = _PERIOD[1].search(t)
    if m:
        return date(int(m.group(1)), _MONTHS[m.group(2)], 1)
    m = _YMD.search(t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def period_label(d: Optional[date]) -> str:
    return d.strftime("%B %Y") if d else ""


def family(title: str) -> str:
    """The document a dated edition belongs to: its title without the period and numbering, e.g.
    'Pan India RM list May -2026.' and 'Pan India RM list Augest 2026' -> 'pan india rm list'."""
    t = (title or "").lower()
    for pat in (*_PERIOD, _YMD):
        t = pat.sub(" ", t)
    return " ".join(re.findall(r"[a-z]+", re.sub(rf"({_MONTH_RE})", " ", t)))


def edition_date(path: str, a: Optional[dict] = None) -> Optional[date]:
    """When a document's active version took effect: its effective-from date, else the period in its name."""
    a = a if a is not None else authority_map().get(path, {})
    if a.get("effective_from"):
        try:
            return date.fromisoformat(a["effective_from"][:10])
        except ValueError:
            pass
    return parse_period(a.get("title") or "") or parse_period(path)


def older_editions(paths) -> Dict[str, str]:
    """For paths that are an older edition of another path in the same set (same family, earlier date; an
    undated edition counts as oldest): path -> the newest edition's label ('August 2026'). Monthly RM lists and
    payout sheets are not full replacements of each other, so older editions stay searchable; they are only
    ranked below, and marked in the prompt, when a newer edition answers too."""
    amap = authority_map()
    groups: Dict[str, List[tuple]] = {}
    for p in dict.fromkeys(paths):
        a = amap.get(p) or {}
        fam = family(a.get("title") or Path(p).stem)
        if fam:
            groups.setdefault(fam, []).append((edition_date(p, a), p))
    out = {}
    for items in groups.values():
        dated = [d for d, _ in items if d]
        if len(items) < 2 or not dated:
            continue
        newest = max(dated)
        for d, p in items:
            if d is None or d < newest:
                out[p] = period_label(newest)
    return out


def _day_before(iso: str) -> Optional[str]:
    try:
        return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ registry
def get_doc(rel: str) -> Optional[dict]:
    with closing(_db()) as db:
        r = db.execute("SELECT * FROM kb_docs WHERE path=?", (rel,)).fetchone()
    return dict(r) if r else None


def _ensure_doc(db, rel: str, sample: str) -> dict:
    r = db.execute("SELECT * FROM kb_docs WHERE path=?", (rel,)).fetchone()
    if r:
        return dict(r)
    meta = infer(rel, sample)
    row = {"path": rel, "doc_id": uuid.uuid4().hex[:12], **meta, "meta_source": "auto", "created": _now(), "updated": _now(),
           "doc_type": meta["category"] if meta["category"] in DOC_TYPES and meta["category"] != "General"
           else entities.infer_doc_type(rel, sample)}
    db.execute("INSERT INTO kb_docs(path, doc_id, title, bank, product, category, meta_source, created, updated, doc_type) "
               "VALUES (:path,:doc_id,:title,:bank,:product,:category,:meta_source,:created,:updated,:doc_type)", row)
    return row


def register(rel: str, sha: str, size: int, sample: str = "", by: str = "", db=None) -> dict:
    """Record that `sha` is the content now at `rel` and make it the ACTIVE version (called by the sync before
    indexing, with its own connection so the two never wait on each other). Returns the chunk metadata."""
    if db is None:
        with closing(_db()) as own:
            return register(rel, sha, size, sample, by, own)
    with _lock:
        doc = _ensure_doc(db, rel, sample)
        row = db.execute("SELECT * FROM kb_versions WHERE doc_id=? AND sha=? "
                         "ORDER BY status='ACTIVE' DESC, version DESC", (doc["doc_id"], sha)).fetchone()
        if row is None:
            period = parse_period(rel)
            version = _insert_version(db, doc["doc_id"], sha, size, rel, ARCHIVED, by=by, label=period_label(period),
                                      effective_from=period.isoformat() if period else None)
        else:
            version = row["version"]
        db.execute("UPDATE kb_versions SET status=? WHERE doc_id=? AND status=? AND version<>?",
                   (ARCHIVED, doc["doc_id"], ACTIVE, version))
        db.execute("UPDATE kb_versions SET status=? WHERE doc_id=? AND version=?", (ACTIVE, doc["doc_id"], version))
        db.commit()
    _invalidate()
    return chunk_meta_for(doc, version, sha)


def _insert_version(db, doc_id: str, sha: str, size: int, file: str, status: str, by: str = "", note: str = "",
                    label: str = "", effective_from: Optional[str] = None, effective_until: Optional[str] = None) -> int:
    n = db.execute("SELECT COALESCE(MAX(version), 0) FROM kb_versions WHERE doc_id=?", (doc_id,)).fetchone()[0] + 1
    db.execute("INSERT INTO kb_versions(doc_id, version, sha, status, size, stored, note, created, by, file, label, "
               "effective_from, effective_until) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (doc_id, n, sha, status, size, None, note, _now(), by, file, label or "", effective_from or None,
                effective_until or None))
    return n


def set_extraction(doc_id: str, sha: str, kind: str, db):
    """How this version's text was obtained: 'text', 'ocr' (not verified) or 'verified'."""
    db.execute("UPDATE kb_versions SET extraction=? WHERE doc_id=? AND sha=?", (kind, doc_id, sha))
    _invalidate()


def chunk_meta_for(doc: dict, version: int, sha: str, status: str = ACTIVE) -> dict:
    return {"document_id": doc["doc_id"], "version_id": f"{doc['doc_id']}-v{version}", "version": version,
            "sha": sha[:12], "status": status, "title": doc.get("title") or "", "bank": doc.get("bank") or "",
            "product": doc.get("product") or "", "category": doc.get("category") or ""}


def chunk_meta(rel: str, sha: str) -> Optional[dict]:
    """Metadata for chunks of this content, if it is registered (else None: preview before the first sync)."""
    with closing(_db()) as db:
        doc = db.execute("SELECT * FROM kb_docs WHERE path=?", (rel,)).fetchone()
        if doc is None:
            return None
        v = db.execute("SELECT version, status FROM kb_versions WHERE doc_id=? AND sha=? "
                       "ORDER BY status='ACTIVE' DESC, version DESC", (doc["doc_id"], sha)).fetchone()
    if v is None:
        return None
    return chunk_meta_for(dict(doc), v["version"], sha, v["status"])


def disable(rel: str, note: str):
    """The document left the knowledge base (archived or deleted): its active version becomes DISABLED."""
    with _lock, closing(_db()) as db:
        doc = db.execute("SELECT doc_id FROM kb_docs WHERE path=?", (rel,)).fetchone()
        if doc:
            db.execute("UPDATE kb_versions SET status=?, note=? WHERE doc_id=? AND status=?",
                       (DISABLED, note, doc["doc_id"], ACTIVE))
            db.commit()
    _invalidate()


def active_versions() -> Dict[str, str]:
    """path -> sha[:12] of its ACTIVE version. Retrieval drops any chunk whose version is not in here."""
    global _active_loaded
    with _lock:
        if not _active_loaded:
            with closing(_db()) as db:
                rows = db.execute("SELECT d.path, v.sha FROM kb_docs d JOIN kb_versions v ON v.doc_id=d.doc_id "
                                  "WHERE v.status=?", (ACTIVE,)).fetchall()
            _active_cache.clear()
            _active_cache.update({r["path"]: r["sha"][:12] for r in rows})
            _active_loaded = True
        return dict(_active_cache)


def known_names() -> Dict[str, List[str]]:
    """Bank and product names entered on documents (used to recognise them in questions)."""
    with closing(_db()) as db:
        rows = db.execute("SELECT DISTINCT bank, product FROM kb_docs").fetchall()
    return {"banks": sorted({r["bank"] for r in rows if r["bank"]}),
            "products": sorted({r["product"] for r in rows if r["product"]})}


def docs_by_path() -> Dict[str, dict]:
    with closing(_db()) as db:
        rows = db.execute("SELECT d.*, COUNT(v.version) AS versions, "
                          "MAX(CASE WHEN v.status='ACTIVE' THEN v.version END) AS active_version, "
                          "MAX(CASE WHEN v.status='ACTIVE' THEN v.label END) AS label, "
                          "MAX(CASE WHEN v.status='ACTIVE' THEN v.effective_from END) AS effective_from, "
                          "MAX(CASE WHEN v.status='ACTIVE' THEN v.effective_until END) AS effective_until, "
                          "MAX(CASE WHEN v.status='ACTIVE' THEN v.extraction END) AS extraction "
                          "FROM kb_docs d LEFT JOIN kb_versions v ON v.doc_id=d.doc_id GROUP BY d.path").fetchall()
    return {r["path"]: dict(r) for r in rows}


def update_meta(rel: str, changes: dict) -> dict:
    """Admin correction of title / bank / product / category. The caller re-indexes the file afterwards."""
    clean = {k: str(v or "").strip()[:120] for k, v in changes.items() if k in META_FIELDS}
    if not clean:
        raise ValueError("Nothing to change.")
    with _lock, closing(_db()) as db:
        if db.execute("SELECT 1 FROM kb_docs WHERE path=?", (rel,)).fetchone() is None:
            raise LookupError("This document has not been processed yet.")
        if clean.get("category") in DOC_TYPES:          # a type (not a product) chosen by an admin is the doc type
            clean["doc_type"] = clean["category"]
        sets = ", ".join(f"{k}=?" for k in clean)
        db.execute(f"UPDATE kb_docs SET {sets}, meta_source='admin', updated=? WHERE path=?", (*clean.values(), _now(), rel))
        db.commit()
    _invalidate()                            # the document type decides the default source type
    return get_doc(rel)


def versions(rel: str) -> List[dict]:
    doc = get_doc(rel)
    if doc is None:
        return []
    with closing(_db()) as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM kb_versions WHERE doc_id=? ORDER BY version DESC", (doc["doc_id"],))]
    for r in rows:
        r["restorable"] = bool(r["stored"]) and Path(r["stored"]).is_file() or r["status"] == ACTIVE
        r["stored"] = bool(r["stored"])
    return rows


# ------------------------------------------------------------------ keeping old bytes, rollback
def _store_dir(root: Path) -> Path:
    return root / VERSIONS_DIR


def retain(root: Path, rel: str, path: Path, sha: str):
    """Keep a copy of the bytes now at `path` (about to be replaced) so that version can be restored later."""
    doc = get_doc(rel)
    if doc is None:
        return
    dst = _store_dir(root) / f"{sha}{path.suffix.lower()}"
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
    with closing(_db()) as db:
        db.execute("UPDATE kb_versions SET stored=? WHERE doc_id=? AND sha=?", (str(dst), doc["doc_id"], sha))
        db.commit()


def activate(root: Path, rel: str, version: int, current: Path, current_sha: str, by: str = "") -> dict:
    """Roll back / forward: put the bytes of `version` back in the folder under the file name that version had,
    and make it ACTIVE (the current content becomes ARCHIVED and is kept). Returns the version with `path` (where
    it now is) and `previous_path`; the caller re-indexes both."""
    doc = get_doc(rel)
    if doc is None:
        raise LookupError("Document not found.")
    with closing(_db()) as db:
        row = db.execute("SELECT * FROM kb_versions WHERE doc_id=? AND version=?", (doc["doc_id"], version)).fetchone()
    if row is None:
        raise LookupError("Version not found.")
    target_rel = row["file"] or rel
    if row["status"] == ACTIVE and row["sha"] == current_sha:
        return {**dict(row), "path": rel, "previous_path": rel}
    if not row["stored"] or not Path(row["stored"]).is_file():
        raise ValueError("The original file of this version was not kept (it was replaced outside the console), "
                         "so it cannot be restored. Its extracted text can still be previewed.")
    target = root / target_rel
    if target_rel != rel and target.exists():
        raise ValueError(f"Another file named {target_rel} is in the knowledge base. Archive or rename it first.")
    if current.is_file():
        retain(root, rel, current, current_sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.restoring")
    shutil.copy2(row["stored"], tmp)
    tmp.replace(target)
    if target_rel != rel and current.is_file():
        current.unlink()
    with _lock, closing(_db()) as db:
        db.execute("UPDATE kb_versions SET status=? WHERE doc_id=? AND status IN (?,?)", (ARCHIVED, doc["doc_id"], ACTIVE, DISABLED))
        db.execute("UPDATE kb_versions SET status=?, note=? WHERE doc_id=? AND version=?",
                   (ACTIVE, f"re-activated by {by}" if by else "re-activated", doc["doc_id"], version))
        db.execute("UPDATE kb_docs SET path=?, updated=? WHERE doc_id=?", (target_rel, _now(), doc["doc_id"]))
        db.commit()
    _invalidate()
    return {**dict(row), "status": ACTIVE, "path": target_rel, "previous_path": rel}


# ------------------------------------------------------------------ approved uploads and merging
def doc_by_id(doc_id: str) -> Optional[dict]:
    with closing(_db()) as db:
        r = db.execute("SELECT * FROM kb_docs WHERE doc_id=?", (doc_id,)).fetchone()
    return dict(r) if r else None


def catalog() -> List[dict]:
    """Every document with an ACTIVE version: what a new upload can be matched against."""
    with closing(_db()) as db:
        rows = db.execute("SELECT d.*, v.version, v.sha, v.size, v.label, v.effective_from, v.effective_until, v.created "
                          "AS version_created FROM kb_docs d JOIN kb_versions v ON v.doc_id=d.doc_id AND v.status=? "
                          "ORDER BY d.title", (ACTIVE,)).fetchall()
    return [dict(r) for r in rows]


def find_sha(sha: str) -> List[dict]:
    """Every registered version with exactly this content (any document, any status)."""
    with closing(_db()) as db:
        rows = db.execute("SELECT d.path, d.title, d.doc_id, v.version, v.status, v.created, v.file FROM kb_versions v "
                          "JOIN kb_docs d ON d.doc_id=v.doc_id WHERE v.sha=? ORDER BY v.status='ACTIVE' DESC, v.created DESC",
                          (sha,)).fetchall()
    return [dict(r) for r in rows]


def _clean_meta(meta: dict) -> dict:
    return {k: str(meta.get(k) or "").strip()[:120] for k in META_FIELDS if meta.get(k) is not None}


def create_active(rel: str, sha: str, size: int, meta: dict, by: str = "", note: str = "", label: str = "",
                  effective_from: Optional[str] = None, effective_until: Optional[str] = None) -> dict:
    """A new document whose first version is ACTIVE (an approved upload placed at `rel`)."""
    clean = {"title": "", "bank": "", "product": "", "category": "", **_clean_meta(meta)}
    with _lock, closing(_db()) as db:
        if db.execute("SELECT 1 FROM kb_docs WHERE path=?", (rel,)).fetchone():
            raise ValueError(f"{rel} is already a document in the knowledge base.")
        doc_id = uuid.uuid4().hex[:12]
        db.execute("INSERT INTO kb_docs(path, doc_id, title, bank, product, category, meta_source, created, updated) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (rel, doc_id, clean["title"] or _title(rel), clean["bank"],
                                                  clean["product"], clean["category"], meta.get("meta_source") or "admin",
                                                  _now(), _now()))
        v = _insert_version(db, doc_id, sha, size, rel, ACTIVE, by, note, label, effective_from, effective_until)
        db.commit()
    _invalidate()
    return {"doc_id": doc_id, "version": v, "path": rel}


def promote(doc_id: str, rel: str, sha: str, size: int, by: str = "", note: str = "", label: str = "",
            effective_from: Optional[str] = None, effective_until: Optional[str] = None, meta: Optional[dict] = None) -> dict:
    """An approved new version: in one transaction the ACTIVE version becomes ARCHIVED (its effective-until date
    set to the day before the new one starts, if empty), the new version becomes ACTIVE, and the document points
    at the new file. The caller has already put the file at `rel` (and kept the old one, see retain)."""
    with _lock, closing(_db()) as db:
        doc = db.execute("SELECT * FROM kb_docs WHERE doc_id=?", (doc_id,)).fetchone()
        if doc is None:
            raise LookupError("Document not found.")
        until = _day_before(effective_from) if effective_from else None
        db.execute("UPDATE kb_versions SET effective_until=COALESCE(effective_until, ?) WHERE doc_id=? AND status=?",
                   (until, doc_id, ACTIVE))
        db.execute("UPDATE kb_versions SET status=? WHERE doc_id=? AND status IN (?,?)", (ARCHIVED, doc_id, ACTIVE, DISABLED))
        v = _insert_version(db, doc_id, sha, size, rel, ACTIVE, by, note, label, effective_from, effective_until)
        clean = _clean_meta(meta or {})
        sets = "".join(f", {k}=?" for k in clean)
        db.execute(f"UPDATE kb_docs SET path=?, updated=?{sets}{', meta_source=?' if clean else ''} WHERE doc_id=?",
                   (rel, _now(), *clean.values(), *(["admin"] if clean else []), doc_id))
        db.commit()
    _invalidate()
    return {"doc_id": doc_id, "version": v, "path": rel, "previous_path": doc["path"]}


def merge(keep_doc_id: str, others: List[dict], by: str = "") -> dict:
    """Make other documents earlier versions of `keep_doc_id` (e.g. Code Book-3 and -4 under Code Book-5).
    `others` are catalog rows in the order they should come before the kept document's versions. Their
    versions move under the kept document, renumbered so the kept document's history stays newest; their
    ACTIVE versions become ARCHIVED. The caller has kept their files (retain) and removes them from the folder."""
    with _lock, closing(_db()) as db:
        keep = db.execute("SELECT * FROM kb_docs WHERE doc_id=?", (keep_doc_id,)).fetchone()
        if keep is None:
            raise LookupError("Document not found.")
        rows = []
        for o in others:
            rows += [dict(r) for r in db.execute("SELECT * FROM kb_versions WHERE doc_id=? ORDER BY version", (o["doc_id"],))]
        rows += [dict(r) for r in db.execute("SELECT * FROM kb_versions WHERE doc_id=? ORDER BY version", (keep_doc_id,))]
        ids = [o["doc_id"] for o in others]
        db.execute(f"DELETE FROM kb_versions WHERE doc_id IN ({','.join('?' * (len(ids) + 1))})", (*ids, keep_doc_id))
        for n, r in enumerate(rows, 1):
            if r["doc_id"] != keep_doc_id:
                r["file"] = r["file"] or next(o["path"] for o in others if o["doc_id"] == r["doc_id"])
                if r["status"] in (ACTIVE, DISABLED):
                    r["status"] = ARCHIVED
                r["note"] = (r["note"] + "; " if r["note"] else "") + (f"merged by {by}" if by else "merged")
            r.update(doc_id=keep_doc_id, version=n)
            db.execute("INSERT INTO kb_versions(doc_id, version, sha, status, size, stored, note, created, by, file, label, "
                       "effective_from, effective_until) VALUES (:doc_id,:version,:sha,:status,:size,:stored,:note,:created,"
                       ":by,:file,:label,:effective_from,:effective_until)", r)
        db.execute(f"DELETE FROM kb_docs WHERE doc_id IN ({','.join('?' * len(ids))})", ids)
        db.commit()
    _invalidate()
    return {"doc_id": keep_doc_id, "versions": len(rows)}


def cached_parts(sha: str, verified: bool = True):
    """Text of any version (current or archived): its verified text if an admin saved one (unless
    verified=False), else the extraction cache; None if neither is available."""
    from app.core.config import get_settings
    from app.services.ingestion import Part
    if verified:
        from app.services.kb_verify import parts_for
        v = parts_for(sha)
        if v:
            return v
    cache = get_settings().kb_cache_dir / f"{sha}.json"
    if not cache.exists():
        return None
    try:
        return [Part.from_dict(p) for p in json.loads(cache.read_text(encoding="utf-8"))["parts"]]
    except (ValueError, KeyError, TypeError):
        return None
