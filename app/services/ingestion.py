"""Load lender documents of any supported format and chunk them.

Extraction turns a file into "parts": free text (a PDF page, a DOCX body, OCR of an image) or a table
(a spreadsheet sheet, CSV, a DOCX table, a JSON list). Tables are chunked row by row and every chunk repeats
the file name, sheet and any title rows above the header, so a row is never separated from its context.
Each chunk starts with "Source: <file>" because file names often carry the lender / RM / product.

Structure is preserved where the format has it: PDF pages keep their page number, headings (detected from font
size / bold), tables (as rows) and drop running headers / footers; Word documents keep their heading hierarchy,
lists and tables in document order; Markdown and HTML are split by heading. Each part carries its page and
heading path ("Eligibility > Loan Amount"), and every chunk starts with a context header (source, document,
bank, product, version, section, page) so a chunk is never separated from what it is about.

Markdown policy sheets are split per program section ("## Program: ...") so each chunk keeps its lender,
program and loan type as metadata. A section longer than one chunk is split, and every piece repeats the
lender/program header and the Banker/RM contact line, so contacts are never separated from their program.
"""
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, List, Optional

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

TEXT_EXTENSIONS = {".txt", ".md", ".text"}
TABLE_EXTENSIONS = {".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".xlsb"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | TABLE_EXTENSIONS | IMAGE_EXTENSIONS | {
    ".pdf", ".docx", ".doc", ".json", ".html", ".htm", ".vcf"}
POLICY_EXTENSIONS = {".yaml", ".yml"}
CHUNK_SIZE = 1000
MIN_PAGE_TEXT = 40              # a PDF page with less text than this is treated as scanned and OCR'd
HEADER_SCAN_ROWS = 15
EXTRACTOR_VERSION = 1           # default cache version; see EXTRACTOR_VERSIONS
# Bump a format's number after changing its loader: only that format's cached extractions are redone
# (so images and spreadsheets are not OCR'd / re-read again when the PDF loader changes).
EXTRACTOR_VERSIONS = {".pdf": 3, ".docx": 2, ".doc": 2, ".md": 2, ".txt": 2, ".text": 2, ".html": 2, ".htm": 2,
                      ".xls": 2, ".xlsx": 2, ".xlsm": 2}       # 2: percentage cells as "11.5%", not 0.115
CHUNKER_VERSION = "2"           # bump when chunk text or metadata changes: the next sync re-indexes every file
HEADER_PREFIXES = ("Source:", "Document:", "Section:", "Lender:")

_splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=150, add_start_index=True)
_CONTACT = re.compile(r"(?mi)^.*Banker/RM:.*$")


def extractor_version(ext: str) -> int:
    return EXTRACTOR_VERSIONS.get(ext.lower(), EXTRACTOR_VERSION)


@dataclass
class Part:
    label: str = ""                                   # "Page 3", "Sheet: LAP", "Table 2"
    text: str = ""                                    # free text
    context: str = ""                                 # table title rows, repeated in every chunk
    rows: List[str] = field(default_factory=list)     # rendered table rows ("Header: value | ...")
    page: Optional[int] = None                        # 1-based page number (PDF)
    heading: str = ""                                 # heading path: "Eligibility > Loan Amount"
    ocr: bool = False                                 # read by OCR (image, scanned PDF page): may misread figures
    verified: bool = False                            # text typed / corrected by an admin (kb_verify)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Part":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ cell / table helpers
class Percent(float):
    """A spreadsheet number formatted as a percentage: 0.115 shown as 11.5% in Excel is indexed as "11.5%"."""


def _percent(v, number_format: str):
    return Percent(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and "%" in (number_format or "") else v


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, Percent):
        return f"{round(float(v) * 100, 4):g}%"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time(0) else v.isoformat(sep=" ", timespec="minutes")
    if isinstance(v, (date, time)):
        return v.isoformat()
    return re.sub(r"\s+", " ", str(v)).strip()


def table_part(label: str, rows: Iterable[Iterable]) -> Optional[Part]:
    """Render a grid as "Header: value | ..." rows. Title rows above the header become context."""
    grid = [[_cell(v) for v in r] for r in rows]
    grid = [r for r in grid if any(r)]
    if not grid:
        return None
    window = grid[:HEADER_SCAN_ROWS]
    widest = max(sum(1 for v in r if v) for r in window)
    h = next((i for i, r in enumerate(window)
              if sum(1 for v in r if v) >= max(2, widest / 2)
              and sum(1 for v in r if v and not re.fullmatch(r"[\d.,%\-/ ]+", v)) >= sum(1 for v in r if v) / 2),
             None)
    if h is None or widest < 2:                       # no usable header: plain rows
        rows = [" | ".join(v for v in r if v) for r in grid]
        if widest == 1 and len(rows) > 1 and not re.search(r"\d", rows[0]):   # single column with a title
            return Part(label=label, context=rows[0][:300], rows=rows[1:])
        return Part(label=label, rows=rows)
    header = grid[h]
    context = " ".join(" ".join(v for v in r if v) for r in grid[:h])
    out = []
    for r in grid[h + 1:]:
        cells = [f"{header[i]}: {v}" if i < len(header) and header[i] else v for i, v in enumerate(r) if v]
        if cells:
            out.append(" | ".join(cells))
    return Part(label=label, context=context[:300], rows=out) if out else None


# ------------------------------------------------------------------ format loaders
def _read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


class _Headings:
    """Heading path by level: a new heading replaces its level and everything below it."""

    def __init__(self):
        self.stack: List[tuple] = []                  # (level, text)

    def push(self, level: float, text: str):
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, text.strip()[:120]))
        del self.stack[:-3]                           # keep the three innermost levels

    @property
    def path(self) -> str:
        return " > ".join(t for _, t in self.stack)


_BULLET = re.compile(r"^\s*([•●▪◦‣\-–*]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s+")
_SHAPE = re.compile(r"\d+")


def _pdf_furniture(pages_lines) -> set:
    """Running headers / footers: short lines near the top or bottom that repeat on at least half the pages
    (digits ignored, so "Page 3 of 12" counts as the same line)."""
    if len(pages_lines) < 3:
        return set()
    counts = Counter()
    for lines in pages_lines:
        edge = {_SHAPE.sub("#", ln["text"].strip().lower()) for ln in lines if ln["edge"] and len(ln["text"]) <= 120}
        counts.update(edge)
    return {k for k, n in counts.items() if k and n >= max(3, len(pages_lines) / 2)}


def _pdf_lines(page, skip_boxes) -> List[dict]:
    """Text lines of a page in reading order, with font size, boldness and whether they sit at the page edge.
    Lines inside table areas are skipped (tables are extracted separately)."""
    h = page.rect.height or 1
    out = []
    for block in page.get_text("dict", sort=True).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            x0, y0, x1, y1 = line["bbox"]
            if any(bx0 - 2 <= x0 and by0 - 2 <= y0 and x1 <= bx1 + 2 and y1 <= by1 + 2 for bx0, by0, bx1, by1 in skip_boxes):
                continue
            text = re.sub(r"\s+", " ", "".join(s["text"] for s in spans)).strip()
            chars = sum(len(s["text"].strip()) for s in spans) or 1
            size = sum(s.get("size", 0) * len(s["text"].strip()) for s in spans) / chars
            bold = all((s.get("flags", 0) & 16) or "bold" in s.get("font", "").lower() for s in spans)
            out.append({"text": text, "size": round(size, 1), "bold": bold, "block": id(block),
                        "edge": y0 < h * 0.08 or y1 > h * 0.92})
    return out


def _is_heading(ln: dict, body: float) -> Optional[float]:
    """Heading level (smaller = higher) or None. Larger-than-body text, or a short bold line, is a heading."""
    t = ln["text"]
    if len(t) > 120 or len(t) < 3 or t.endswith((".", ",", ";")) or _BULLET.match(t) or not re.search(r"[A-Za-z]", t):
        return None
    if ln["size"] >= body * 1.15:
        return round(100 - ln["size"], 1)            # bigger font -> higher level
    if ln["bold"] and len(t) <= 80 and ln["size"] >= body * 0.95:
        return 100 - body + 0.5                       # bold body-size heading: lowest level
    return None


def _pdf(path: Path) -> List[Part]:
    from app.services import ocr
    try:
        import pymupdf
    except ImportError:                               # text-only fallback
        from pypdf import PdfReader
        return [Part(f"Page {i}", text=(p.extract_text() or "").strip(), page=i)
                for i, p in enumerate(PdfReader(str(path)).pages, 1)]
    with pymupdf.open(str(path)) as doc:
        pages = []                                    # (page_no, lines, tables, ocr_text)
        for i, page in enumerate(doc, 1):
            tables, boxes = [], []
            try:
                for t in page.find_tables().tables:
                    rows = t.extract()
                    if rows and len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                        tables.append(rows)
                        boxes.append(tuple(t.bbox))
            except Exception:                         # table detection is best effort
                tables, boxes = [], []
            lines = _pdf_lines(page, boxes)
            ocr_text = ""
            if sum(len(ln["text"]) for ln in lines) < MIN_PAGE_TEXT and not tables and ocr.available():
                ocr_text = ocr.ocr_image(page.get_pixmap(dpi=150).tobytes("png"))
            pages.append((i, lines, tables, ocr_text))

    sizes = Counter()
    for _, lines, _, _ in pages:
        for ln in lines:
            sizes[ln["size"]] += len(ln["text"])
    body = sizes.most_common(1)[0][0] if sizes else 10.0
    furniture = _pdf_furniture([p[1] for p in pages])
    heads = _Headings()
    parts: List[Part] = []
    for i, lines, tables, ocr_text in pages:
        buf: List[str] = []
        last_block = None

        def flush():
            text = "\n".join(buf).strip()
            if text:
                parts.append(Part(f"Page {i}", text=text, page=i, heading=heads.path))
            buf.clear()

        for ln in lines:
            if ln["edge"] and _SHAPE.sub("#", ln["text"].strip().lower()) in furniture:
                continue
            level = _is_heading(ln, body)
            if level is not None:
                flush()
                heads.push(level, ln["text"])
                last_block = None
                continue
            if buf and ln["block"] == last_block and not _BULLET.match(ln["text"]):
                buf[-1] += " " + ln["text"]           # a wrapped line continues the paragraph
            else:
                buf.append(ln["text"])
            last_block = ln["block"]
        flush()
        if ocr_text.strip():
            parts.append(Part(f"Page {i}", text=ocr_text.strip(), page=i, heading=heads.path, ocr=True))
        for n, rows in enumerate(tables, 1):
            tp = table_part(f"Page {i}", rows)
            if tp:
                tp.page, tp.heading = i, heads.path
                tp.context = (f"Table {n}. " + tp.context).strip() if len(tables) > 1 else tp.context
                parts.append(tp)
    return parts


def _docx_blocks(d):
    """Paragraphs and tables of a Word document in document order."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for el in d.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(el, d)
        elif tag == "tbl":
            yield Table(el, d)


def _docx_heading_level(p) -> Optional[float]:
    style = (p.style.name if p.style is not None else "") or ""
    m = re.match(r"(?i)heading\s*(\d)", style)
    if m:
        return float(m.group(1))
    if style.lower() == "title":
        return 0.0
    text = p.text.strip()
    runs = [r for r in p.runs if r.text.strip()]
    if runs and all(r.bold for r in runs) and 3 <= len(text) <= 80 and not text.endswith((".", ",", ";", ":")):
        return 9.0                                    # a short all-bold line acts as a heading
    return None


def _docx(path: Path) -> List[Part]:
    import docx
    from docx.table import Table
    d = docx.Document(str(path))
    heads, parts, buf, tables = _Headings(), [], [], 0

    def flush():
        text = "\n".join(buf).strip()
        if text:
            parts.append(Part(text=text, heading=heads.path))
        buf.clear()

    for block in _docx_blocks(d):
        if isinstance(block, Table):
            flush()
            tables += 1
            tp = table_part(f"Table {tables}", ([c.text for c in row.cells] for row in block.rows))
            if tp:
                tp.heading = heads.path
                parts.append(tp)
            continue
        text = block.text.strip()
        if not text:
            continue
        level = _docx_heading_level(block)
        if level is not None:
            flush()
            heads.push(level, text)
            continue
        style = (block.style.name if block.style is not None else "") or ""
        is_list = "list" in style.lower() or block._p.pPr is not None and block._p.pPr.numPr is not None
        buf.append(f"- {text}" if is_list and not _BULLET.match(text) else text)
    flush()
    return parts


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _markdown_parts(text: str) -> List[Part]:
    """Split Markdown-style text at its headings; text without headings stays one part."""
    heads, parts, buf = _Headings(), [], []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            parts.append(Part(text=body, heading=heads.path))
        buf.clear()

    for line in text.splitlines():
        m = _MD_HEADING.match(line)
        if m:
            flush()
            heads.push(len(m.group(1)), m.group(2))
        else:
            buf.append(line)
    flush()
    return parts or [Part(text=text)]


def _doc(path: Path) -> List[Part]:
    """Legacy Word: convert with LibreOffice when it is installed."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise ValueError("Legacy .doc needs LibreOffice (soffice) on PATH; or save the file as .docx.")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([soffice, "--headless", "--convert-to", "docx", "--outdir", tmp, str(path)],
                       check=True, capture_output=True, timeout=180)
        return _docx(next(Path(tmp).glob("*.docx")))


def _csv(path: Path) -> List[Part]:
    text = _read_text_file(path)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    tp = table_part("", csv.reader(io.StringIO(text), dialect))
    return [tp] if tp else []


def _xls(path: Path) -> List[Part]:
    import xlrd
    try:
        book = xlrd.open_workbook(str(path), on_demand=True, formatting_info=True)
    except NotImplementedError:                       # formatting not readable for this file: values as they are
        book = xlrd.open_workbook(str(path), on_demand=True)
    formats = {}
    if book.formatting_info:
        for i, xf in enumerate(book.xf_list):
            f = book.format_map.get(xf.format_key)
            formats[i] = f.format_str if f else ""
    parts = []
    for sh in book.sheets():
        def rows(sh=sh):
            for r in range(sh.nrows):
                yield [xlrd.xldate_as_datetime(c.value, book.datemode) if c.ctype == xlrd.XL_CELL_DATE
                       else _percent(c.value, formats.get(c.xf_index, "")) if c.ctype == xlrd.XL_CELL_NUMBER else c.value
                       for c in sh.row(r)]
        tp = table_part(f"Sheet: {sh.name}", rows())
        if tp:
            parts.append(tp)
    return parts


def _xlsx(path: Path) -> List[Part]:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        def rows(ws):
            for row in ws.iter_rows():
                yield [_percent(c.value, getattr(c, "number_format", "")) for c in row]
        return [tp for ws in wb.worksheets if (tp := table_part(f"Sheet: {ws.title}", rows(ws)))]
    finally:
        wb.close()


def _xlsb(path: Path) -> List[Part]:
    from pyxlsb import open_workbook
    parts = []
    with open_workbook(str(path)) as wb:
        for name in wb.sheets:
            with wb.get_sheet(name) as sh:
                tp = table_part(f"Sheet: {name}", ([c.v for c in row] for row in sh.rows()))
            if tp:
                parts.append(tp)
    return parts


def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list) and not all(isinstance(v, (str, int, float, bool, type(None))) for v in obj):
        for i, v in enumerate(obj):
            yield from _flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, ", ".join(map(_cell, obj)) if isinstance(obj, list) else _cell(obj)


def _json(path: Path) -> List[Part]:
    data = json.loads(_read_text_file(path))
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        rows = [" | ".join(f"{k}: {v}" for k, v in _flatten(x) if v) for x in data]
    else:
        rows = [f"{k}: {v}" for k, v in _flatten(data) if v]
    return [Part(rows=[r for r in rows if r])]


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self):
        super().__init__()
        self.out, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append(f"\n{'#' * int(tag[1])} ")    # headings become Markdown headings
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def _html(path: Path) -> List[Part]:
    p = _HTMLText()
    p.feed(_read_text_file(path))
    text = "\n".join(re.sub(r"[ \t]+", " ", ln).strip(" |") for ln in "".join(p.out).splitlines())
    return _markdown_parts(re.sub(r"\n{3,}", "\n\n", text).strip())


def _vcf(path: Path) -> List[Part]:
    names = {"FN": "Name", "ORG": "Organization", "TITLE": "Title", "TEL": "Phone", "EMAIL": "Email",
             "ADR": "Address", "NOTE": "Note"}
    lines = []
    for raw in _read_text_file(path).splitlines():
        key, _, val = raw.partition(":")
        prop = key.split(";")[0].upper()
        if prop == "BEGIN" and lines:
            lines.append("")
        elif prop in names and val.strip():
            lines.append(f"{names[prop]}: {val.replace(';', ' ').strip()}")
    return [Part(text="\n".join(lines))]


def _image(path: Path) -> List[Part]:
    from app.services import ocr
    return [Part("OCR", text=ocr.ocr_image(path.read_bytes()), ocr=True)]


_LOADERS = {".pdf": _pdf, ".docx": _docx, ".doc": _doc, ".csv": _csv, ".tsv": _csv, ".xls": _xls,
            ".xlsx": _xlsx, ".xlsm": _xlsx, ".xlsb": _xlsb, ".json": _json, ".html": _html, ".htm": _html,
            ".vcf": _vcf}


def extract(path: Path) -> List[Part]:
    """Read a file into text / table parts. Raises ValueError for unsupported or unreadable files."""
    ext = path.suffix.lower()
    if ext in TEXT_EXTENSIONS:
        text = _read_text_file(path)
        parts = _markdown_parts(text) if ext == ".md" or len(re.findall(r"(?m)^#{1,6} \S", text)) >= 2 else [Part(text=text)]
    elif ext in IMAGE_EXTENSIONS:
        parts = _image(path)
    elif ext in _LOADERS:
        parts = _LOADERS[ext](path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")
    return [p for p in parts if p.text.strip() or p.rows]


def load_text(path: Path) -> str:
    return "\n\n".join(p.text or "\n".join(filter(None, [p.context, *p.rows])) for p in extract(path))


# ------------------------------------------------------------------ chunking
def document_line(meta: dict) -> str:
    """'Document: HDFC BL Policy | Bank: HDFC Bank | Product: Business Loan | Version: v3' (empty without a title)."""
    if not meta.get("title"):
        return ""
    bits = [f"Document: {meta['title']}"]
    for key, label in (("bank", "Bank"), ("product", "Product"), ("category", "Category")):
        if meta.get(key):
            bits.append(f"{label}: {meta[key]}")
    if meta.get("version"):
        bits.append(f"Version: v{meta['version']}")
    return " | ".join(bits)


def chunk_header(source: str, p: Part, meta: dict, lead: str = "") -> str:
    """Context repeated at the top of every chunk of a part: what file, document, section and page it is from."""
    where = f"Source: {source}" + (f" | {p.label}" if p.label else "")
    section = f"Section: {p.heading}" if p.heading else ""
    return "\n".join(x for x in (lead, where, document_line(meta), section, p.context) if x)


def strip_header(text: str) -> str:
    """Chunk text without its context header lines."""
    lines = text.splitlines()
    while lines and lines[0].startswith(HEADER_PREFIXES):
        lines = lines[1:]
    return "\n".join(lines).strip()


def chunk_parts(parts: List[Part], source: str, meta: dict, lead: str = "") -> List[Document]:
    """Chunk extracted parts, one section at a time. Every chunk starts with a context header: `lead`
    (e.g. "Lender: X"), "Source: file | page/sheet", the document line, the section heading and table titles."""
    docs = []
    for p in parts:
        head = chunk_header(source, p, meta, lead)
        m = {**meta, "source": source, "section": p.heading or p.label, "part": p.label}
        if p.page:
            m["page"] = p.page
        budget = max(200, CHUNK_SIZE - len(head))
        if p.text.strip():
            pieces = [p.text.strip()] if len(p.text) <= budget else _splitter.split_text(p.text)
            docs.extend(Document(page_content=f"{head}\n{x}", metadata=dict(m)) for x in pieces)
        buf, size = [], 0
        for row in p.rows:
            for piece in ([row] if len(row) <= budget else _splitter.split_text(row)):
                if buf and size + len(piece) + 1 > budget:
                    docs.append(Document(page_content=head + "\n" + "\n".join(buf), metadata=dict(m)))
                    buf, size = [], 0
                buf.append(piece)
                size += len(piece) + 1
        if buf:
            docs.append(Document(page_content=head + "\n" + "\n".join(buf), metadata=dict(m)))
    return docs


def _split_markdown(text: str, source: str) -> List[Document]:
    header = re.match(r"#\s*Lender:\s*(.+)", text)
    lender = header.group(1).split("(")[0].strip() if header else "Unknown"
    docs = []
    for sec in re.split(r"(?m)^##\s+", text)[1:]:
        title, _, body = sec.partition("\n")
        lt = re.search(r"Loan type:\s*(.+)", body)
        meta = {"source": source, "lender": lender, "program": title.replace("Program:", "").strip(),
                "loan_type": lt.group(1).strip() if lt else "Unknown"}
        head = f"Lender: {lender}\n{title.strip()}"
        body = body.strip()
        if len(head) + len(body) <= CHUNK_SIZE:
            docs.append(Document(page_content=f"{head}\n{body}", metadata=meta))
            continue
        contact = _CONTACT.search(body)
        contact_line = contact.group(0).strip() if contact else ""
        rest = _CONTACT.sub("", body).strip()
        for piece in _splitter.split_text(rest):
            docs.append(Document(page_content="\n".join(x for x in (head, piece, contact_line) if x),
                                 metadata=dict(meta)))
    return docs


def load_and_chunk(path: Path, source_name: Optional[str] = None, lender: Optional[str] = None,
                   loan_type: Optional[str] = None, parts: Optional[List[Part]] = None,
                   extra_meta: Optional[dict] = None) -> List[Document]:
    """Chunk one file. `parts` lets the caller pass a cached extraction instead of re-reading (and re-OCR'ing)."""
    source = source_name or path.name
    if parts is None and path.suffix.lower() == ".md":
        text = _read_text_file(path)
        if re.search(r"(?m)^#\s*Lender:", text) and re.search(r"(?m)^##\s+", text):
            docs = _split_markdown(text, source)
            if docs:
                return docs
    parts = extract(path) if parts is None else parts
    if not parts:
        raise ValueError("The file has no readable text.")
    meta = {"lender": lender or "Unknown", "program": "", "loan_type": loan_type or "Unknown",
            **{k: v for k, v in (extra_meta or {}).items() if v is not None}}
    docs = chunk_parts(parts, source, meta, lead=f"Lender: {lender}" if lender else "")
    for i, d in enumerate(docs):
        d.metadata["chunk_index"] = i
    return docs


def load_directory(directory: Path) -> List[Document]:
    chunks: List[Document] = []
    for p in sorted(directory.glob("*")):
        if p.suffix.lower() in ALLOWED_EXTENSIONS:
            chunks.extend(load_and_chunk(p))
    return chunks
