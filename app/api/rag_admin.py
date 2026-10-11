"""Admin console: RAG quality. Answer traces, the test bench with stage-by-stage diagnosis, golden tests and
the configuration every answer is produced with."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import admin_user
from app.services import accounts, rag_debug

router = APIRouter(prefix="/api/admin/rag")


class TestIn(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    loan_type: Optional[str] = None
    expected_answer: str = Field(default="", max_length=500)
    expected_document: str = Field(default="", max_length=300)
    expected_page: Optional[int] = Field(default=None, ge=1)


class GoldenIn(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    expected_answer: str = Field(default="", max_length=500)
    expected_document: str = Field(default="", max_length=300)
    expected_page: Optional[int] = Field(default=None, ge=1)
    loan_type: Optional[str] = Field(default=None, max_length=60)
    notes: str = Field(default="", max_length=1000)
    active: bool = True
    category: str = Field(default="", max_length=60)
    reference_answer: str = Field(default="", max_length=5000)
    history: str = Field(default="", max_length=4000)        # earlier user questions, one per line
    approved_by: str = Field(default="", max_length=120)


@router.get("/config")
def rag_config(_=Depends(admin_user)):
    cfg = rag_debug.config()
    g = rag_debug.gate_current()
    return {"config": cfg, "hash": rag_debug.config_hash(cfg), "last_run_hash": rag_debug.last_config_hash(),
            "gate": {k: g.get(k) for k in ("verdict", "reasons", "run_id", "baseline_id")}}


@router.get("/traces")
def rag_traces(q: str = "", only: str = "", limit: int = 100, _=Depends(admin_user)):
    return {"traces": rag_debug.traces(min(max(limit, 1), 500), q.strip(), only)}


@router.get("/traces/{tid}")
def rag_trace(tid: str, expected_answer: str = "", expected_document: str = "", expected_page: Optional[int] = None,
              _=Depends(admin_user)):
    t = rag_debug.trace(tid)
    if t is None:
        raise HTTPException(404, "Trace not found.")
    t["diagnosis"] = rag_debug.diagnose(t["data"], expected_answer, expected_document, expected_page)
    return t


@router.post("/test")
def rag_test(body: TestIn, admin=Depends(admin_user)):
    """Run a question through the full pipeline (not saved to any chat) and compare it with the expectation."""
    from app.rag import chat
    try:
        res = chat.run("kb", body.question, loan_type=body.loan_type or None, user_id=admin["id"], origin="test",
                       require_llm=False)
    except Exception as e:
        raise HTTPException(502, f"The pipeline failed: {type(e).__name__}: {e}"[:300])
    ev = rag_debug.evaluate(res, body.expected_answer, body.expected_document, body.expected_page)
    return {"answer": res["answer"], "sources": res["sources"], "unsupported_figures": res["unsupported_figures"],
            "not_found": res["not_found"], "llm_skipped": res["llm_skipped"], "evaluation": ev,
            "trace_id": res["trace_id"], "trace": res["trace"],
            "diagnosis": rag_debug.diagnose(res["trace"], body.expected_answer, body.expected_document, body.expected_page)}


@router.get("/golden")
def golden_list(_=Depends(admin_user)):
    return {"tests": rag_debug.golden_list(), "runs": rag_debug.runs(), "state": rag_debug.state}


@router.post("/golden")
def golden_create(body: GoldenIn, admin=Depends(admin_user)):
    item = rag_debug.golden_save({**body.model_dump(), "active": int(body.active)})
    accounts.log(admin["id"], "rag_golden_saved", body.question[:200])
    return item


@router.put("/golden/{gid}")
def golden_update(gid: int, body: GoldenIn, admin=Depends(admin_user)):
    try:
        item = rag_debug.golden_save({**body.model_dump(), "active": int(body.active)}, gid)
    except LookupError as e:
        raise HTTPException(404, str(e))
    accounts.log(admin["id"], "rag_golden_saved", body.question[:200])
    return item


@router.delete("/golden/{gid}")
def golden_delete(gid: int, admin=Depends(admin_user)):
    if not rag_debug.golden_delete(gid):
        raise HTTPException(404, "Test not found.")
    accounts.log(admin["id"], "rag_golden_deleted", str(gid))
    return {"deleted": gid}


@router.post("/golden/run")
def golden_run(admin=Depends(admin_user)):
    if rag_debug.state["running"]:
        raise HTTPException(409, "A golden-test run is already in progress.")
    if not rag_debug.run_golden_async("manual"):
        raise HTTPException(400, "Add at least one active golden test first.")
    accounts.log(admin["id"], "rag_golden_run", "manual")
    return {"started": True}


class FeedbackReview(BaseModel):
    status: str = Field(pattern="^(new|confirmed|dismissed)$")
    note: str = Field(default="", max_length=2000)


@router.get("/feedback")
def feedback_list(status: str = "", rating: str = "", _=Depends(admin_user)):
    from app.services import feedback as fb
    return {"items": fb.items(status, rating), "counts": fb.counts(), "reasons": fb.REASONS}


@router.put("/feedback/{fid}")
def feedback_review(fid: int, body: FeedbackReview, admin=Depends(admin_user)):
    from app.services import feedback as fb
    try:
        item = fb.review(fid, body.status, admin.get("name") or admin["email"], body.note)
    except LookupError as e:
        raise HTTPException(404, str(e))
    accounts.log(admin["id"], "feedback_reviewed", f"{fid} {body.status}")
    return item


@router.post("/feedback/{fid}/golden")
def feedback_golden(fid: int, body: GoldenIn, admin=Depends(admin_user)):
    """Turn a confirmed mistake into a golden test (with the correct reference answer)."""
    from app.services import feedback as fb
    try:
        g = fb.to_golden(fid, {**body.model_dump(), "active": int(body.active)}, admin.get("name") or admin["email"])
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    accounts.log(admin["id"], "rag_golden_saved", f"from feedback {fid}: {g['question'][:150]}")
    return g


@router.post("/golden/runs/{rid}/baseline")
def golden_baseline(rid: int, admin=Depends(admin_user)):
    """Approve a run as the release gate's baseline."""
    try:
        out = rag_debug.set_baseline(rid)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    accounts.log(admin["id"], "rag_baseline_set", str(rid))
    return out


@router.post("/golden/runs/{rid}/resume")
def golden_resume(rid: int, admin=Depends(admin_user)):
    """Continue a run stopped by the AI provider's limit: its finished cases are kept, the rest run."""
    if rag_debug.state["running"]:
        raise HTTPException(409, "A golden-test run is already in progress.")
    try:
        if not rag_debug.run_golden_async(f"resumed run {rid}", resume=rid):
            raise HTTPException(400, "Add at least one active golden test first.")
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    accounts.log(admin["id"], "rag_golden_run", f"resume {rid}")
    return {"started": True}


@router.get("/golden/runs/{rid}")
def golden_run_detail(rid: int, _=Depends(admin_user)):
    r = rag_debug.run_detail(rid)
    if r is None:
        raise HTTPException(404, "Run not found.")
    return r
