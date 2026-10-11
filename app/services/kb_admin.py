"""Admin document management for the Urban Money knowledge base.

The folder stays the source of truth: it holds the ACTIVE file of every document. Uploads are staged and
checked first (app/services/kb_staging.py) and reach the folder as a new document or, after an admin decision,
as a new version; archived files move to its hidden ".archive" subfolder (which sync ignores), and deletes
remove the file. Replaced versions are kept in the hidden ".versions" subfolder, so they can be re-activated
(see kb_versions). Every change is then applied to the vector store and keyword index by
knowledge_base.sync(only=...), run one job at a time by a background worker so uploads, checks, reprocessing
and full syncs never overlap.
"""
import logging
import re
import shutil
import threading
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.core.config import BASE_DIR, get_settings
from app.services import kb_versions
from app.services import knowledge_base as kb
from app.services.ingestion import ALLOWED_EXTENSIONS

log = logging.getLogger(__name__)
ARCHIVE = ".archive"
PREVIEW_CHUNKS = 40


class KBError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def root() -> Path:
    r = kb.kb_root()
    if r is None:
        configured = get_settings().urban_money_dir
        if configured and str(configured) not in ("", "."):
            raise KBError("The knowledge-base folder set in URBAN_MONEY_DIR does not exist.", 503)
        r = BASE_DIR / "data" / "URBAN MONEY"
        r.mkdir(parents=True, exist_ok=True)
    return r


# ------------------------------------------------------------------ safe names and paths
_BAD = re.compile(r'[<>:"|?*\x00-\x1f]')


def clean_name(name: str) -> str:
    name = _BAD.sub("_", Path(name.replace("\\", "/")).name).strip().strip(".")
    if not name or name.startswith("~$"):
        raise KBError("That file name is not allowed.")
    return name[:180]


def clean_folder(folder: str) -> str:
    parts = []
    for seg in re.split(r"[\\/]+", folder or ""):
        seg = _BAD.sub("_", seg).strip()
        if not seg or seg == ".":
            continue
        if seg == ".." or seg.startswith("."):
            raise KBError("Folder names cannot start with a dot.")
        parts.append(seg[:80])
    return "/".join(parts)


def resolve(rel: str, base: Optional[Path] = None) -> Path:
    """Folder-relative path -> absolute path, refusing anything that escapes the folder."""
    base = (base or root()).resolve()
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel or any(p in ("", ".", "..") for p in rel.split("/")):
        raise KBError("Invalid file path.")
    p = (base / rel).resolve()
    if base not in p.parents:
        raise KBError("Invalid file path.")
    return p


def _archive_root() -> Path:
    return root() / ARCHIVE


# ------------------------------------------------------------------ background jobs
_jobs: deque = deque()
_cv = threading.Condition()
_worker: Optional[threading.Thread] = None
state = {"current": None, "last": None}


def _ensure_worker():
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run_jobs, name="kb-jobs", daemon=True)
        _worker.start()


def enqueue(kind: str, paths: Optional[List[str]] = None, full: bool = False, by: str = ""):
    """kind 'files' (process just these paths), 'sync' (the whole folder) or 'stage' (check these staged uploads)."""
    with _cv:
        _jobs.append({"kind": kind, "paths": set(paths or []), "full": full, "by": by, "queued": _now()})
        _cv.notify()
    _ensure_worker()


def queued_paths() -> set:
    with _cv:
        out = set()
        for j in _jobs:
            out |= j["paths"]
        return out


def _take():
    with _cv:
        while not _jobs:
            _cv.wait()
        job = _jobs.popleft()
        if job["kind"] == "files":                 # merge waiting file jobs into one pass
            while _jobs and _jobs[0]["kind"] == "files":
                nxt = _jobs.popleft()
                job["paths"] |= nxt["paths"]
                job["full"] = job["full"] or nxt["full"]
        return job


def _run_jobs():
    while True:
        job = _take()
        state["current"] = {"kind": job["kind"], "files": len(job["paths"]), "full": job["full"], "started": _now()}
        if job["kind"] == "stage":
            _check_staged(job["paths"])
            state["current"] = None
            continue
        try:
            from app.rag.vectorstore import get_vectorstore
            get_vectorstore()                      # build first: an in-memory build runs its own sync
            while True:
                try:
                    report = kb.sync(full=job["full"], only=job["paths"] if job["kind"] == "files" else None,
                                     by=job["by"])
                    break
                except RuntimeError:               # another sync (startup, /api/kb/sync) holds the lock
                    time.sleep(2)
            state["last"] = {"kind": job["kind"], "finished": _now(), "report": kb._summary(report)}
            from app.services import rag_debug
            rag_debug.autorun_after_documents_changed(report)
        except Exception as e:
            log.exception("Knowledge-base job failed")
            state["last"] = {"kind": job["kind"], "finished": _now(), "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            state["current"] = None


def _check_staged(ids):
    from app.services import kb_staging
    for pid in sorted(ids):
        try:
            kb_staging.process(pid)
        except Exception as e:
            log.exception("Checking staged upload %s failed", pid)
            kb_staging._set(pid, status=kb_staging.FAILED, error=f"{type(e).__name__}: {e}"[:300])


def busy() -> bool:
    with _cv:
        return bool(_jobs) or state["current"] is not None or kb.status["running"]


# ------------------------------------------------------------------ listing
def _manifest() -> dict:
    with closing(kb._db()) as db:
        return {r["path"]: dict(r) for r in db.execute("SELECT * FROM kb_files WHERE store=?", (kb._store_key(),))}


def _info(p: Path, base: Path) -> dict:
    st = p.stat()
    rel = p.relative_to(base).as_posix()
    return {"path": rel, "name": p.name, "folder": rel.rpartition("/")[0], "type": p.suffix.lower().lstrip("."),
            "size": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "_mtime": st.st_mtime}


def overview() -> dict:
    base = root()
    max_bytes = get_settings().kb_max_file_mb * 1024 * 1024
    rows, waiting = _manifest(), queued_paths()
    docs = kb_versions.docs_by_path()
    authority = kb_versions.authority_map()
    current = kb.status.get("progress") or ""
    files = []
    for p in kb.discover(base):
        f = _info(p, base)
        mtime = f.pop("_mtime")
        row = rows.get(f["path"])
        unchanged = bool(row) and row["size"] == f["size"] and row["mtime"] == mtime
        if ("." + f["type"]) not in ALLOWED_EXTENSIONS:
            st = "unsupported"
        elif f["size"] > max_bytes:
            st = "too_large"
        elif f["path"] in waiting:
            st = "queued"
        elif current.endswith(" " + f["path"]) or f" {f['path']} (chunks" in current:
            st = "processing"
        elif unchanged and row["status"] != "indexing":
            st = row["status"]                     # indexed, keyword or failed
        else:
            st = "pending"                         # new or changed since the last sync
        d = docs.get(f["path"]) or {}
        files.append({**f, "status": st, "chunks": row["chunks"] if unchanged else None,
                      "error": row["error"] if unchanged else None, "processed": row["ts"] if unchanged else None,
                      "title": d.get("title") or "", "bank": d.get("bank") or "", "product": d.get("product") or "",
                      "category": d.get("category") or "", "version": d.get("active_version"),
                      "versions": d.get("versions") or 0, "label": d.get("label") or "",
                      "effective_from": d.get("effective_from"), "effective_until": d.get("effective_until"),
                      "extraction": d.get("extraction") or "",
                      **({k: authority[f["path"]][k] for k in ("priority", "source_label", "notes")}
                         if f["path"] in authority else {})})
    arch = _archive_root()
    archived = [{k: v for k, v in _info(p, arch).items() if k != "_mtime"} for p in kb.discover(arch)] if arch.is_dir() else []
    folders = sorted({f["folder"] for f in files if f["folder"]})
    counts: dict = {}
    for f in files:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    from app.rag.vectorstore import is_pinecone
    from app.services import kb_staging
    s = get_settings()
    return {
        "pending": kb_staging.listing(),
        "documents": [{"doc_id": d["doc_id"], "path": d["path"], "title": d["title"], "category": d["category"],
                       "label": d["label"] or "", "version": d["version"]} for d in kb_versions.catalog()],
        "doc_types": kb_versions.DOC_TYPES,
        "source_types": [{"key": k, "label": v[0], "priority": v[1]} for k, v in kb_versions.SOURCE_TYPES.items()],
        "source_defaults": {c: kb_versions.default_source(c) for c in kb_versions.DOC_TYPES},
        "folder": str(base), "store": f"Pinecone ({s.pinecone_index_name})" if is_pinecone() else "In-memory",
        "allowed_types": sorted(e.lstrip(".") for e in ALLOWED_EXTENSIONS),
        "max_upload_mb": min(s.kb_upload_max_mb, s.kb_max_file_mb),
        "files": files, "archived": archived, "folders": folders, "counts": counts,
        "total_size": sum(f["size"] for f in files),
        "chunks": sum(f["chunks"] or 0 for f in files),
        "jobs": {"busy": busy(), "queued": len(_jobs), "current": state["current"], "last": state["last"],
                 "progress": kb.status.get("progress") or "", "sync": kb.status},
    }


# ------------------------------------------------------------------ changes
def delete_file(rel: str) -> str:
    p = resolve(rel)
    if not p.is_file():
        raise KBError("File not found.", 404)
    p.unlink()
    _prune_empty_dirs(p.parent, root())
    return rel


def archive_file(rel: str) -> str:
    src = resolve(rel)
    if not src.is_file():
        raise KBError("File not found.", 404)
    dst = resolve(rel, _archive_root())
    if dst.exists():
        dst = dst.with_name(f"{dst.stem} ({datetime.now():%Y%m%d-%H%M%S}){dst.suffix}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    _prune_empty_dirs(src.parent, root())
    return rel


def restore_file(rel: str) -> str:
    arch = _archive_root()
    src = resolve(rel, arch)
    if not src.is_file():
        raise KBError("Archived file not found.", 404)
    dst = resolve(rel)
    if dst.exists():
        raise KBError(f"{rel} already exists in the knowledge base. Delete or archive that one first.", 409)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    _prune_empty_dirs(src.parent, arch)
    return rel


def delete_archived(rel: str) -> str:
    arch = _archive_root()
    p = resolve(rel, arch)
    if not p.is_file():
        raise KBError("Archived file not found.", 404)
    p.unlink()
    _prune_empty_dirs(p.parent, arch)
    return rel


def _prune_empty_dirs(d: Path, stop: Path):
    stop = stop.resolve()
    d = d.resolve()
    while d != stop and stop in d.parents:
        try:
            d.rmdir()                              # only succeeds when empty
        except OSError:
            return
        d = d.parent


def file_path(rel: str, archived: bool = False) -> Path:
    p = resolve(rel, _archive_root() if archived else None)
    if not p.is_file():
        raise KBError("File not found.", 404)
    return p


def preview(rel: str) -> dict:
    """The text the knowledge base holds for a file (from the extraction cache; never runs OCR here)."""
    p = file_path(rel)
    base = root()
    row = _manifest().get(rel)
    st = p.stat()
    if not row or row["size"] != st.st_size or row["mtime"] != st.st_mtime:
        return {"path": rel, "processed": False, "chunks": [], "total": 0,
                "message": "This file has not been processed yet. Process it to see its text."}
    if row["status"] == "failed":
        return {"path": rel, "processed": True, "chunks": [], "total": 0, "message": f"Could not read: {row['error']}"}
    if not (get_settings().kb_cache_dir / f"{row['sha']}.json").exists():
        return {"path": rel, "processed": True, "chunks": [], "total": row["chunks"],
                "message": "The extracted text is not cached. Reprocess the file to preview it."}
    docs = kb.chunk_file(p, base, row["sha"])
    return {"path": rel, "processed": True, "total": len(docs), "status": row["status"],
            "chunks": [d.page_content for d in docs[:PREVIEW_CHUNKS]]}


def test_search(query: str, loan_type: Optional[str] = None) -> dict:
    """The retrieval the copilot would do for this question: plan, scored candidates, selection, evidence check."""
    from app.rag.retrieval import retrieve
    r = retrieve(query, loan_type=loan_type, use_llm=False)
    t = r.trace()
    results = []
    for c in t["candidates"]:
        if c["selected"]:
            results.append({**c, "primary": c["corpus"] == kb.CORPUS,
                            "match": "exact" if c["exact"] else "keyword" if "lexical" in c["found_by"] and "vector" not in c["found_by"] else "semantic"})
    return {"results": results, "trace": t}


# ------------------------------------------------------------------ versions and document metadata
def document(rel: str) -> dict:
    p = file_path(rel)
    doc = kb_versions.get_doc(rel)
    if doc is None:
        return {"path": rel, "processed": False, "versions": [],
                "suggested": kb_versions.infer(rel), "message": "Process this file first to see its versions."}
    return {"path": rel, "processed": True, "document": doc, "versions": kb_versions.versions(rel),
            "current_sha": kb._hash_file(p)[:12]}


def update_document(rel: str, changes: dict) -> dict:
    file_path(rel)
    try:
        return kb_versions.update_meta(rel, changes)
    except LookupError as e:
        raise KBError(str(e), 404)
    except ValueError as e:
        raise KBError(str(e))


def activate_version(rel: str, version: int, by: str = "") -> dict:
    """Rollback / forward to another version (it comes back under its own file name). Returns the version with
    `path` and `previous_path`; the caller re-indexes both."""
    p = file_path(rel)
    if not kb._sync_lock.acquire(timeout=60):
        raise KBError("The knowledge base is busy processing other files. Try again in a minute.", 409)
    try:
        return kb_versions.activate(root(), rel, version, p, kb._hash_file(p), by)
    except LookupError as e:
        raise KBError(str(e), 404)
    except ValueError as e:
        raise KBError(str(e), 409)
    finally:
        kb._sync_lock.release()


def version_preview(rel: str, version: int) -> dict:
    """Extracted text of any version, including archived ones (from the extraction cache)."""
    from app.services.ingestion import chunk_parts
    for v in kb_versions.versions(rel):
        if v["version"] == version:
            parts = kb_versions.cached_parts(v["sha"])
            if parts is None:
                return {"path": rel, "version": version, "chunks": [], "total": 0,
                        "message": "The extracted text of this version is no longer cached."}
            docs = chunk_parts(parts, rel, {})
            return {"path": rel, "version": version, "status": v["status"], "total": len(docs),
                    "chunks": [d.page_content for d in docs[:PREVIEW_CHUNKS]]}
    raise KBError("Version not found.", 404)
