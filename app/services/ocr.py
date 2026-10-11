"""OCR for images and scanned PDF pages (RapidOCR on ONNX Runtime: pip-installable, no system binaries).

Text boxes are regrouped into reading-order lines so tables and posters keep their row structure.
ocr_image() returns the text that is indexed; read_boxes() returns every box with its confidence and position,
for the verification screen (ocr_review), including the boxes too uncertain to be indexed.
"""
import io
import threading
from statistics import median

from app.core.config import get_settings

_engine = None
_lock = threading.Lock()
MIN_SCORE = 0.5                   # boxes below this are left out of the indexed text


def available() -> bool:
    if not get_settings().ocr_enabled:
        return False
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def _get_engine():
    global _engine
    with _lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
    return _engine


def _run(data: bytes):
    if not available():
        raise ValueError("OCR is disabled or rapidocr_onnxruntime is not installed.")
    engine = _get_engine()
    with _lock:                                   # the ONNX session is not guaranteed thread-safe
        result, _ = engine(data)
    return result or []


def _box(box, text, score) -> dict:
    ys, xs = [p[1] for p in box], [p[0] for p in box]
    return {"y": (min(ys) + max(ys)) / 2, "h": max(ys) - min(ys), "x": min(xs), "text": text.strip(),
            "conf": round(float(score), 3), "box": [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))]}


def _lines(boxes: list) -> list:
    """Boxes grouped into visual lines (top to bottom), each line left to right."""
    if not boxes:
        return []
    tol = 0.5 * median(b["h"] for b in boxes)
    lines, current, line_y = [], [], None
    for b in sorted(boxes, key=lambda b: (b["y"], b["h"], b["x"], b["text"])):
        if line_y is not None and abs(b["y"] - line_y) > tol:
            lines.append(current)
            current = []
        current.append(b)
        line_y = b["y"] if len(current) == 1 else line_y
    lines.append(current)
    return [sorted(line, key=lambda b: b["x"]) for line in lines]


def ocr_image(data: bytes) -> str:
    """Return the text in an image (PNG/JPG/WEBP/... bytes), one visual line per text line."""
    boxes = [_box(*r) for r in _run(data) if float(r[2]) >= MIN_SCORE and r[1].strip()]
    if sum(ch.isalnum() for b in boxes for ch in b["text"]) < 3:      # stickers, photos: nothing to index
        return ""
    return "\n".join("  ".join(b["text"] for b in line) for line in _lines(boxes))


def image_size(data: bytes) -> tuple:
    """(width, height) of the image as displayed (EXIF orientation applied, as OCR and browsers do)."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    return img.size


def read_boxes(data: bytes) -> list:
    """Every text box in reading order: {text, conf, box: [x0, y0, x1, y1], line, dropped}. dropped = below
    MIN_SCORE, so not in the indexed text."""
    boxes = [_box(*r) for r in _run(data) if r[1].strip()]
    out = []
    for n, line in enumerate(_lines(boxes)):
        out += [{"text": b["text"], "conf": b["conf"], "box": b["box"], "line": n, "dropped": b["conf"] < MIN_SCORE}
                for b in line]
    return out
