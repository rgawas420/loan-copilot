"""Who is calling: the logged-in user from the session cookie, and admin access (admin user or admin key)."""
import secrets

from fastapi import Cookie, Depends, Header, HTTPException

from app.core.config import get_settings
from app.services import accounts

SESSION_COOKIE = "um_session"


def optional_user(um_session: str | None = Cookie(default=None)):
    return accounts.session_user(um_session)


def current_user(user=Depends(optional_user)):
    if user is None:
        raise HTTPException(401, "Log in to continue.")
    return user


def admin_user(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only.")
    return user


def require_admin(x_admin_key: str | None = Header(default=None), user=Depends(optional_user)):
    """Staff endpoints accept a logged-in admin, or the shared ADMIN_API_KEY (scripts and the classic page)."""
    if user is not None and user["role"] == "admin":
        return
    s = get_settings()
    if s.app_env == "production" and s.admin_api_key in ("change-me", "change-me-in-production"):
        raise HTTPException(503, "Set a real ADMIN_API_KEY before using admin endpoints in production.")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, s.admin_api_key):
        raise HTTPException(401, "Invalid admin key.")
