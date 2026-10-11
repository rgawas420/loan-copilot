"""What an admin must check before OCR text is verified (Knowledge base > Verify text).

OCR confidence alone does not find misreadings: RapidOCR reads "ROI 8.50%" as "RO18.50%" with 96% confidence.
So each image / scanned PDF page is checked four ways, and every finding becomes an item the admin ticks off:

    low_confidence   an OCR box below OCR_LOW_CONFIDENCE (shown on the page image)
    dropped          an OCR box too uncertain to be indexed at all (below ocr.MIN_SCORE): its text is missing
    suspicious       a figure that looks misread: letters run into it ("RO18.50%"), O / I / l inside digits
                     ("1O.5%"), a percentage above 100, two decimal points
    only_ocr         a figure in the OCR text that an independent second reading (a vision model) does not have
    only_second      a figure the second reading has that the OCR text does not
    empty_page       a scanned page on which OCR found no text
    page             every page: the admin confirms it was compared with the original (so a page nobody looked at
                     cannot be verified, even when no check found anything)

kb_verify.save() refuses to save until every item is ticked (the ids are stable: the review is stored per file
content, SHA-256). The review runs in a background thread when an upload is checked or when the verification
screen is opened; the screen polls until it is READY. Page images (PNG, the same rendering the OCR used) are
cached next to the extraction cache.
"""
import json
import logging
import re
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.core.config import get_settings

log = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_ocr_review(sha TEXT PRIMARY KEY, status TEXT, data TEXT, error TEXT, at TEXT);
"""
RUNNING, READY, FAILED = "RUNNING", "READY", "FAILED"
PDF_DPI = 150                       # the resolution ingestion OCRs scanned PDF pages at
MAX_PAGES = 300                     # scanned pages reviewed per document
MAX_ITEMS_PER_KIND = 60             # per page: beyond this the page is unreadable anyway; the rest are summarised
_threads: dict = {}
_lock = threading.Lock()


def _db():
    from app.services.knowledge_base import _db
    return _db()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ figure checks
_GLUED = re.compile(r"(?<![A-Za-z\d])([A-Za-z]+\d[\d,]*(?:\.\d+)?\s*%)")
_CONFUSED = re.compile(r"(?<![A-Za-z\d.,])([0-9OoIl][0-9OoIl.,]*[0-9OoIl](?:\s*%)?)(?![A-Za-z\d])")
_PCT = re.compile(r"(?<![\d.])(\d{1,6}(?:\.\d+)?)\s*%")
_DOUBLE_POINT = re.compile(r"(?<![\d.])(\d+\.\d+\.\d+\s*%?)")
UNREADABLE = "[unreadable]"


def suspicious(text: str) -> List[dict]:
    """Figures that look misread: [{text, detail}], each written form once."""
    out = {}
    for m in _GLUED.finditer(text or ""):
        out.setdefault(m.group(1), "letters run into the figure; OCR often reads I as 1 (“ROI 8.5%” → “RO18.5%”)")
    for m in _CONFUSED.finditer(text or ""):
        tok = m.group(1)
        digits, letters = sum(c.isdigit() for c in tok), sum(c in "OoIl" for c in tok)
        if letters and (digits >= 2 or tok.endswith("%")):
            out.setdefault(tok, "the letter O, I or l inside a number (OCR confuses them with 0 and 1)")
    for m in _PCT.finditer(text or ""):
        if float(m.group(1)) > 100:
            out.setdefault(m.group(0), "a percentage above 100")
    for m in _DOUBLE_POINT.finditer(text or ""):
        out.setdefault(m.group(1), "two decimal points in one number")
    return [{"text": k, "detail": v} for k, v in out.items()]


def lint(text: str) -> List[dict]:
    """Problems in text an admin is about to save as verified."""
    out = suspicious(text)
    if UNREADABLE in (text or "").lower():
        n = text.lower().count(UNREADABLE)
        out.insert(0, {"text": UNREADABLE, "blocking": True,
                       "detail": f"{n} place(s) marked unreadable: type the value from the original or remove the marker"})
    return out


def _figures(text: str) -> dict:
    from app.services.kb_review import _figure_set
    return _figure_set(text or "")


# ------------------------------------------------------------------ pages and page images
def is_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def scanned_pages(path: Path, machine) -> List[int]:
    """Page numbers to review: 0 for an image; for a PDF, the pages ingestion read by OCR plus pages with (almost)
    no text layer (scans OCR could not read)."""
    from app.services.ingestion import IMAGE_EXTENSIONS, MIN_PAGE_TEXT
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return [0]
    if not is_pdf(path):
        return []
    pages = {p.page for p in machine or [] if p.ocr and p.page}
    text_pages = {p.page for p in machine or [] if not p.ocr and p.page and (p.text.strip() or p.rows)}
    try:
        import pymupdf
        with pymupdf.open(str(path)) as doc:
            for i, page in enumerate(doc, 1):
                if i not in text_pages and len(page.get_text().strip()) < MIN_PAGE_TEXT:
                    pages.add(i)
    except Exception as e:                    # pymupdf missing or the file unreadable: the OCR'd pages only
        log.info("OCR review: could not list the pages of %s: %s", path.name, e)
    return sorted(pages)[:MAX_PAGES]


def _png_path(sha: str, page: int) -> Path:
    return get_settings().kb_cache_dir / "review" / f"{sha}-{page}.png"


def page_image(sha: str, path: Path, page: int) -> bytes:
    """PNG of one page as the OCR saw it (an image with its EXIF rotation applied; a PDF page at PDF_DPI)."""
    cached = _png_path(sha, page)
    if cached.exists():
        return cached.read_bytes()
    import io
    if is_pdf(path):
        import pymupdf
        with pymupdf.open(str(path)) as doc:
            if not 1 <= page <= doc.page_count:
                raise ValueError("No such page.")
            data = doc[page - 1].get_pixmap(dpi=PDF_DPI).tobytes("png")
    else:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(path))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = buf.getvalue()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data


def _page_text(machine, page: int) -> str:
    from app.services.kb_verify import _body
    parts = [p for p in machine or [] if p.ocr and (page == 0 or p.page == page)]
    return "\n".join(_body(p).strip() for p in parts if _body(p).strip())


# ------------------------------------------------------------------ the review
def _box_for(boxes: list, surface: str) -> Optional[int]:
    key = re.sub(r"\s+", "", surface)
    return next((i for i, b in enumerate(boxes) if key and key in re.sub(r"\s+", "", b["text"])), None)


def items_for(page: dict, low: float) -> List[dict]:
    """The checklist of one reviewed page (ids are stable for stored reviews)."""
    n, label, out = page["page"], page["label"], []

    def add(kind, text, detail, box=None):
        same = [x for x in out if x["kind"] == kind]
        if len(same) == MAX_ITEMS_PER_KIND:
            out.append({"id": f"{n}-{kind}-more", "page": n, "label": label, "kind": kind, "text": "…",
                        "detail": "Many more: check the whole page against the original", "box": None})
        if len(same) >= MAX_ITEMS_PER_KIND:
            return
        out.append({"id": f"{n}-{kind}-{len(same)}", "page": n, "label": label, "kind": kind, "text": text,
                    "detail": detail, "box": box})

    add("page", label, "I compared this page with the original: every rate, amount, code and name is right"
        + ("" if page.get("second") is not None else " (no second reading: check every figure)"))
    if page.get("error"):
        add("page_error", label, f"This page could not be checked ({page['error']}): compare it with the original")
    if not page["ocr_text"].strip():
        add("empty_page", label, "OCR found no text on this page: type its text, or confirm it has none")
    for i, b in enumerate(page["boxes"]):
        if b["dropped"]:
            add("dropped", b["text"], f"OCR was unsure ({b['conf']:.0%}) and left this out of the text", i)
        elif b["conf"] < low:
            add("low_confidence", b["text"], f"read with {b['conf']:.0%} confidence", i)
    for s in suspicious(page["ocr_text"]):
        add("suspicious", s["text"], s["detail"], _box_for(page["boxes"], s["text"]))
    if page.get("second") is not None:
        fo, fs = _figures(page["ocr_text"]), _figures(page["second"])
        for k in fo:
            if k not in fs:
                add("only_ocr", fo[k], "in the OCR text but not in the second reading", _box_for(page["boxes"], fo[k]))
        for k in fs:
            if k not in fo:
                add("only_second", fs[k], "in the second reading but not in the OCR text")
    return out


def compute(sha: str, path: Path, machine=None) -> dict:
    """Review every scanned page of a file (slow: OCR of each page and a vision-model call)."""
    from app.services import ocr, vision
    s = get_settings()
    if machine is None:                         # changed on disk and not processed yet: extract it now (cached)
        from app.services.knowledge_base import machine_extract
        try:
            machine = machine_extract(path, sha)
        except Exception as e:
            log.info("OCR review: %s could not be extracted: %s", path.name, e)
            machine = []
    _, reader = vision.second_reader()
    pages = []
    for idx, n in enumerate(scanned_pages(path, machine)):
        page = {"page": n, "label": "Image" if n == 0 else f"Page {n}", "width": 0, "height": 0, "boxes": [],
                "ocr_text": _page_text(machine, n), "second": None, "second_error": None, "error": None}
        try:
            png = page_image(sha, path, n)
            page["width"], page["height"] = ocr.image_size(png)
            if ocr.available():
                page["boxes"] = ocr.read_boxes(png)
            if reader and idx < s.ocr_second_reader_max_pages:
                try:
                    page["second"] = vision.second_reading(png)
                except Exception as e:
                    page["second_error"] = f"{type(e).__name__}: {e}"[:300]
                    log.warning("OCR review: second reading of %s %s failed: %s", path.name, page["label"], e)
            elif reader:
                page["second_error"] = f"not read: only the first {s.ocr_second_reader_max_pages} pages are read twice"
        except Exception as e:
            page["error"] = f"{type(e).__name__}: {e}"[:300]
            log.warning("OCR review: %s %s: %s", path.name, page["label"], e)
        page["items"] = items_for(page, s.ocr_low_confidence)
        pages.append(page)
    return {"pages": pages, "reader": reader, "low_confidence": s.ocr_low_confidence, "at": _now()}


def _store(sha: str, status: str, data: Optional[dict] = None, error: Optional[str] = None):
    with closing(_db()) as db:
        db.execute("INSERT OR REPLACE INTO kb_ocr_review(sha, status, data, error, at) VALUES (?,?,?,?,?)",
                   (sha, status, json.dumps(data, ensure_ascii=False) if data is not None else None, error, _now()))
        db.commit()


def run(sha: str, path: Path, machine=None) -> dict:
    """Compute and store the review now (the background thread; tests call it directly)."""
    try:
        data = compute(sha, path, machine)
    except Exception as e:
        log.exception("OCR review of %s failed", path.name)
        _store(sha, FAILED, error=f"{type(e).__name__}: {e}"[:500])
        raise
    _store(sha, READY, data)
    return data


def get(sha: str) -> Optional[dict]:
    """{status, error, at, data} or None. A RUNNING review whose thread is gone (server restarted) reads as None."""
    with closing(_db()) as db:
        r = db.execute("SELECT * FROM kb_ocr_review WHERE sha=?", (sha,)).fetchone()
    if r is None:
        return None
    if r["status"] == RUNNING and not (_threads.get(sha) and _threads[sha].is_alive()):
        return None
    return {"status": r["status"], "error": r["error"], "at": r["at"],
            "data": json.loads(r["data"]) if r["data"] else None}


def start(sha: str, path: Path, force: bool = False) -> Optional[dict]:
    """Start the review in the background unless it is running or done (force: run it again). Returns get()."""
    if not get_settings().ocr_review:
        return None
    with _lock:
        cur = get(sha)
        if cur and (cur["status"] == RUNNING or not force):
            return cur
        _store(sha, RUNNING)
        t = threading.Thread(target=_run_quietly, args=(sha, Path(path)), name=f"ocr-review-{sha[:8]}", daemon=True)
        _threads[sha] = t
        t.start()
    return get(sha)


def _run_quietly(sha: str, path: Path):
    try:
        run(sha, path)
    except Exception:
        pass                                    # stored as FAILED and logged by run()
    finally:
        _threads.pop(sha, None)


def required_items(review: Optional[dict]) -> List[dict]:
    """Every item of a READY review (the ones that must be ticked before saving)."""
    if not review or review["status"] != READY or not review["data"]:
        return []
    return [it for p in review["data"]["pages"] for it in p["items"]]
