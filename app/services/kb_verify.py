"""Verified text for image-based documents (images, scanned PDF pages read by OCR).

OCR can misread rates, amounts, PIN codes and codes. An admin can check the OCR text against the image, correct
it (or type / paste it from a verified source) and save it. The verified text is stored per file content (the
SHA-256 of the bytes), so it applies wherever that content is read:

    image ─> OCR text ─> admin corrects ─> verified text ─> chunks, embeddings, keyword and row index, previews

knowledge_base._extract_cached() returns the verified text instead of the OCR output, so a sync re-indexes the
document from it; the original image stays the document's file (download, rollback). The same works for an
upload that is still waiting for review. Removing the verified text goes back to the OCR output.

Several pages are edited as one text with "=== Page 3 ===" lines between them, so page numbers are kept.
"""
import json
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.services.ingestion import IMAGE_EXTENSIONS, Part

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_verified(sha TEXT PRIMARY KEY, sections TEXT, by TEXT, at TEXT, changes TEXT);
"""
MAX_CHARS = 2_000_000
_HEAD = re.compile(r"^=== (.{1,80}?) ===[ \t]*$", re.M)
TEXT, OCR, VERIFIED = "text", "ocr", "verified"


class VerifyError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _db():
    from app.services.knowledge_base import _db
    return _db()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ text <-> sections
def _body(p: Part) -> str:
    return p.text or "\n".join(x for x in [p.context, *p.rows] if x)


def to_text(parts: List[Part]) -> str:
    """The text an admin edits: one section per part, headed '=== label ===' when there are several."""
    parts = [p for p in parts or [] if _body(p).strip()]
    if len(parts) == 1:
        return _body(parts[0]).strip()
    return "\n\n".join(f"=== {p.label or f'Part {i}'} ===\n{_body(p).strip()}" for i, p in enumerate(parts, 1))


def parse(text: str) -> List[dict]:
    """Edited text -> sections [{label, page, text}]."""
    heads = list(_HEAD.finditer(text))
    if not heads:
        return [{"label": "Verified text", "page": None, "text": text.strip()}] if text.strip() else []
    out = []
    lead = text[:heads[0].start()].strip()
    if lead:
        out.append({"label": "Verified text", "page": None, "text": lead})
    for i, m in enumerate(heads):
        body = text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip()
        if body:
            label = m.group(1).strip()
            page = re.fullmatch(r"Page (\d{1,5})", label)
            out.append({"label": label, "page": int(page.group(1)) if page else None, "text": body})
    return out


# ------------------------------------------------------------------ store
def get(sha: str) -> Optional[dict]:
    with closing(_db()) as db:
        r = db.execute("SELECT * FROM kb_verified WHERE sha=?", (sha,)).fetchone()
    if r is None:
        return None
    return {**dict(r), "sections": json.loads(r["sections"]), "changes": json.loads(r["changes"] or "{}")}


def parts_for(sha: str) -> Optional[List[Part]]:
    """The verified text of this content as extraction parts, or None."""
    v = get(sha)
    if not v:
        return None
    return [Part(s["label"], text=s["text"], page=s["page"], verified=True) for s in v["sections"]]


def ocr_parts(sha: str) -> Optional[List[Part]]:
    """The machine extraction (OCR) of this content, ignoring any verified text."""
    from app.services import kb_versions
    return kb_versions.cached_parts(sha, verified=False)


def figure_changes(before: str, after: str) -> dict:
    """Figures (rates, amounts, codes, PIN codes) that the correction removed or added."""
    from app.services.kb_review import _figure_set
    b, a = _figure_set(before), _figure_set(after)
    return {"removed": [b[k] for k in b if k not in a][:100], "added": [a[k] for k in a if k not in b][:100]}


def save(sha: str, text: str, by: str = "") -> dict:
    text = (text or "").replace("\r\n", "\n")
    if len(text) > MAX_CHARS:
        raise VerifyError("The text is too long.")
    sections = parse(text)
    if not sections:
        raise VerifyError("Enter the text of the document.")
    before = to_text(ocr_parts(sha) or [])
    changes = figure_changes(before, text)
    with closing(_db()) as db:
        db.execute("INSERT OR REPLACE INTO kb_verified(sha, sections, by, at, changes) VALUES (?,?,?,?,?)",
                   (sha, json.dumps(sections, ensure_ascii=False), by, _now(), json.dumps(changes, ensure_ascii=False)))
        db.commit()
    return {"sections": len(sections), "changes": changes}


def remove(sha: str) -> bool:
    with closing(_db()) as db:
        n = db.execute("DELETE FROM kb_verified WHERE sha=?", (sha,)).rowcount
        db.commit()
    return bool(n)


# ------------------------------------------------------------------ kinds
def kind(parts: List[Part], suffix: str = "") -> str:
    """'verified' (an admin's text), 'ocr' (image or scanned pages, not verified) or 'text'."""
    if any(p.verified for p in parts or []):
        return VERIFIED
    if suffix.lower() in IMAGE_EXTENSIONS or any(p.ocr for p in parts or []):
        return OCR
    return TEXT


def editor(sha: str, name: str) -> dict:
    """What the verification screen shows: the text to edit (verified if saved, else OCR), the OCR's figures to
    check against the image, and who verified it."""
    ocr = ocr_parts(sha) or []
    v = get(sha)
    from app.services.kb_review import _figure_set
    ocr_text = to_text(ocr)
    return {"name": name, "image": Path(name).suffix.lower() in IMAGE_EXTENSIONS,
            "text": "\n\n".join((f"=== {s['label']} ===\n" if len(v["sections"]) > 1 else "") + s["text"]
                                for s in v["sections"]) if v else ocr_text,
            "ocr_text": ocr_text, "ocr_available": bool(ocr),
            "figures": list(_figure_set(ocr_text).values())[:200],
            "verified": {"by": v["by"], "at": v["at"], "changes": v["changes"]} if v else None}
