"""Accounts, private conversations and the admin console API."""
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Cookie, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from app.api.deps import SESSION_COOKIE, admin_user, current_user
from app.core.config import get_settings
from app.core.ratelimit import auth_rate_limited
from app.services import accounts, attachments, source_policy, usage

router = APIRouter(prefix="/api")


class RegisterIn(BaseModel):
    email: str = Field(max_length=200)
    name: str = Field(max_length=80)
    password: str = Field(max_length=200)


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


class GoogleIn(BaseModel):
    credential: str = Field(min_length=20, max_length=4096)     # the ID token from Google's sign-in button


class PasswordIn(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


class ConversationIn(BaseModel):
    title: str = Field(default="New chat", max_length=200)
    data: Dict[str, Any] = Field(default_factory=dict)


class ConversationPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    data: Optional[Dict[str, Any]] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None


class FeedbackIn(BaseModel):
    conversation_id: str = Field(max_length=64)
    message_id: str = Field(max_length=64)
    rating: Optional[Literal["up", "down"]] = None
    reason: Optional[str] = Field(default=None, max_length=40)       # for a dislike: see app/services/feedback.py
    comment: str = Field(default="", max_length=1000)


class UserPatch(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, max_length=200)


def _set_session(response: Response, user_id: int):
    s = get_settings()
    response.set_cookie(SESSION_COOKIE, accounts.create_session(user_id), max_age=s.session_days * 86400,
                        httponly=True, samesite="lax", secure=s.app_env == "production", path="/")


# ------------------------------------------------------------------ auth
@router.post("/auth/register", dependencies=[Depends(auth_rate_limited)])
def register(body: RegisterIn, response: Response):
    if not get_settings().allow_registration:
        raise HTTPException(403, "Sign-up is closed. Ask an administrator for an account.")
    try:
        user = accounts.register(body.email, body.name, body.password)
    except accounts.AccountError as e:
        raise HTTPException(422, str(e))
    _set_session(response, user["id"])
    accounts.log(user["id"], "register", f"role={user['role']}")
    return {"user": user}


@router.post("/auth/login", dependencies=[Depends(auth_rate_limited)])
def login(body: LoginIn, response: Response):
    try:
        user = accounts.authenticate(body.email, body.password)
    except accounts.AccountError as e:
        raise HTTPException(401, str(e))
    _set_session(response, user["id"])
    accounts.log(user["id"], "login")
    return {"user": user}


@router.post("/auth/google", dependencies=[Depends(auth_rate_limited)])
def google_login(body: GoogleIn, response: Response):
    """Sign in (or, when sign-up is open, register) with a Google account: the same users, sessions and roles as
    an email / password login (app/services/google_auth.py)."""
    from app.services import google_auth
    if not google_auth.enabled():
        raise HTTPException(404, "Google sign-in is not set up.")
    try:
        g = google_auth.verify(body.credential)
        user, created = accounts.google_login(g.sub, g.email, g.name, get_settings().allow_registration)
    except google_auth.GoogleAuthError as e:
        raise HTTPException(401, str(e))
    except accounts.AccountError as e:
        raise HTTPException(403, str(e))
    _set_session(response, user["id"])
    accounts.log(user["id"], "register" if created else "login", "google" + (f", role={user['role']}" if created else ""))
    return {"user": user, "created": created}


@router.post("/auth/logout")
def logout(response: Response, um_session: str | None = Cookie(default=None)):
    user = accounts.session_user(um_session)
    accounts.end_session(um_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    if user:
        accounts.log(user["id"], "logout")
    return {"ok": True}


@router.get("/auth/me")
def me(user=Depends(current_user)):
    return {"user": user, "allow_registration": get_settings().allow_registration}


@router.get("/auth/config")
def auth_config():
    s = get_settings()
    return {"allow_registration": s.allow_registration, "google_client_id": s.google_client_id or None}


@router.post("/auth/password")
def change_password(body: PasswordIn, user=Depends(current_user)):
    try:
        accounts.change_password(user["id"], body.current, body.new)
    except accounts.AccountError as e:
        raise HTTPException(422, str(e))
    accounts.log(user["id"], "password_changed")
    return {"ok": True}


# ------------------------------------------------------------------ conversations (owner only)
@router.get("/conversations")
def conversation_list(q: str = "", archived: bool = False, user=Depends(current_user)):
    return {"conversations": accounts.list_conversations(user["id"], q[:200], archived)}


@router.post("/conversations")
def conversation_create(body: ConversationIn, user=Depends(current_user)):
    try:
        conv = accounts.create_conversation(user["id"], body.title, source_policy.conversation_for(user, body.data))
    except accounts.AccountError as e:
        raise HTTPException(413, str(e))
    accounts.log(user["id"], "conversation_created", conv["id"])
    return conv


@router.get("/conversations/{cid}")
def conversation_get(cid: str, user=Depends(current_user)):
    conv = accounts.get_conversation(user["id"], cid)
    if conv is None:
        raise HTTPException(404, "Conversation not found.")
    return {**conv, "data": source_policy.conversation_for(user, conv["data"])}   # also cleans chats saved earlier


@router.patch("/conversations/{cid}")
def conversation_update(cid: str, body: ConversationPatch, user=Depends(current_user)):
    try:
        conv = accounts.update_conversation(user["id"], cid, body.title, source_policy.conversation_for(user, body.data),
                                            body.pinned, body.archived)
    except accounts.AccountError as e:
        raise HTTPException(413, str(e))
    if conv is None:
        raise HTTPException(404, "Conversation not found.")
    if body.archived is not None:
        accounts.log(user["id"], "conversation_archived" if body.archived else "conversation_unarchived", cid)
    return conv


@router.delete("/conversations/{cid}")
def conversation_delete(cid: str, user=Depends(current_user)):
    if not accounts.delete_conversation(user["id"], cid):
        raise HTTPException(404, "Conversation not found.")
    accounts.log(user["id"], "conversation_deleted", cid)
    return {"deleted": cid}


@router.post("/feedback")
def feedback(body: FeedbackIn, user=Depends(current_user)):
    """Like / dislike (with a reason) on an AI answer. The rating shown is saved in the chat; this records it with
    the answer and its trace for the admins' review queue, and in the activity log."""
    from app.services import feedback as fb
    conv = accounts.get_conversation(user["id"], body.conversation_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found.")
    try:
        fb.save(user["id"], conv, body.message_id, body.rating, body.reason, body.comment)
    except ValueError as e:
        raise HTTPException(400, str(e))
    accounts.log(user["id"], "feedback", f"{body.rating or 'cleared'}{' ' + body.reason if body.reason else ''} "
                                         f"{body.conversation_id}/{body.message_id}")
    return {"ok": True}


# ------------------------------------------------------------------ chat attachments (owner only)
@router.post("/attachments")
async def attachment_upload(file: UploadFile = File(...), user=Depends(current_user)):
    """Read a file attached to a chat message; returns an id to send with the question."""
    from fastapi.concurrency import run_in_threadpool
    used, plan = await run_in_threadpool(usage.attachments_this_period, user["id"])
    if plan["max_files_per_month"] and used >= plan["max_files_per_month"]:
        raise HTTPException(402, {"code": "file_limit", "plan": plan["name"],
                                  "message": f"The {plan['name']} plan allows {plan['max_files_per_month']} attached files "
                                             "per billing period, and you have used them all."})
    max_mb = min(get_settings().attachment_max_mb, plan["max_file_mb"] or get_settings().attachment_max_mb)
    data = await file.read(max_mb * 1024 * 1024 + 1)
    if len(data) > max_mb * 1024 * 1024:
        raise HTTPException(413, f"{file.filename}: larger than {max_mb} MB, the limit on the {plan['name']} plan.")
    try:
        info = await run_in_threadpool(attachments.save, user["id"], file.filename or "", data)
    except attachments.AttachmentError as e:
        raise HTTPException(e.code, str(e))
    accounts.log(user["id"], "attachment", info["name"])
    return info


@router.get("/attachments/{aid}/file")
def attachment_file(aid: str, user=Depends(current_user)):
    """The attached image itself, for the chat thumbnail (owner only)."""
    from fastapi.responses import FileResponse
    found = attachments.image_file(user["id"], aid)
    if found is None:
        raise HTTPException(404, "Image not found.")
    path, media = found
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=86400",
                                                         "X-Content-Type-Options": "nosniff"})


@router.delete("/attachments/{aid}")
def attachment_delete(aid: str, user=Depends(current_user)):
    if not attachments.delete(user["id"], aid):
        raise HTTPException(404, "Attachment not found.")
    return {"deleted": aid}


# ------------------------------------------------------------------ admin console
@router.get("/admin/stats")
def admin_stats(_=Depends(admin_user)):
    return accounts.stats()


@router.get("/admin/users")
def admin_users(_=Depends(admin_user)):
    return {"users": accounts.list_users()}


@router.post("/admin/users")
def admin_create_user(body: RegisterIn, admin=Depends(admin_user)):
    try:
        user = accounts.register(body.email, body.name, body.password)
    except accounts.AccountError as e:
        raise HTTPException(422, str(e))
    accounts.log(admin["id"], "admin_created_user", user["email"])
    return {"user": user}


@router.patch("/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserPatch, admin=Depends(admin_user)):
    try:
        user = accounts.update_user(user_id, admin["id"], body.role, body.active, body.password)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except accounts.AccountError as e:
        raise HTTPException(422, str(e))
    changes = [f"role={body.role}" if body.role else "", f"active={body.active}" if body.active is not None else "",
               "password reset" if body.password else ""]
    accounts.log(admin["id"], "admin_updated_user", f"{user['email']}: {', '.join(c for c in changes if c)}")
    return {"user": user}


@router.delete("/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin=Depends(admin_user)):
    try:
        user = accounts.delete_user(user_id, admin["id"])
    except LookupError as e:
        raise HTTPException(404, str(e))
    except accounts.AccountError as e:
        raise HTTPException(422, str(e))
    accounts.log(admin["id"], "admin_deleted_user", user["email"])
    return {"deleted": user["email"]}


@router.get("/admin/activity")
def admin_activity(limit: int = 100, user_id: Optional[int] = None, _=Depends(admin_user)):
    return {"activity": accounts.recent_activity(max(1, min(limit, 500)), user_id)}
