"""Admin console: manage the knowledge-base documents (upload and review, process, preview, archive, delete,
versions, rollback and merging duplicates)."""
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import admin_user
from app.core.config import get_settings
from app.services import accounts, kb_admin, kb_audit, kb_staging, kb_verify, kb_versions

router = APIRouter(prefix="/api/admin/kb")
MAX_FILES_PER_UPLOAD = 50


class PathsIn(BaseModel):
    paths: List[str] = Field(min_length=1, max_length=1000)


class SyncIn(BaseModel):
    full: bool = False


class SearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    loan_type: Optional[str] = None


class DocumentIn(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    title: Optional[str] = Field(default=None, max_length=120)
    bank: Optional[str] = Field(default=None, max_length=120)
    product: Optional[str] = Field(default=None, max_length=120)
    category: Optional[str] = Field(default=None, max_length=120)
    source_type: Optional[str] = Field(default=None, max_length=40)     # "" or null with priority null: defaults
    priority: Optional[int] = Field(default=None, ge=0, le=100)


class ActivateIn(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    version: int = Field(ge=1)


class DecideIn(BaseModel):
    action: str = Field(pattern="^(version|separate|reject)$")
    doc_id: Optional[str] = Field(default=None, max_length=40)
    force: bool = False                               # activate although knowledge tests fail
    title: Optional[str] = Field(default=None, max_length=120)
    category: Optional[str] = Field(default=None, max_length=120)
    label: Optional[str] = Field(default=None, max_length=120)
    source_type: Optional[str] = Field(default=None, max_length=40)
    effective_from: Optional[str] = Field(default=None, max_length=10)
    effective_until: Optional[str] = Field(default=None, max_length=10)
    folder: Optional[str] = Field(default=None, max_length=300)


class MergeIn(BaseModel):
    keep: str = Field(min_length=1, max_length=1000)
    others: List[str] = Field(min_length=1, max_length=50)


def _each(fn, paths):
    """Apply fn to each path; a failure on one file does not stop the others."""
    done, errors = [], []
    for p in paths:
        try:
            done.append(fn(p))
        except kb_admin.KBError as e:
            errors.append({"file": p, "error": str(e)})
    return done, errors


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (kb_admin.KBError, kb_staging.StagingError, kb_verify.VerifyError) as e:
        raise HTTPException(e.code, getattr(e, "detail", None) or str(e))


@router.get("")
def kb_overview(_=Depends(admin_user)):
    return _call(kb_admin.overview)


@router.post("/upload")
async def kb_upload(files: List[UploadFile] = File(...), folder: str = Form(default=""),
                    target: str = Form(default=""), admin=Depends(admin_user)):
    """Stage files for checking. A file that matches nothing is then added as a new document; one that may be a
    new version of an existing document (or `target`, chosen by the admin), a duplicate or an image waits for a
    decision (see /pending/{id}/decide)."""
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(400, f"Upload at most {MAX_FILES_PER_UPLOAD} files at a time.")
    s = get_settings()
    limit = min(s.kb_upload_max_mb, s.kb_max_file_mb) * 1024 * 1024
    staged, errors = [], []
    for f in files:
        data = await f.read(limit + 1)
        try:
            staged.append(kb_staging.stage(f.filename or "", data, folder, target, by=admin["email"]))
        except kb_staging.StagingError as e:
            errors.append({"file": f.filename, "error": str(e)})
    if staged:
        kb_admin.enqueue("stage", [x["id"] for x in staged], by=admin["email"])
        accounts.log(admin["id"], "kb_upload", f"{len(staged)} file(s) staged: {', '.join(x['name'] for x in staged)}"[:300])
    return {"staged": staged, "errors": errors}


@router.post("/pending/{pid}/decide")
def kb_decide(pid: str, body: DecideIn, admin=Depends(admin_user)):
    """The admin's decision on a staged upload: activate as a new version of a document, add as a separate
    document, or reject."""
    fields = body.model_dump(exclude={"action", "doc_id", "force"}, exclude_unset=True)
    res = _call(kb_staging.decide, pid, body.action, body.doc_id, fields, by=admin["email"], force=body.force)
    what = {"version": "kb_version_activated", "separate": "kb_document_added", "reject": "kb_upload_rejected"}[body.action]
    accounts.log(admin["id"], what, json.dumps({"upload": pid, **{k: v for k, v in res.items() if k != "doc_id"}})[:300])
    return res


@router.get("/pending/{pid}/review")
def kb_pending_review(pid: str, doc_id: str, _=Depends(admin_user)):
    """Compare the upload with the document's current version, and run the document's knowledge tests on it."""
    return _call(kb_staging.review, pid, doc_id)


@router.get("/versions/compare")
def kb_versions_compare(path: str, version: int, _=Depends(admin_user)):
    """Compare an earlier version of a document with its ACTIVE version."""
    from app.services import kb_review
    old, v = kb_review.version_parts(path, version)
    new, cur = kb_review.version_parts(path)
    if v is None or cur is None:
        raise HTTPException(404, "Version not found.")
    if old is None or new is None:
        raise HTTPException(409, "The extracted text of one of these versions is no longer cached.")
    return {"old": {"version": v["version"], "label": v["label"]}, "new": {"version": cur["version"], "label": cur["label"]},
            "comparison": kb_review.compare(old, new)}


@router.get("/audit")
def kb_audit_log(limit: int = 100, offset: int = 0, q: str = "", action: str = "", doc_id: str = "",
                 _=Depends(admin_user)):
    """Every change to the knowledge base: who, what, which document and version, when."""
    return kb_audit.listing(limit, offset, q[:100], action[:20], doc_id[:40])


@router.get("/banker-quality")
def banker_quality(limit: int = 20, _=Depends(admin_user)):
    """The banker directory's last build: totals, and examples of every data problem found in the RM lists
    (duplicate files, unreadable contact files, missing / invalid / conflicting numbers, numbers inside names)."""
    from app.services import banker_directory
    return banker_directory.quality_report(max(1, min(limit, 200)))


class AliasIn(BaseModel):
    kind: str = Field(pattern="^(bank|product|city)$")
    alias: str = Field(min_length=2, max_length=80)
    canonical: str = Field(default="", max_length=80)


@router.get("/aliases")
def alias_list(_=Depends(admin_user)):
    """Admin-added names for banks, products and cities (on top of the built-in ones)."""
    from app.services import aliases
    return {"aliases": aliases.listing()}


@router.post("/aliases")
def alias_add(body: AliasIn, user=Depends(admin_user)):
    from app.services import aliases
    try:
        return aliases.add(body.kind, body.alias, body.canonical, user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/aliases/remove")
def alias_remove(body: AliasIn, user=Depends(admin_user)):
    from app.services import aliases
    if not aliases.remove(body.kind, body.alias, user["email"]):
        raise HTTPException(404, "No such alias.")
    return {"removed": True}


class FactIdsIn(BaseModel):
    ids: List[int] = Field(min_length=1, max_length=5000)


@router.get("/facts")
def facts_list(status: str = "", _=Depends(admin_user)):
    """Structured policy facts (ROI, payout, fees, LTV ...): pending ones wait for approval."""
    from app.services import policy_facts
    return {"facts": policy_facts.listing(status if status in ("pending", "approved") else ""),
            "kinds": policy_facts.FACTS}


@router.post("/facts/import")
async def facts_import(file: UploadFile = File(...), user=Depends(admin_user)):
    """CSV: lender, product, fact, value, unit, customer_category, effective_from, effective_until, source, page.
    Rows are added as pending; nothing is used until approved."""
    from app.services import policy_facts
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "The file is larger than 5 MB.")
    try:
        r = policy_facts.import_csv(data.decode("utf-8-sig", errors="replace"), user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    kb_audit.record("facts", user["email"], detail=f"imported {r['added']} fact(s) from {file.filename} (pending)")
    return r


class PathIn(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


@router.post("/facts/extract-payouts")
def facts_extract_payouts(body: PathIn, user=Depends(admin_user)):
    """Read a slab book / payout grid in the knowledge base into pending payout facts."""
    from app.services import policy_facts
    try:
        target = kb_admin.resolve(body.path)
        if not target.is_file():
            raise kb_admin.KBError("No such file in the knowledge base.")
    except kb_admin.KBError as e:
        raise HTTPException(400, str(e))
    r = policy_facts.extract_payout_grid(target, body.path, user["email"])
    kb_audit.record("facts", user["email"], body.path, detail=f"extracted {r['added']} payout fact(s) (pending)")
    return r


@router.post("/facts/approve")
def facts_approve(body: FactIdsIn, user=Depends(admin_user)):
    from app.services import policy_facts
    n = policy_facts.approve(body.ids, user["email"])
    kb_audit.record("facts", user["email"], detail=f"approved {n} fact(s)")
    return {"approved": n}


@router.post("/facts/delete")
def facts_delete(body: FactIdsIn, user=Depends(admin_user)):
    from app.services import policy_facts
    n = policy_facts.delete(body.ids)
    kb_audit.record("facts", user["email"], detail=f"deleted {n} fact(s)")
    return {"deleted": n}


class HistoryIn(BaseModel):
    query: str = Field(min_length=2, max_length=300)


@router.post("/history-search")
def kb_history_search(body: HistoryIn, _=Depends(admin_user)):
    """Admins only: search the text of ARCHIVED and DISABLED versions (never used for normal answers)."""
    from app.services import kb_review
    return {"results": kb_review.history_search(body.query)}


class VerifyIn(BaseModel):
    path: Optional[str] = Field(default=None, max_length=1000)          # a document in the knowledge base...
    pending_id: Optional[str] = Field(default=None, max_length=40)      # ...or an upload waiting for review
    text: str = Field(default="", max_length=2_000_000)


def _verify_target(path: Optional[str], pending_id: Optional[str]):
    """(content sha, display name) of the file whose text is verified."""
    if pending_id:
        row = kb_staging._get(pending_id)
        if row is None:
            raise HTTPException(404, "Upload not found.")
        if row["status"] not in kb_staging.OPEN:
            raise HTTPException(409, "This upload has already been decided.")
        return row["sha"], row["name"]
    if path:
        from app.services import knowledge_base as kb
        return kb._hash_file(_call(kb_admin.file_path, path)), path
    raise HTTPException(400, "Give the document or the upload.")


def _after_verify(path: Optional[str], pending_id: Optional[str], by: str):
    if pending_id:
        kb_staging.recheck(pending_id)                      # its checks and status change with the new text
    else:
        kb_admin.enqueue("files", [path], full=True, by=by)  # re-index the same content from the verified text


@router.get("/verify")
def kb_verify_get(path: Optional[str] = None, pending_id: Optional[str] = None, _=Depends(admin_user)):
    """The OCR text of an image-based document (or its verified text), with the figures to check."""
    sha, name = _verify_target(path, pending_id)
    return {**kb_verify.editor(sha, name), "path": path, "pending_id": pending_id}


@router.post("/verify")
def kb_verify_save(body: VerifyIn, admin=Depends(admin_user)):
    """Save the admin's checked / corrected text; it replaces the OCR output for this file from now on."""
    sha, name = _verify_target(body.path, body.pending_id)
    res = _call(kb_verify.save, sha, body.text, admin["email"])
    _after_verify(body.path, body.pending_id, admin["email"])
    ch = res["changes"]
    accounts.log(admin["id"], "kb_text_verified", f"{name}: {len(ch['removed'])} figure(s) corrected, "
                                                   f"{len(ch['added'])} added"[:300])
    kb_audit.record("verify", admin["email"], name if body.path else f"{name} (upload)",
                    detail=(f"corrected {', '.join(ch['removed'])} → {', '.join(ch['added'])}" if ch["removed"] or ch["added"]
                            else "OCR text confirmed, no figures changed"))
    return {**res, "reindexing": True}


@router.post("/verify/remove")
def kb_verify_remove(body: VerifyIn, admin=Depends(admin_user)):
    """Go back to the OCR output for this file."""
    sha, name = _verify_target(body.path, body.pending_id)
    if not kb_verify.remove(sha):
        raise HTTPException(404, "This file has no verified text.")
    _after_verify(body.path, body.pending_id, admin["email"])
    accounts.log(admin["id"], "kb_text_unverified", name[:300])
    kb_audit.record("unverify", admin["email"], name, detail="back to the OCR output")
    return {"removed": True, "reindexing": True}


@router.get("/pending/{pid}/file")
def kb_pending_file(pid: str, _=Depends(admin_user)):
    from pathlib import Path as _P
    row = kb_staging._get(pid)
    if row is None or not _P(row["stored"]).is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(row["stored"], filename=row["name"])


@router.get("/pending/{pid}/preview")
def kb_pending_preview(pid: str, _=Depends(admin_user)):
    return _call(kb_staging.preview, pid)


@router.get("/duplicates")
def kb_duplicates(_=Depends(admin_user)):
    """Documents already in the knowledge base that look like versions of each other."""
    return {"groups": kb_staging.find_groups()}


@router.post("/merge")
def kb_merge(body: MergeIn, admin=Depends(admin_user)):
    """Keep one document; the others become its earlier (archived) versions and leave the folder."""
    res = _call(kb_staging.merge, body.keep, body.others, by=admin["email"])
    accounts.log(admin["id"], "kb_merged", f"kept {body.keep}; archived {', '.join(body.others)}"[:300])
    return res


@router.post("/process")
def kb_process(body: PathsIn, admin=Depends(admin_user)):
    """Re-extract and re-index these files even if they did not change."""
    for p in body.paths:
        _call(kb_admin.file_path, p)
    kb_admin.enqueue("files", body.paths, full=True, by=admin["email"])
    accounts.log(admin["id"], "kb_reprocess", f"{len(body.paths)} file(s)")
    return {"queued": len(body.paths)}


@router.post("/sync")
def kb_sync(body: SyncIn, admin=Depends(admin_user)):
    """Process everything new or changed in the folder (full=true: re-index every file)."""
    kb_admin.enqueue("sync", full=body.full, by=admin["email"])
    accounts.log(admin["id"], "kb_sync", "full rebuild" if body.full else "new and changed files")
    return {"queued": True, "full": body.full}


@router.post("/delete")
def kb_delete(body: PathsIn, admin=Depends(admin_user)):
    """Delete files from the folder and remove them from the index."""
    done, errors = _each(kb_admin.delete_file, body.paths)
    if done:
        kb_admin.enqueue("files", done, by=admin["email"])
        accounts.log(admin["id"], "kb_delete", ", ".join(done)[:300])
        for p in done:
            kb_audit.record("delete", admin["email"], p, detail="file deleted from the folder")
    return {"deleted": done, "errors": errors}


@router.post("/archive")
def kb_archive(body: PathsIn, admin=Depends(admin_user)):
    """Take files out of the knowledge base without deleting them (they can be restored)."""
    done, errors = _each(kb_admin.archive_file, body.paths)
    if done:
        kb_admin.enqueue("files", done, by=admin["email"])
        accounts.log(admin["id"], "kb_archive", ", ".join(done)[:300])
        for p in done:
            kb_audit.record("archive", admin["email"], p, detail="moved to the archive folder")
    return {"archived": done, "errors": errors}


@router.post("/restore")
def kb_restore(body: PathsIn, admin=Depends(admin_user)):
    done, errors = _each(kb_admin.restore_file, body.paths)
    if done:
        kb_admin.enqueue("files", done, by=admin["email"])
        accounts.log(admin["id"], "kb_restore", ", ".join(done)[:300])
        for p in done:
            kb_audit.record("restore", admin["email"], p, detail="restored from the archive folder")
    return {"restored": done, "errors": errors}


@router.post("/archived/delete")
def kb_delete_archived(body: PathsIn, admin=Depends(admin_user)):
    done, errors = _each(kb_admin.delete_archived, body.paths)
    if done:
        accounts.log(admin["id"], "kb_delete", "archived: " + ", ".join(done)[:280])
        for p in done:
            kb_audit.record("delete", admin["email"], p, detail="archived file deleted permanently")
    return {"deleted": done, "errors": errors}


@router.get("/preview")
def kb_preview(path: str, _=Depends(admin_user)):
    return _call(kb_admin.preview, path)


@router.get("/download")
def kb_download(path: str, archived: bool = False, _=Depends(admin_user)):
    p = _call(kb_admin.file_path, path, archived)
    return FileResponse(p, filename=p.name)


@router.post("/search")
def kb_test_search(body: SearchIn, _=Depends(admin_user)):
    """Run a query through the same retrieval pipeline the copilot uses (no LLM), with every score."""
    try:
        return kb_admin.test_search(body.query, body.loan_type)
    except Exception as e:
        raise HTTPException(502, f"Search failed: {type(e).__name__}")


@router.get("/document")
def kb_document(path: str, _=Depends(admin_user)):
    """A document's metadata (title, bank, product, category) and its versions."""
    return _call(kb_admin.document, path)


@router.patch("/document")
def kb_document_update(body: DocumentIn, admin=Depends(admin_user)):
    """Correct a document's metadata (its chunks are re-indexed so search and filters use the new values), or its
    source type and priority (used at once: no re-indexing)."""
    changes = body.model_dump(exclude={"path", "source_type", "priority"}, exclude_none=True)
    doc, reindex = None, bool(changes)
    if changes:
        doc = _call(kb_admin.update_document, body.path, changes)
        kb_admin.enqueue("files", [body.path], full=True, by=admin["email"])
        kb_audit.record("metadata", admin["email"], body.path, detail=", ".join(f"{k}: {v}" for k, v in changes.items()))
        accounts.log(admin["id"], "kb_metadata", f"{body.path}: " + ", ".join(f"{k}={v}" for k, v in changes.items())[:250])
    if {"source_type", "priority"} & body.model_fields_set:
        try:
            doc = kb_versions.set_authority(body.path, body.source_type or None, body.priority)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        a = kb_versions.authority_of(doc)
        kb_audit.record("authority", admin["email"], body.path,
                        detail=f"{a['source_label']}, priority {a['base']}" + (" (custom)" if a["custom"] else " (default)"))
    if doc is None:
        raise HTTPException(400, "Nothing to change.")
    return {"document": doc, "reindexing": reindex}


@router.post("/versions/activate")
def kb_activate_version(body: ActivateIn, admin=Depends(admin_user)):
    """Make an earlier (archived) version the active one again; the current one is archived and kept."""
    v = _call(kb_admin.activate_version, body.path, body.version, admin["email"])
    kb_admin.enqueue("files", list(dict.fromkeys([v["previous_path"], v["path"]])), full=True, by=admin["email"])
    accounts.log(admin["id"], "kb_rollback", f"{body.path}: v{body.version} active again ({v['path']})"[:300])
    kb_audit.record("rollback", admin["email"], v["path"], version=body.version,
                    detail=f"v{body.version}" + (f" ({v['label']})" if v.get("label") else "") + f" active again; {body.path} archived")
    return {"activated": v["version"], "path": v["path"], "reindexing": True}


@router.get("/versions/preview")
def kb_version_preview(path: str, version: int, _=Depends(admin_user)):
    return _call(kb_admin.version_preview, path, version)
