"""Files a user attaches to a chat message. The text is extracted once and kept per user. Attachments are never
added to the shared knowledge base and only their owner can use or view them.

Images are read the way a chat assistant looks at a picture: a vision model transcribes the text with its
layout (tables stay tables) and describes the image (app/services/vision.py); OCR is the fallback. The image
itself is kept so the chat can show it as a thumbnail. For other files only the extracted text is kept.
"""
import json
import re
import secrets
from pathlib import Path
from typing import List, NamedTuple, Optional

from app.core.config import get_settings
from app.services.ingestion import ALLOWED_EXTENSIONS, IMAGE_EXTENSIONS, extract

MAX_CHARS = 60000
ID_RE = re.compile(r"^[0-9a-f]{24}$")
MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
         ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff"}


class Attachment(NamedTuple):
    name: str
    text: str
    id: str = ""
    kind: str = "file"                 # "image" or "file"


class AttachmentError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _dir(user_id: int) -> Path:
    return get_settings().attachment_dir / str(int(user_id))


def _render(parts) -> str:
    out = []
    for p in parts:
        block = [x for x in (p.label, p.context, p.text, "\n".join(p.rows)) if x]
        if block:
            out.append("\n".join(block))
    return "\n\n".join(out).strip()


def save(user_id: int, filename: str, data: bytes) -> dict:
    s = get_settings()
    name = Path((filename or "").replace("\\", "/")).name.strip() or "attachment"
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AttachmentError(f"{name}: this file type cannot be read. Use PDF, Word, Excel, CSV, text or an image.")
    if not data:
        raise AttachmentError(f"{name}: the file is empty.")
    if len(data) > s.attachment_max_mb * 1024 * 1024:
        raise AttachmentError(f"{name}: larger than {s.attachment_max_mb} MB.", 413)
    is_image = ext in IMAGE_EXTENSIONS
    aid = secrets.token_hex(12)
    d = _dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    original = d / f"{aid}{ext}"
    original.write_bytes(data)
    ocr, text, read_with = "", "", "text"
    try:
        if is_image:                              # the vision model first; OCR only when it is unavailable
            from app.services import vision
            text = vision.read_image(data) or ""
            read_with = "vision" if text else "ocr"
        if not text:
            try:
                ocr = text = _render(extract(original))
            except Exception as e:
                if not is_image:
                    raise AttachmentError(f"{name}: could not read the file ({type(e).__name__}).", 422)
        if not text:
            raise AttachmentError(f"{name}: no readable text was found in this "
                                  f"{'image' if is_image else 'file'}.", 422)
    except AttachmentError:
        original.unlink(missing_ok=True)
        raise
    if not is_image:
        original.unlink(missing_ok=True)          # only images are kept (to show them in the chat)
    info = {"id": aid, "name": name[:160], "size": len(data), "chars": len(text), "truncated": len(text) > MAX_CHARS,
            "kind": "image" if is_image else "file", "read_with": read_with}
    (d / f"{aid}.json").write_text(json.dumps({**info, "ext": ext, "text": text[:MAX_CHARS], "ocr": ocr[:MAX_CHARS]},
                                              ensure_ascii=False), encoding="utf-8")
    return info


def load(user_id: int, ids: List[str]) -> List[Attachment]:
    """The user's own attachments; unknown ids are ignored."""
    out = []
    for aid in ids[:5]:
        if not ID_RE.match(aid or ""):
            continue
        f = _dir(user_id) / f"{aid}.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            out.append(Attachment(data["name"], data["text"], aid, data.get("kind", "file")))
    return out


def image_file(user_id: int, aid: str) -> Optional[tuple]:
    """(path, media type) of the user's own attached image, or None."""
    if not ID_RE.match(aid or ""):
        return None
    f = _dir(user_id) / f"{aid}.json"
    if not f.exists():
        return None
    ext = json.loads(f.read_text(encoding="utf-8")).get("ext", "")
    p = _dir(user_id) / f"{aid}{ext}"
    return (p, MEDIA.get(ext, "application/octet-stream")) if ext in MEDIA and p.exists() else None


def delete(user_id: int, aid: str) -> bool:
    if not ID_RE.match(aid or ""):
        return False
    f = _dir(user_id) / f"{aid}.json"
    if f.exists():
        ext = json.loads(f.read_text(encoding="utf-8")).get("ext", "")
        if ext:
            (_dir(user_id) / f"{aid}{ext}").unlink(missing_ok=True)
        f.unlink()
        return True
    return False


def owned(user_id: int, aid: Optional[str]) -> bool:
    return bool(aid and ID_RE.match(aid) and (_dir(user_id) / f"{aid}.json").exists())
