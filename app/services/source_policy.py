"""Who may see where an answer came from.

Knowledge-base sources (file names, sheets, pages and the passages themselves: RM lists, payout sheets, code
books) are internal documents, so only admins see them. Everyone else gets the answer without its sources.
A user always sees the files they attached themselves. Applied on the server, to everything that leaves it:
streamed answers, API replies, recommendation results and saved chats (old chats are cleaned when read).
"""
import copy
from typing import Optional


def is_admin(user: Optional[dict]) -> bool:
    return bool(user) and user.get("role") == "admin"


def sources_for(user: Optional[dict], sources):
    """The answer's sources this user may see: all for admins; otherwise their own attachments and public web pages."""
    if is_admin(user) or not sources:
        return sources
    return [s for s in sources if isinstance(s, dict) and (s.get("attachment") or s.get("web"))]


def result_for(user: Optional[dict], result: dict) -> dict:
    """A recommendation result without its document citations (for non-admins)."""
    if is_admin(user) or not isinstance(result, dict) or "citations" not in result:
        return result
    return {k: v for k, v in result.items() if k != "citations"}


def conversation_for(user: Optional[dict], data):
    """A saved chat with document sources removed from every message (for non-admins)."""
    if is_admin(user) or not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return data
    out = copy.copy(data)
    msgs = []
    for m in data["messages"]:
        if isinstance(m, dict) and (m.get("sources") or (isinstance(m.get("result"), dict) and "citations" in m["result"])):
            m = dict(m)
            if m.get("sources"):
                m["sources"] = sources_for(user, m["sources"])
            if isinstance(m.get("result"), dict):
                m["result"] = result_for(user, m["result"])
        msgs.append(m)
    out["messages"] = msgs
    if isinstance(out.get("result"), dict):
        out["result"] = result_for(user, out["result"])
    return out
