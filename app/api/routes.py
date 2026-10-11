import json
import logging
import re
import threading
import urllib.request
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import current_user, optional_user, require_admin
from app.core.config import get_settings
from app.core.ratelimit import rate_limited
from app.rag import llm, metering, policies
from app.rag.questionnaire import LOAN_TYPES, apply_answer, next_payload, prune_case
from app.services import accounts, audit, source_policy, usage
from app.services.ingestion import ALLOWED_EXTENSIONS, POLICY_EXTENSIONS, file_hash, load_and_chunk

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


class CaseIn(BaseModel):
    case: Dict[str, Any]
    case_id: Optional[str] = None


class SaveIn(CaseIn):
    token: Optional[str] = None


class AnswerIn(BaseModel):
    case: Dict[str, Any]
    key: str
    value: Optional[str] = None
    skip: bool = False


class IntakeIn(BaseModel):
    case: Dict[str, Any] = Field(default_factory=dict)
    text: str = Field(min_length=3, max_length=2000)
    conversation_id: Optional[str] = Field(default=None, max_length=64)


class ChatIn(BaseModel):
    case: Dict[str, Any]
    result: Dict[str, Any]
    question: str = Field(min_length=2, max_length=500)
    history: List[Dict[str, str]] = Field(default_factory=list)


# ------------------------------------------------------------------ metering: quota, reservation, usage ledger
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _size(*parts) -> int:
    """Characters the user sends to the model (for the reservation estimate and the plan's context limit)."""
    return sum(len(p) if isinstance(p, str) else len(json.dumps(p, default=str)) for p in parts if p)


def _reserve(user, feature: str, chars: int, conversation_id=None, message_id=None, needs_rag=False):
    """Check rate, concurrency and token quota atomically and reserve tokens for this request (HTTP error if
    refused). Ids from the browser are only labels: a conversation id is kept only if it is the user's own."""
    if user is None or not (conversation_id and ID_RE.match(conversation_id)
                            and accounts.owns_conversation(user["id"], conversation_id)):
        conversation_id = None
    message_id = message_id if message_id and ID_RE.match(message_id) else None
    try:
        res = usage.reserve(user, feature, chars, conversation_id, message_id)
    except usage.QuotaError as e:
        raise HTTPException(e.status, e.detail())
    if needs_rag and res.plan and not res.plan.get("rag_enabled", True):
        usage.settle(res, "refused")
        raise HTTPException(403, {"code": "rag_not_in_plan", "plan": res.plan["name"],
                                  "message": f"Knowledge-base answers are not included in the {res.plan['name']} plan."})
    return res


@contextmanager
def _metered(res):
    """Run model calls under the reservation, then release it (only the recorded usage stays)."""
    status = "failed"
    try:
        with metering.metered(metering.Meter(res)):
            yield
        status = "completed"
    finally:
        usage.settle(res, status)


def _usage_event(res, request: dict) -> dict:
    return {"request": request, "usage": usage.current_usage(res.user_id) if res.user_id else None}


@router.get("/health")
def health():
    s = get_settings()
    return {"status": "ok", "vector_store": "pinecone" if s.pinecone_api_key else "in-memory",
            "groq_configured": bool(s.groq_api_key), "tavily_configured": bool(s.tavily_api_key),
            "llm_provider": llm.provider(), "llm_configured": llm.configured(),
            "structured_programs": len(policies.load_programs())}


# ------------------------------------------------------------------ questionnaire
@router.post("/questionnaire/next")
def questionnaire_next(body: CaseIn):
    case = prune_case(body.case)
    return {"case": case, **next_payload(case)}


@router.post("/questionnaire/answer")
def questionnaire_answer(body: AnswerIn):
    """Validate one answer (or skip an optional question) and return the updated case and next question."""
    case, err = apply_answer(prune_case(body.case), body.key, body.value, body.skip)
    if err:
        raise HTTPException(422, err)
    return {"case": case, **next_payload(case)}


@router.post("/intake", dependencies=[Depends(rate_limited)])
def intake(body: IntakeIn, user=Depends(optional_user)):
    """Fill the case from a free-text description; then the questionnaire asks only what is missing."""
    from app.rag.intake import extract
    res = _reserve(user, "intake", _size(body.text, body.case), body.conversation_id)
    try:
        with _metered(res):
            case, filled, rejected = extract(body.text, prune_case(body.case))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception:
        log.exception("Intake extraction failed")
        raise HTTPException(502, "Could not read the description. Answer the questions instead.")
    _activity(user, "intake", f"{len(filled)} answers filled")
    return {"case": case, "filled": filled, "rejected": rejected, **next_payload(case)}


@router.get("/pin/{pin}")
def pin_lookup(pin: str):
    """Auto-fill City / District / State from a 6-digit PIN (India Post public API)."""
    if not re.fullmatch(r"\d{6}", pin):
        raise HTTPException(400, "PIN code must be 6 digits.")
    try:
        with urllib.request.urlopen(f"https://api.postalpincode.in/pincode/{pin}", timeout=4) as r:
            data = json.loads(r.read().decode())
        po = data[0]["PostOffice"][0]
        return {"city": po["Block"] if po.get("Block") not in (None, "NA") else po["Name"],
                "district": po["District"], "state": po["State"]}
    except Exception:
        raise HTTPException(404, "PIN code lookup unavailable. Enter the details manually.")


# ------------------------------------------------------------------ saved cases
@router.post("/case/save")
def case_save(body: SaveIn):
    try:
        case_id, token = audit.save_case(prune_case(body.case), body.case_id, body.token)
    except audit.CaseAccessError as e:
        raise HTTPException(403, str(e))
    return {"case_id": case_id, "token": token}


@router.get("/case/{case_id}")
def case_load(case_id: str, x_case_token: str | None = Header(default=None)):
    c = audit.load_case(case_id, x_case_token)
    if c is None:
        raise HTTPException(404, "Saved case not found, or the access link is incomplete.")
    return {"case_id": case_id, "case": c}


# ------------------------------------------------------------------ recommendation
def _audit(case_id, case, result, user=None):
    audit.log_recommendation(case_id, case.get("loan_type"), result.get("status"),
                             result.get("evidence_source"), result.get("trace", []))
    _activity(user, "recommendation", f"{case.get('loan_type')}: {result.get('status')}")


def _activity(user, action, detail=""):
    """Activity log for the admin console: only for logged-in users, and never with case details."""
    if user:
        try:
            accounts.log(user["id"], action, detail)
        except Exception:
            log.exception("Could not write the activity log")


@router.post("/recommend", dependencies=[Depends(rate_limited)])
def recommend(body: CaseIn, user=Depends(optional_user)):
    from app.rag.workflow import run_recommendation      # heavy imports only when needed
    case = prune_case(body.case)
    res = _reserve(user, "recommend", _size(case), body.case_id)
    try:
        with _metered(res):
            result = run_recommendation(case)
    except Exception as e:
        log.exception("Recommendation failed")
        raise HTTPException(500, f"Recommendation failed: {type(e).__name__}. Check server logs.")
    _audit(body.case_id, case, result, user)
    return source_policy.result_for(user, result)


@router.post("/recommend/stream", dependencies=[Depends(rate_limited)])
def recommend_stream(body: CaseIn, user=Depends(optional_user)):
    """Server-sent events: 'step' for each agent step as it happens, then 'result' (or 'error')."""
    from app.rag.workflow import stream_recommendation
    case = prune_case(body.case)
    res = _reserve(user, "recommend", _size(case), body.case_id)

    def events():
        gen, status = metering.bind(metering.Meter(res), stream_recommendation(case)), "stopped"
        try:
            for kind, payload in gen:
                if kind == "result":
                    _audit(body.case_id, case, payload, user)
                    status = "completed"
                    yield _sse("usage", _usage_event(res, usage.settle(res, status)))
                if kind == "result":
                    payload = source_policy.result_for(user, payload)
                yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"
        except Exception as e:
            status = "failed"
            log.exception("Recommendation failed")
            yield f"event: error\ndata: {json.dumps({'message': f'Recommendation failed: {type(e).__name__}.'})}\n\n"
        finally:
            gen.close()
            usage.settle(res, status)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/chat", dependencies=[Depends(rate_limited)])
def chat(body: ChatIn, user=Depends(optional_user)):
    from app.rag.chat import answer
    res = _reserve(user, "chat", _size(body.question, body.history, body.case, body.result), needs_rag=True)
    try:
        with _metered(res):
            reply = {"answer": answer(body.question, prune_case(body.case), body.result, body.history)}
        _activity(user, "chat", "follow-up question")
        return reply
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception:
        log.exception("Chat failed")
        raise HTTPException(502, "Could not answer right now. Try again.")


class ChatStreamIn(BaseModel):
    mode: Literal["ask", "kb"]
    question: str = Field(min_length=2, max_length=2000)
    history: List[Dict[str, str]] = Field(default_factory=list)
    case: Dict[str, Any] = Field(default_factory=dict)
    result: Dict[str, Any] = Field(default_factory=dict)
    loan_type: Optional[str] = None
    attachment_ids: List[str] = Field(default_factory=list, max_length=5)
    partial: str = Field(default="", max_length=20000)        # continue an answer that was cut off
    conversation_id: Optional[str] = Field(default=None, max_length=64)   # labels for the usage ledger only
    message_id: Optional[str] = Field(default=None, max_length=64)


def _sse(kind: str, payload) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload, default=str, ensure_ascii=False)}\n\n"


def _sponsored(user, body, answer: str):
    """A sponsored card after a knowledge-base answer (see app/services/ads.py). Never after a follow-up about a
    recommendation, a continued answer, or a "not found" reply; an ad problem never affects the answer."""
    from app.rag.grounding import is_not_found
    from app.rag import smalltalk
    if body.mode != "kb" or body.partial or not answer.strip() or is_not_found(answer) or smalltalk.kind(body.question):
        return None
    try:
        from app.services import ads
        return ads.pick(user["id"], body.question, body.history)
    except Exception:
        log.exception("Could not choose an ad")
        return None


@router.post("/chat/stream", dependencies=[Depends(rate_limited)])
def chat_stream(body: ChatStreamIn, user=Depends(current_user)):
    """Server-sent events for the chat UI: 'status' as each retrieval step starts, 'token' for each piece of
    text as the model writes it, 'replace' with the whole answer if it was rewritten after failing the source
    check, 'sources' (the passages the answer cites) when it is complete, 'verification' if a figure in the
    answer could not be found in those sources, then 'done' (or 'error')."""
    from app.rag.chat import ATTACHMENT_CHARS, stream
    from app.services import attachments
    files = attachments.load(user["id"], body.attachment_ids)
    case = prune_case(body.case)
    chars = _size(body.question, body.history, body.partial, case, body.result if body.mode == "ask" else None) + \
        sum(min(len(f[1]), ATTACHMENT_CHARS) for f in files)
    res = _reserve(user, "chat", chars, body.conversation_id, body.message_id, needs_rag=True)

    def events():
        wrote, status, answer = False, "stopped", []
        from app.services import rag_debug
        rag_debug.message_link.set((body.conversation_id, body.message_id))   # copied into the bound context below
        gen = metering.bind(metering.Meter(res), stream(
            body.mode, body.question, case, body.result, body.loan_type or case.get("loan_type"), body.history, files,
            body.partial, user_id=user["id"]))
        try:
            for kind, payload in gen:
                wrote = wrote or kind == "token"
                if kind == "token":
                    answer.append(payload)
                if kind == "replace":             # the answer was rewritten after the source check
                    answer[:] = [payload]
                if kind == "sources":             # internal documents: admins only (own uploads stay visible)
                    payload = source_policy.sources_for(user, payload)
                yield _sse(kind, payload)
            status = "completed"
            ad = _sponsored(user, body, "".join(answer))
            if ad:
                yield _sse("ad", ad)
            yield _sse("usage", _usage_event(res, usage.settle(res, status)))
            yield _sse("done", {})
            _activity(user, "chat" if body.mode == "ask" else "kb_question",
                      "streamed" + (f", {len(files)} attachment(s)" if files else ""))
        except RuntimeError as e:
            status = "failed"
            yield _sse("error", {"message": str(e), "partial": wrote})
        except Exception as e:
            status = "failed"
            from app.rag.llm import is_rate_limit, rate_limit_message
            if is_rate_limit(e):
                log.warning("Chat stream stopped by the AI provider's rate limit: %s", str(e)[:300])
                yield _sse("error", {"message": rate_limit_message(e), "partial": wrote, "code": "rate_limited"})
            else:
                log.exception("Chat stream failed")
                yield _sse("error", {"message": f"Could not finish the answer ({type(e).__name__}). Try again.", "partial": wrote})
        finally:
            gen.close()                       # a stopped answer records its (estimated) usage before release
            usage.settle(res, status)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ admin: policies
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:60] or "policy"


@router.post("/ingest", dependencies=[Depends(require_admin)])
async def ingest(file: UploadFile = File(...), lender: str = Form(default=""), loan_type: str = Form(default="")):
    """Upload a structured policy (.yaml) or a policy document (PDF, Word, text, spreadsheet, JSON, HTML, image)."""
    s = get_settings()
    filename = Path(file.filename or "").name
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS | POLICY_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type. Use: {', '.join(sorted(ALLOWED_EXTENSIONS | POLICY_EXTENSIONS))}")
    if loan_type and loan_type not in LOAN_TYPES:
        raise HTTPException(400, "Unknown loan type.")
    data = await file.read()
    if len(data) > s.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File is larger than {s.max_upload_mb} MB.")

    if ext in POLICY_EXTENSIONS:          # structured rules: validate, then store under a safe name
        try:
            pf = policies.parse_policy(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(422, str(e)[:500])
        s.policy_dir.mkdir(parents=True, exist_ok=True)
        (s.policy_dir / f"{_slug(pf.lender)}.yaml").write_bytes(data)
        policies.reload()
        return {"filename": filename, "kind": "structured", "lender": pf.lender, "programs": len(pf.programs)}

    sha = file_hash(data)
    if audit.get_policy_doc(sha):
        raise HTTPException(409, "This exact file is already in the knowledge base.")
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    stored = s.upload_dir / f"{sha}{ext}"            # never trust the uploaded name for paths
    stored.write_bytes(data)
    try:
        chunks = load_and_chunk(stored, source_name=filename, lender=lender.strip() or None, loan_type=loan_type or None)
    except Exception as e:
        stored.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not read the file: {e}")
    from app.rag.vectorstore import add_documents, chunk_ids
    n = add_documents(chunks, ids=chunk_ids(sha, len(chunks)))
    audit.add_policy_doc(sha, filename, ext, lender.strip() or None, loan_type or None, n)
    return {"filename": filename, "kind": "document", "id": sha, "chunks_indexed": n}


@router.get("/policies", dependencies=[Depends(require_admin)])
def policy_list():
    stale_days, today = get_settings().policy_stale_days, date.today()
    programs = [{"id": p.id, "lender": p.lender, "program": p.name, "loan_type": p.loan_type, "sample": p.sample,
                 "policy_last_verified": str(p.policy_last_verified) if p.policy_last_verified else None,
                 "stale": p.policy_last_verified is None or (today - p.policy_last_verified).days > stale_days}
                for p in policies.load_programs()]
    return {"structured_programs": programs, "documents": audit.list_policy_docs()}


@router.delete("/policies/doc/{sha}", dependencies=[Depends(require_admin)])
def policy_delete(sha: str):
    row = audit.get_policy_doc(sha) if re.fullmatch(r"[0-9a-f]{64}", sha) else None
    if row is None:
        raise HTTPException(404, "Document not found.")
    from app.rag.vectorstore import chunk_ids, delete_documents
    delete_documents(chunk_ids(sha, row["chunks"]))
    (get_settings().upload_dir / f"{sha}{row['ext']}").unlink(missing_ok=True)
    audit.delete_policy_doc(sha)
    return {"deleted": row["filename"]}


# ------------------------------------------------------------------ Urban Money knowledge base
class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    loan_type: Optional[str] = None
    history: List[Dict[str, str]] = Field(default_factory=list)


@router.post("/kb/ask", dependencies=[Depends(rate_limited)])
def kb_ask(body: AskIn, user=Depends(optional_user)):
    """Answer a question from the Urban Money knowledge base, with the source files used."""
    from app.rag.chat import ask_kb
    res = _reserve(user, "chat", _size(body.question, body.history), needs_rag=True)
    try:
        with _metered(res):
            reply = ask_kb(body.question, body.loan_type, body.history)
        reply["sources"] = source_policy.sources_for(user, reply.get("sources"))
        _activity(user, "kb_question", body.loan_type or "")
        return reply
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception:
        log.exception("KB question failed")
        raise HTTPException(502, "Could not answer right now. Try again.")


@router.post("/kb/sync", dependencies=[Depends(require_admin)])
def kb_sync(full: bool = False):
    """Re-scan the Urban Money folder in the background: index new / changed files, remove deleted ones."""
    from app.services import knowledge_base
    if knowledge_base.status["running"]:
        raise HTTPException(409, "A sync is already running.")

    def run():
        try:
            knowledge_base.sync(full=full)
        except Exception:
            log.exception("Knowledge-base sync failed")
    threading.Thread(target=run, daemon=True).start()
    return {"started": True, "full": full}


@router.get("/kb/status", dependencies=[Depends(require_admin)])
def kb_status():
    from app.services import knowledge_base
    root = knowledge_base.kb_root()
    return {"folder": str(root) if root else None, "sync": knowledge_base.status, "files": knowledge_base.files()}


@router.get("/audit", dependencies=[Depends(require_admin)])
def audit_list():
    return audit.recent()
