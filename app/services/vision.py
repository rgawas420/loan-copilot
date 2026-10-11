"""Reading attached images with a vision model, the way a chat assistant "looks" at a picture.

The model transcribes every piece of text exactly (numbers, percentages, names), keeps tables as Markdown
tables and headings as headings, then says briefly what the image is. Posters and rate cards have multi-column
layouts that OCR reads line by line across columns (mixing up which figure belongs to which product); the
vision model keeps them together. OCR is still run, and is used when the vision model is not configured or fails.
"""
import base64
import io
import logging
from typing import Optional

log = logging.getLogger(__name__)
MAX_SIDE = 2048                     # longest image side sent to the model
MAX_BYTES = 3_500_000               # stay under the provider's 4 MB limit for inline images

PROMPT = """Read this image for a loan channel partner.

1. Transcribe ALL the text in the image exactly as written: names, numbers, amounts, percentages, dates, codes,
   phone numbers. Do not correct, round or convert anything. Keep the reading order of the layout.
2. Render tables as Markdown tables, keeping each value in its row and column. Use Markdown headings for titles
   and bullet lists for lists.
3. If some text is unreadable, write [unreadable] instead of guessing.
4. After the transcription, add a section "## What this image shows" with one to three sentences describing the
   image (e.g. "A product one-pager for ... listing ...").

Output only the Markdown, without code fences."""


def _prepare(data: bytes) -> tuple:
    """(base64 JPEG/PNG, mime) small enough to send."""
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    for quality in (90, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        if buf.tell() <= MAX_BYTES:
            break
        img.thumbnail((int(img.size[0] * 0.8), int(img.size[1] * 0.8)))
    return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"


SECOND_READING_PROMPT = """Transcribe this scanned page of a bank loan document (policy, rate sheet, payout sheet or
poster) so that a person can check another program's reading of it.

- Transcribe ALL the text exactly as written: names, numbers, amounts, percentages, dates, codes, PIN codes, phone
  numbers. Do not correct, round, convert, summarise or translate anything.
- Keep the reading order. On a multi-column poster, finish one column or panel before the next.
- Render every table as a Markdown table, keeping each value in its own row and column. Use Markdown headings for
  titles and bullet lists for lists.
- If a character or value cannot be read with certainty, write [unreadable] in its place. Never guess a figure.

Output only the transcription in Markdown, without code fences and without any comment."""


def second_reader():
    """(LangChain chat model, its name) for second readings of OCR'd pages, or (None, "") when not configured."""
    from app.core.config import get_settings
    from app.rag import llm as L
    s = get_settings()
    choice = (s.ocr_second_reader or "auto").lower()
    if choice == "off":
        return None, ""
    if choice == "openai" or (choice == "auto" and s.openai_api_key):
        if not s.openai_api_key:
            return None, ""
        model = s.ocr_second_reader_model or "gpt-5"
        params = {"model": model, "max_tokens": 16000, "timeout": max(s.llm_timeout_s, 180)}
        if model.startswith(("gpt-5", "o")):
            params["reasoning_effort"] = "medium"
        return L._build_openai(L._freeze(params)), model
    if choice in ("groq", "auto") and s.groq_api_key and (s.ocr_second_reader_model or s.vision_model):
        model = s.ocr_second_reader_model or s.vision_model
        return L._build(L._freeze({"model": model, "temperature": 0, "max_tokens": 6000})), model
    return None, ""


def second_reading(data: bytes) -> Optional[str]:
    """An independent Markdown transcription of a page image, or None when no reader is configured. Raises when
    the call fails (the caller records the error)."""
    llm, _ = second_reader()
    if llm is None:
        return None
    from langchain_core.messages import HumanMessage
    from app.rag.metering import purpose
    b64, mime = _prepare(data)
    msg = HumanMessage(content=[{"type": "text", "text": SECOND_READING_PROMPT},
                                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}])
    with purpose("ocr_review"):
        text = (llm.invoke([msg]).content or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("markdown").strip()
    return text


def available() -> bool:
    from app.core.config import get_settings
    s = get_settings()
    return bool(s.groq_api_key and s.vision_model)


def read_image(data: bytes) -> Optional[str]:
    """Markdown transcription of the image, or None when no vision model is configured or the call fails."""
    if not available():
        return None
    from langchain_core.messages import HumanMessage
    from app.rag.llm import get_llm
    from app.rag.metering import purpose
    llm = get_llm("vision")
    if llm is None:
        return None
    try:
        b64, mime = _prepare(data)
        msg = HumanMessage(content=[{"type": "text", "text": PROMPT},
                                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}])
        with purpose("attachment"):
            text = (llm.invoke([msg]).content or "").strip()
    except Exception as e:
        log.warning("Vision model could not read the image: %s", e)
        return None
    if text.startswith("```"):
        text = text.strip("`").removeprefix("markdown").strip()
    return text or None
