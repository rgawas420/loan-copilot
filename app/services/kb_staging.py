"""Staged uploads: an admin upload is checked before it can change what the copilot knows.

    upload ─> saved in the hidden ".pending" folder (the sync never reads it) ─> background check:
              text extracted (OCR cached), sections counted, exact duplicate (SHA-256 of the bytes),
              existing documents it may be a new version of (file name, document type, bank / product, content)
         ─> no match, not a duplicate, not an image ─> added at once as a new document (bulk uploads stay easy)
         ─> otherwise it waits for the admin (REVIEW / DUPLICATE / FAILED):
              Activate as new version of <document>: in one step the old file is kept for rollback and leaves the
                  folder, the new file takes its place, the old version becomes ARCHIVED and the new one ACTIVE.
                  The document's knowledge tests run first (app/services/kb_review.py); if any fail, activation
                  needs the admin to confirm (force).
              Add as separate document
              Reject (the upload is deleted)

Matching only suggests; the admin decides. Activation re-indexes the file (the extraction is reused from the
cache, so only the embeddings are made then). Between activation and the end of that job the document is
briefly missing from answers, never doubled.

find_groups() runs the same matching across the documents already in the folder ("Code Book-3, -4, -5"), and
merge() turns the ones the admin picks into earlier versions of the latest.
"""
import hashlib
import json
import logging
import re
import shutil
import uuid
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.core.config import get_settings
from app.services import kb_audit, kb_versions
from app.services import knowledge_base as kb
from app.services.ingestion import ALLOWED_EXTENSIONS, chunk_parts

log = logging.getLogger(__name__)
PENDING_DIR = ".pending"
CHECKING, REVIEW, DUPLICATE, FAILED = "CHECKING", "REVIEW", "DUPLICATE", "FAILED"
PUBLISHED, ACTIVATED, SEPARATE, REJECTED = "PUBLISHED", "ACTIVATED", "SEPARATE", "REJECTED"
OPEN = (CHECKING, REVIEW, DUPLICATE, FAILED)
MATCH_SCORE = 0.45                 # suggest a document at or above this similarity...
MATCH_CONTENT = 0.6                # ...or when this much of the content is the same
MAX_CANDIDATES = 3
SAMPLE_CHARS = 30000
MAX_SHINGLES = 5000
PREVIEW_CHUNKS = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_pending(id TEXT PRIMARY KEY, name TEXT, folder TEXT, stored TEXT, sha TEXT, size INTEGER,
    status TEXT, target_doc TEXT, candidates TEXT, duplicates TEXT, checks TEXT, chunks INTEGER, rows INTEGER,
    image INTEGER, title TEXT, category TEXT, bank TEXT, product TEXT, label TEXT, effective_from TEXT,
    effective_until TEXT, error TEXT, by TEXT, created TEXT, updated TEXT, decided_by TEXT, decided TEXT, result TEXT);
"""
_JSON = ("candidates", "duplicates", "checks", "result")


class StagingError(Exception):
    def __init__(self, message: str, code: int = 400, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.detail = detail                 # sent instead of the message when set (e.g. failed knowledge tests)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root() -> Path:
    from app.services import kb_admin
    return kb_admin.root()


@contextmanager
def _switch(timeout: float = 60):
    """Hold the sync lock while files move and versions switch, so a sync never sees half a switch."""
    if not kb._sync_lock.acquire(timeout=timeout):
        raise StagingError("The knowledge base is busy processing other files. Try again in a minute.", 409)
    try:
        yield
    finally:
        kb._sync_lock.release()


# ------------------------------------------------------------------ similarity
_NAME_STOP = {"the", "and", "of", "for", "to", "in", "a", "an", "copy", "final", "new", "latest", "updated", "update",
              "version", "ver", "rev", "doc", "document", "file", "wa", "img", "image", "scan", "scanned"}


def name_tokens(name: str) -> set:
    """Words of the file name that name the document: not dates, numbers, ids or filler ("copy", "final")."""
    words = re.findall(r"[a-z0-9]+", Path(name).stem.lower())
    return {w for w in words if len(w) > 1 and not any(ch.isdigit() for ch in w)
            and w not in _NAME_STOP and w not in kb_versions._MONTHS}


def _name_score(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    j = len(a & b) / len(a | b)
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return max(j, 0.75) if len(small) >= 2 and small <= big else j


def _sample(parts) -> str:
    out, n = [], 0
    for p in parts:
        t = p.text or "\n".join(x for x in [p.context, *p.rows] if x)
        out.append(t)
        n += len(t)
        if n >= SAMPLE_CHARS:
            break
    return "\n".join(out)[:SAMPLE_CHARS]


def shingles(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i:i + 3]) for i in range(min(len(words) - 2, MAX_SHINGLES))} if len(words) >= 3 else set()


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _features(name: str, meta: dict, text: Optional[str]) -> dict:
    return {"name": Path(name).name, "tokens": name_tokens(name), "category": meta.get("category") or "",
            "bank": meta.get("bank") or "", "product": meta.get("product") or "",
            "shingles": shingles(text) if text else set()}


def similarity(a: dict, b: dict) -> tuple:
    """(score 0..1, content overlap 0..1, reasons) for two documents' features."""
    reasons = []
    same_name = a["name"].lower() == b["name"].lower()
    name = 1.0 if same_name else _name_score(a["tokens"], b["tokens"])
    if same_name:
        reasons.append("same file name")
    elif name >= 0.5:
        reasons.append("similar name (" + ", ".join(sorted(a["tokens"] & b["tokens"])) + ")")
    content = _jaccard(a["shingles"], b["shingles"])
    if content >= 0.1:
        reasons.append(f"{round(content * 100)}% of the content is the same")
    cat = a["category"] and a["category"] == b["category"] and a["category"] != "General"
    if cat:
        reasons.append(f"same document type ({a['category']})")
    score = 0.45 * name + 0.4 * content + (0.15 if cat else 0.0)
    if a["bank"] and b["bank"]:
        if a["bank"] != b["bank"]:
            score *= 0.3
            reasons.append(f"different bank ({a['bank']} / {b['bank']})")
        else:
            reasons.append(f"same bank ({a['bank']})")
    elif a["product"] and b["product"] and a["product"] != b["product"]:
        score *= 0.6
    return round(score, 3), round(content, 3), reasons


def _doc_features(d: dict) -> dict:
    parts = kb_versions.cached_parts(d["sha"]) if d.get("sha") else None
    return _features(d["path"], d, _sample(parts) if parts else None)


def candidates(name: str, meta: dict, text: str, exclude: set = frozenset()) -> List[dict]:
    """Existing documents this file may be a new version of, best first."""
    new = _features(name, meta, text)
    out = []
    for d in kb_versions.catalog():
        if d["doc_id"] in exclude:
            continue
        score, content, reasons = similarity(new, _doc_features(d))
        if score >= MATCH_SCORE or content >= MATCH_CONTENT:
            out.append({"doc_id": d["doc_id"], "path": d["path"], "title": d["title"], "category": d["category"],
                        "version": d["version"], "label": d["label"] or "", "effective_from": d["effective_from"],
                        "score": score, "reasons": reasons})
    return sorted(out, key=lambda c: -c["score"])[:MAX_CANDIDATES]


# ------------------------------------------------------------------ records
def _get(pid: str) -> Optional[dict]:
    with closing(kb._db()) as db:
        r = db.execute("SELECT * FROM kb_pending WHERE id=?", (pid,)).fetchone()
    if r is None:
        return None
    row = dict(r)
    for k in _JSON:
        row[k] = json.loads(row[k]) if row[k] else ([] if k != "result" else None)
    return row


def _set(pid: str, **fields):
    for k in _JSON:
        if k in fields and fields[k] is not None:
            fields[k] = json.dumps(fields[k])
    fields["updated"] = _now()
    with closing(kb._db()) as db:
        db.execute(f"UPDATE kb_pending SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), pid))
        db.commit()


def _view(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "stored"}


def listing(recent: int = 15) -> List[dict]:
    """Uploads waiting for a decision (or still being checked), then the most recent decisions."""
    with closing(kb._db()) as db:
        ids = [r[0] for r in db.execute(f"SELECT id FROM kb_pending WHERE status IN ({','.join('?' * len(OPEN))}) "
                                        "ORDER BY created", OPEN)]
        ids += [r[0] for r in db.execute(f"SELECT id FROM kb_pending WHERE status NOT IN ({','.join('?' * len(OPEN))}) "
                                         "ORDER BY updated DESC LIMIT ?", (*OPEN, recent))]
    return [_view(_get(i)) for i in ids]


def open_ids() -> List[str]:
    with closing(kb._db()) as db:
        return [r[0] for r in db.execute("SELECT id FROM kb_pending WHERE status=?", (CHECKING,))]


# ------------------------------------------------------------------ upload and check
def stage(filename: str, data: bytes, folder: str = "", target_path: str = "", by: str = "") -> dict:
    """Keep an upload aside for checking. target_path: an existing document the admin says this updates."""
    from app.services import kb_admin
    s = get_settings()
    try:
        name, folder = kb_admin.clean_name(filename), kb_admin.clean_folder(folder)
    except kb_admin.KBError as e:
        raise StagingError(str(e), e.code)
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise StagingError(f"{name}: unsupported file type. Use: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    limit = min(s.kb_upload_max_mb, s.kb_max_file_mb)
    if len(data) > limit * 1024 * 1024:
        raise StagingError(f"{name}: larger than {limit} MB.", 413)
    if not data:
        raise StagingError(f"{name}: the file is empty.")
    target = None
    if target_path:
        doc = kb_versions.get_doc(target_path)
        if doc is None:
            raise StagingError(f"{target_path} is not a document in the knowledge base.", 404)
        target = doc["doc_id"]
    pid = uuid.uuid4().hex[:12]
    d = _root() / PENDING_DIR / pid
    d.mkdir(parents=True)
    (d / name).write_bytes(data)
    with closing(kb._db()) as db:
        db.execute("INSERT INTO kb_pending(id, name, folder, stored, sha, size, status, target_doc, by, created, updated) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, name, folder, str(d / name), hashlib.sha256(data).hexdigest(),
                                                       len(data), CHECKING, target, by, _now(), _now()))
        db.commit()
    kb_audit.record("upload", by, f"{folder}/{name}" if folder else name, doc_id=target,
                    detail=f"staged for checking ({len(data):,} bytes)" + (f"; for {target_path}" if target_path else ""))
    return _view(_get(pid))


def _figures(text: str) -> int:
    return len(re.findall(r"\d+(?:\.\d+)?\s*(?:%|percent\b)|(?:₹|rs\.?)\s*\d|\b\d{6}\b|\b\d[\d,]{3,}\b", text, re.I))


def process(pid: str):
    """The background check of one staged upload (see the module docstring)."""
    row = _get(pid)
    if row is None or row["status"] != CHECKING:
        return
    from app.services import kb_verify
    path, name = Path(row["stored"]), row["name"]
    checks = [{"check": "File received", "ok": True, "detail": f"{row['size']:,} bytes"}]
    try:
        parts = kb._extract_cached(path, row["sha"])
        text = _sample(parts)
        if not text.strip():
            raise ValueError("no readable text")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
        _set(pid, status=FAILED, error=err, checks=checks + [{"check": "Text extracted", "ok": False, "detail": err}])
        return
    meta = kb_versions.infer(name, text)
    period = kb_versions.parse_period(name)
    extraction = kb_verify.kind(parts, path.suffix)
    image = extraction == kb_verify.OCR                  # read by OCR and not verified yet
    chunks = len(chunk_parts(parts, name, {}))
    rows = sum(len(p.rows) for p in parts)
    dups = [d for d in kb_versions.find_sha(row["sha"])]
    with closing(kb._db()) as db:
        dups += [{"pending": True, "path": r["name"], "status": r["status"], "created": r["created"]}
                 for r in db.execute(f"SELECT name, status, created FROM kb_pending WHERE sha=? AND id<>? AND status IN "
                                     f"({','.join('?' * len(OPEN))})", (row["sha"], pid, *OPEN))]
    cands = candidates(name, meta, text)
    if row["target_doc"] and not any(c["doc_id"] == row["target_doc"] for c in cands):
        d = next((x for x in kb_versions.catalog() if x["doc_id"] == row["target_doc"]), None)
        if d:
            score, _, reasons = similarity(_features(name, meta, text), _doc_features(d))
            cands.insert(0, {"doc_id": d["doc_id"], "path": d["path"], "title": d["title"], "category": d["category"],
                             "version": d["version"], "label": d["label"] or "", "effective_from": d["effective_from"],
                             "score": score, "reasons": ["chosen at upload"] + reasons})
    elif row["target_doc"]:
        cands.sort(key=lambda c: c["doc_id"] != row["target_doc"])
    dup_text = "; ".join(f"{'upload waiting: ' if d.get('pending') else ''}{d['path']}"
                         + (f" (v{d['version']}, {d['status']})" if not d.get("pending") else "") for d in dups[:3])
    checks += [
        {"check": "Text extracted", "ok": True, "detail": f"{len(text):,} characters" + (
            " (by OCR)" if image else " (verified by an admin)" if extraction == kb_verify.VERIFIED else "")},
        {"check": "Sections created", "ok": chunks > 0, "detail": f"{chunks} sections" + (f", {rows:,} table rows" if rows else "")},
        {"check": "Not a duplicate", "ok": not dups, "detail": f"Exactly the same file: {dup_text}" if dups else "No identical file"},
        {"check": "Existing document", "ok": True, "info": True,
         "detail": f"Possible new version of: {cands[0]['title']}" if cands else "No match: a new document"},
        {"check": "Effective date", "ok": bool(period), "warn": True,
         "detail": kb_versions.period_label(period) if period else "Not in the file name: enter it before activating"},
        {"check": "Document type", "ok": meta["category"] != "General", "warn": True, "detail": meta["category"]},
        {"check": "Important figures", "ok": True, "info": True,
         "detail": f"{_figures(text)} rates, amounts, codes or PIN codes found"},
    ]
    if cands:
        t = _tests_for(cands[0]["doc_id"], parts, name)
        if t and t["total"]:
            checks.append({"check": "Knowledge tests", "ok": not t["failed"], "warn": True,
                           "detail": f"{t['passed']} of {t['total']} tests for {cands[0]['title']} find their expected answer"
                                     + (f"; {t['failed']} do not" if t["failed"] else "")})
    if image:
        from app.services import ocr_review
        ocr_review.start(row["sha"], path)              # page images, OCR confidence, second reading: in the background
        checks.append({"check": "Image-based", "ok": False, "warn": True,
                       "detail": "Read by OCR, which can misread rates, amounts and codes. Use “Verify text” to check "
                                 "the flagged values against the page images and correct them before adding it."})
    elif extraction == kb_verify.VERIFIED:
        v = kb_verify.get(row["sha"]) or {}
        checks.append({"check": "Image-based", "ok": True,
                       "detail": f"Text verified by {v.get('by') or 'an admin'}; it is used instead of the OCR output"})
    status = DUPLICATE if dups else REVIEW if (cands or row["target_doc"] or image) else None
    _set(pid, status=status or CHECKING, candidates=cands, duplicates=dups, checks=checks, chunks=chunks, rows=rows,
         image=int(image), title=meta["title"], category=meta["category"], bank=meta["bank"], product=meta["product"],
         label=kb_versions.period_label(period), effective_from=period.isoformat() if period else None, error=None)
    if status is None:                                   # nothing it could conflict with: add it now
        try:
            _add_separate(_get(pid), {}, row["by"] or "", auto=True, timeout=600)
        except StagingError as e:
            checks.append({"check": "Added to the knowledge base", "ok": False, "detail": str(e)})
            _set(pid, status=REVIEW, checks=checks)


# ------------------------------------------------------------------ review: comparison and knowledge tests
def _tests_for(doc_id: str, new_parts, new_name: str) -> Optional[dict]:
    from app.services import kb_review
    doc = kb_versions.doc_by_id(doc_id)
    if doc is None:
        return None
    old_parts, _ = kb_review.version_parts(doc["path"])
    return kb_review.knowledge_tests(doc, kb_review.full_text(new_parts), kb_review.full_text(old_parts),
                                     [new_name])


def review(pid: str, doc_id: str) -> dict:
    """What activating this upload as the new version of doc_id would change, and its knowledge tests."""
    from app.services import kb_review
    row = _get(pid)
    if row is None:
        raise StagingError("Upload not found.", 404)
    doc = kb_versions.doc_by_id(doc_id)
    if doc is None:
        raise StagingError("Document not found.", 404)
    new_parts = kb_versions.cached_parts(row["sha"])
    if new_parts is None:
        raise StagingError("This upload has not been read yet.", 409)
    old_parts, current = kb_review.version_parts(doc["path"])
    return {"document": {"doc_id": doc_id, "path": doc["path"], "title": doc["title"]},
            "current": {k: current.get(k) for k in ("version", "label", "effective_from", "file")} if current else None,
            "new": {"name": row["name"], "label": row["label"], "effective_from": row["effective_from"]},
            "comparison": kb_review.compare(old_parts or [], new_parts),
            "tests": kb_review.knowledge_tests(doc, kb_review.full_text(new_parts), kb_review.full_text(old_parts),
                                               [row["name"]])}


# ------------------------------------------------------------------ decisions
def _clean_fields(fields: dict) -> dict:
    out = {}
    for k in ("title", "category", "label", "folder", "source_type"):
        if fields.get(k) is not None:
            out[k] = str(fields[k]).strip()[:120]
    for k in ("effective_from", "effective_until"):
        v = (fields.get(k) or "").strip()
        if v:
            try:
                out[k] = date.fromisoformat(v).isoformat()
            except ValueError:
                raise StagingError(f"{k.replace('_', ' ').capitalize()} must be a date (YYYY-MM-DD).")
        elif k in fields:
            out[k] = None
    if out.get("effective_from") and out.get("effective_until") and out["effective_until"] < out["effective_from"]:
        raise StagingError("Effective until is before effective from.")
    return out


def _place(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.placing")
    shutil.copy2(src, tmp)
    tmp.replace(dst)                                     # atomic: the sync never sees a half-written file


def _drop_stored(row: dict):
    shutil.rmtree(Path(row["stored"]).parent, ignore_errors=True)


def _pick(row: dict, f: dict, key: str):
    return f[key] if key in f else row.get(key)


def _add_separate(row: dict, f: dict, by: str, auto: bool = False, timeout: float = 60) -> dict:
    from app.services import kb_admin
    folder = kb_admin.clean_folder(f["folder"]) if f.get("folder") is not None else row["folder"]
    rel = f"{folder}/{row['name']}" if folder else row["name"]
    with _switch(timeout):
        if (_root() / rel).exists():
            raise StagingError(f"A file named {rel} is already in the knowledge base. Activate this upload as its new "
                               "version, or reject it and upload it under another name.", 409)
        _place(Path(row["stored"]), kb_admin.resolve(rel))
        meta = {"title": _pick(row, f, "title"), "category": _pick(row, f, "category"), "bank": row["bank"],
                "product": row["product"], "meta_source": "auto" if auto else "admin"}
        res = kb_versions.create_active(rel, row["sha"], row["size"], meta, by=by, note="added automatically (no match)"
                                        if auto else "added as a new document", label=_pick(row, f, "label") or "",
                                        effective_from=_pick(row, f, "effective_from"),
                                        effective_until=_pick(row, f, "effective_until"))
        if f.get("source_type"):
            kb_versions.set_authority(rel, f["source_type"], None)
    kb_audit.record("add", by, rel, res["doc_id"], res["version"],
                    "added automatically: no matching document" if auto else "added as a separate document")
    _set(row["id"], status=PUBLISHED if auto else SEPARATE, result=res, decided_by=by or "auto", decided=_now())
    _drop_stored(row)
    kb_admin.enqueue("files", [rel], by=by)
    return res


def _activate_version(row: dict, doc_id: str, f: dict, by: str, force: bool = False) -> dict:
    from app.services import kb_admin
    doc = kb_versions.doc_by_id(doc_id)
    if doc is None or not any(d["doc_id"] == doc_id for d in kb_versions.catalog()):
        raise StagingError("That document is not in the knowledge base any more.", 404)
    tests = _tests_for(doc_id, kb_versions.cached_parts(row["sha"]) or [], row["name"]) or {"failed": 0, "tests": []}
    if tests["failed"] and not force:
        msg = (f"{tests['failed']} of {tests['total']} knowledge tests for {doc['title']} do not find their expected "
               "answer in this version. Check them, or activate anyway.")
        raise StagingError(msg, 409, {"code": "tests_failed", "message": msg,
                                      "tests": [t for t in tests["tests"] if t["status"] == "FAIL"]})
    root = _root()
    cur = next(d for d in kb_versions.catalog() if d["doc_id"] == doc_id)
    old_rel = doc["path"]
    folder = old_rel.rpartition("/")[0]
    new_rel = f"{folder}/{row['name']}" if folder else row["name"]
    with _switch():
        old, new = root / old_rel, kb_admin.resolve(new_rel)
        if new_rel != old_rel and new.exists():
            raise StagingError(f"Another file named {new_rel} is in the knowledge base. Archive or rename it first.", 409)
        if old.is_file():                                  # keep the current version so it can be rolled back
            kb_versions.retain(root, old_rel, old, kb._hash_file(old))
        _place(Path(row["stored"]), new)
        if new_rel != old_rel and old.is_file():
            old.unlink()
        meta = {k: f[k] for k in ("title", "category") if f.get(k)}
        note = f"uploaded as {row['name']}" + (f"; activated with {tests['failed']} failing knowledge test(s)" if tests["failed"] else "")
        res = kb_versions.promote(doc_id, new_rel, row["sha"], row["size"], by=by, note=note,
                                  label=_pick(row, f, "label") or "", effective_from=_pick(row, f, "effective_from"),
                                  effective_until=_pick(row, f, "effective_until"), meta=meta)
        if f.get("source_type"):
            kb_versions.set_authority(new_rel, f["source_type"], doc.get("priority"))
    res = {**res, "tests_failed": tests["failed"]}
    name = lambda v, label: f"v{v}" + (f" ({label})" if label else "")
    kb_audit.record("activate", by, new_rel, doc_id, res["version"],
                    f"{name(res['version'], _pick(row, f, 'label'))} active ({row['name']}); "
                    f"{name(cur['version'], cur['label'])} archived ({old_rel})"
                    + (f"; activated although {tests['failed']} knowledge test(s) failed" if tests["failed"] else ""))
    _set(row["id"], status=ACTIVATED, result=res, decided_by=by, decided=_now())
    _drop_stored(row)
    kb_admin.enqueue("files", list(dict.fromkeys([old_rel, new_rel])), by=by)
    return res


def decide(pid: str, action: str, doc_id: Optional[str] = None, fields: Optional[dict] = None, by: str = "",
           force: bool = False) -> dict:
    """action: 'version' (activate as a new version of doc_id), 'separate' (a new document) or 'reject'.
    force: activate although knowledge tests fail."""
    row = _get(pid)
    if row is None:
        raise StagingError("Upload not found.", 404)
    if row["status"] == CHECKING:
        raise StagingError("This upload is still being checked.", 409)
    if row["status"] not in OPEN:
        raise StagingError("This upload has already been decided.", 409)
    if action == "reject":
        _set(pid, status=REJECTED, decided_by=by, decided=_now())
        kb_audit.record("reject", by, row["name"], detail=f"upload rejected ({row['status'].lower()})")
        _drop_stored(row)
        return {"rejected": row["name"]}
    if row["status"] == FAILED:
        raise StagingError("This file could not be read, so it can only be rejected.", 409)
    f = _clean_fields(fields or {})
    if action == "separate":
        return _add_separate(row, f, by)
    if action == "version":
        if not doc_id:
            raise StagingError("Choose the document this is a new version of.")
        return _activate_version(row, doc_id, f, by, force)
    raise StagingError("Unknown action.")


def recheck(pid: str) -> bool:
    """Check an open upload again (after its text was verified): back to CHECKING, then the background check."""
    from app.services import kb_admin
    row = _get(pid)
    if row is None or row["status"] not in OPEN or row["status"] == CHECKING:
        return False
    _set(pid, status=CHECKING, error=None)
    kb_admin.enqueue("stage", [pid])
    return True


def preview(pid: str) -> dict:
    row = _get(pid)
    if row is None:
        raise StagingError("Upload not found.", 404)
    parts = kb_versions.cached_parts(row["sha"])
    if parts is None:
        return {"id": pid, "chunks": [], "total": 0, "message": "Not read yet." if row["status"] == CHECKING
                else "The extracted text is not available."}
    docs = chunk_parts(parts, row["name"], {})
    return {"id": pid, "total": len(docs), "chunks": [d.page_content for d in docs[:PREVIEW_CHUNKS]]}


# ------------------------------------------------------------------ duplicates already in the folder
def _order_key(d: dict):
    """Oldest first: the month in the name, then a trailing edition number ("Code Book-5"), then when added."""
    period = kb_versions.parse_period(d["path"])
    num = re.findall(r"(\d+)\D*$", Path(d["path"]).stem)
    return (period or date.min, int(num[-1]) if num and len(num[-1]) <= 3 else -1, d.get("version_created") or "")


def find_groups() -> List[dict]:
    """Documents in the knowledge base that look like versions of one another (e.g. Code Book-3, -4, -5)."""
    docs = kb_versions.catalog()
    feats = {d["doc_id"]: _features(d["path"], d, None) for d in docs}
    parent = {d["doc_id"]: d["doc_id"] for d in docs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    loaded: set = set()

    def load(d):                                         # content only for plausible pairs (it reads the cache)
        if d["doc_id"] not in loaded:
            loaded.add(d["doc_id"])
            parts = kb_versions.cached_parts(d["sha"]) if d.get("sha") else None
            feats[d["doc_id"]]["shingles"] = shingles(_sample(parts)) if parts else set()

    reasons: Dict[frozenset, List[str]] = {}
    for i, a in enumerate(docs):
        for b in docs[i + 1:]:
            fa, fb = feats[a["doc_id"]], feats[b["doc_id"]]
            same = a["sha"] == b["sha"]
            quick = 0.45 * (1.0 if fa["name"].lower() == fb["name"].lower() else _name_score(fa["tokens"], fb["tokens"]))
            related = quick >= 0.25 or (fa["category"] and fa["category"] == fb["category"] and fa["category"] != "General")
            if not (same or related):
                continue
            load(a), load(b)
            score, content, why = similarity(fa, fb)
            if same or score >= MATCH_SCORE + 0.05 or content >= MATCH_CONTENT:
                parent[find(a["doc_id"])] = find(b["doc_id"])
                reasons[frozenset((a["doc_id"], b["doc_id"]))] = (["identical file"] if same else []) + why
    groups: Dict[str, List[dict]] = {}
    for d in docs:
        groups.setdefault(find(d["doc_id"]), []).append(d)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=_order_key)
        ids = {m["doc_id"] for m in members}
        why = sorted({r for k, v in reasons.items() if k <= ids for r in v})
        out.append({"members": [{"doc_id": m["doc_id"], "path": m["path"], "title": m["title"], "category": m["category"],
                                 "label": m["label"] or "", "effective_from": m["effective_from"], "size": m["size"]}
                                for m in members],
                    "latest": members[-1]["path"], "reasons": why})
    return sorted(out, key=lambda g: g["members"][0]["title"].lower())


def merge(keep_path: str, other_paths: List[str], by: str = "") -> dict:
    """Make the other documents earlier versions of keep_path: their files leave the folder (kept for rollback)
    and their versions become ARCHIVED versions of the kept document."""
    from app.services import kb_admin
    if not other_paths or keep_path in other_paths:
        raise StagingError("Choose the document to keep and at least one other.")
    cat = {d["path"]: d for d in kb_versions.catalog()}
    missing = [p for p in [keep_path, *other_paths] if p not in cat]
    if missing:
        raise StagingError(f"Not an active document: {', '.join(missing)}", 404)
    others = sorted((cat[p] for p in other_paths), key=_order_key)
    root = _root()
    with _switch():
        for o in others:
            f = root / o["path"]
            if f.is_file():
                kb_versions.retain(root, o["path"], f, kb._hash_file(f))
        res = kb_versions.merge(cat[keep_path]["doc_id"], others, by)
        for o in others:
            f = root / o["path"]
            if f.is_file():
                f.unlink()
                kb_admin._prune_empty_dirs(f.parent, root)
    kb_admin.enqueue("files", [o["path"] for o in others], by=by)
    kb_audit.record("merge", by, keep_path, res["doc_id"],
                    detail=f"kept {keep_path}; now earlier versions: {', '.join(o['path'] for o in others)}")
    return {**res, "kept": keep_path, "archived": [o["path"] for o in others]}
