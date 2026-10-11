"""OCR for images and scanned PDF pages (RapidOCR on ONNX Runtime: pip-installable, no system binaries).

Text boxes are regrouped into reading-order lines so tables and posters keep their row structure.
"""
import threading
from statistics import median

from app.core.config import get_settings

_engine = None
_lock = threading.Lock()
MIN_SCORE = 0.5


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


def ocr_image(data: bytes) -> str:
    """Return the text in an image (PNG/JPG/WEBP/... bytes), one visual line per text line."""
    if not available():
        raise ValueError("OCR is disabled or rapidocr_onnxruntime is not installed.")
    engine = _get_engine()
    with _lock:                                   # the ONNX session is not guaranteed thread-safe
        result, _ = engine(data)
    boxes = []
    for box, text, score in result or []:
        if float(score) < MIN_SCORE or not text.strip():
            continue
        ys, xs = [p[1] for p in box], [p[0] for p in box]
        boxes.append(((min(ys) + max(ys)) / 2, max(ys) - min(ys), min(xs), text.strip()))
    if sum(ch.isalnum() for b in boxes for ch in b[3]) < 3:      # stickers, photos: nothing to index
        return ""
    tol = 0.5 * median(b[1] for b in boxes)
    lines, current, line_y = [], [], None
    for b in sorted(boxes):
        if line_y is not None and abs(b[0] - line_y) > tol:
            lines.append(current)
            current = []
        current.append(b)
        line_y = b[0] if len(current) == 1 else line_y
    lines.append(current)
    return "\n".join("  ".join(b[3] for b in sorted(line, key=lambda b: b[2])) for line in lines)
