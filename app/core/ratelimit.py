"""Small in-process sliding-window rate limit for the LLM-backed endpoints.

Logged-in users are limited per account: partners in one office share a public IP, so a per-IP limit would let
one busy branch lock out its colleagues. Requests without a valid session are limited per client IP. The user
comes from the validated session (the same lookup the endpoint uses), so a made-up cookie gives no new bucket.
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from app.api.deps import optional_user
from app.core.config import get_settings

_hits: dict = defaultdict(deque)
_lock = threading.Lock()
_SWEEP_AT = 5000                      # drop idle buckets once there are this many


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limited(request: Request, user=Depends(optional_user)):
    key = f"user:{user['id']}" if user else "ip:" + _ip(request)
    _hit(key, get_settings().rate_limit_per_min)


def auth_rate_limited(request: Request):
    """Separate, stricter bucket for login and register (slows password guessing)."""
    _hit("auth:" + _ip(request), get_settings().auth_rate_limit_per_min)


def _hit(key: str, limit: int):
    now = time.monotonic()
    with _lock:
        if len(_hits) > _SWEEP_AT:
            for k in [k for k, q in _hits.items() if not q or now - q[-1] > 60]:
                del _hits[k]
        q = _hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "Too many requests. Wait a minute and try again.")
        q.append(now)
