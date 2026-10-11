"""Verified text for image-based documents (images, scanned PDF pages read by OCR).

OCR can misread rates, amounts, PIN codes and codes. An admin checks the OCR text against the page images, corrects
it (or types / pastes it, or starts from an independent second reading) and saves it. The verified text is stored
per file content (the SHA-256 of the bytes), so it applies wherever that content is read:

    image ─> OCR text ─> checks (ocr_review) ─> admin ticks every finding, corrects ─> verified text
          ─> chunks, embeddings, keyword and row index, previews

knowledge_base._extract_cached() applies the verified text to the extraction, so a sync re-indexes the document
from it; the original file stays the document's file (download, rollback). The same works for an upload that is
still waiting for review. Removing the verified text goes back to the OCR output.

Scope: for an image, or a PDF whose every page was scanned, the verified text replaces the whole extraction
("all"). For a PDF with real text pages too, only the OCR'd pages are edited and replaced ("pages"): its text
pages and tables keep their machine extraction.

Several pages are edited as one text with "=== Page 3 ===" lines between them, so page numbers are kept. Markdown
tables in the verified text (as the second reading writes them) become table rows, like a spreadsheet's.

Saving is refused while the checks are running, while any of their findings is not ticked, when the text still has
[unreadable] markers, when it has suspicious figures that were not accepted, and when someone else saved a
verification after this one was opened. Every earlier verified text is kept (kb_verified_history).
"""
import json
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.services.ingestion import IMAGE_EXTENSIONS, Part, table_part

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_verified(sha TEXT PRIMARY KEY, sections TEXT, by TEXT, at TEXT, changes TEXT);
CREATE TABLE IF NOT EXISTS kb_verified_history(sha TEXT, sections TEXT, by TEXT, at TEXT, changes TEXT, scope TEXT,
    checked TEXT, replaced_at TEXT, replaced_by TEXT, action TEXT);
CREATE INDEX IF NOT EXISTS kb_verified_history_sha ON kb_verified_history(sha);
"""
COLUMNS = {"scope": "TEXT", "checked": "TEXT"}           # added after the first release (knowledge_base._migrate)
MAX_CHARS = 2_000_000
_HEAD = re.compile(r"^=== (.{1,80}?) ===[ \t]*$", re.M)
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
TEXT, OCR, VERIFIED = "text", "ocr", "verified"
ALL, PAGES = "all", "pages"


class VerifyError(Exception):
    def __init__(self, message: str, code: int = 400, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.detail = {"message": message, **detail} if detail else None


def _db():
    from app.services.knowledge_base import _db
    return _db()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ text <-> sections
def _body(p: Part) -> str:
    return p.text or "\n".join(x for x in [p.context, *p.rows] if x)


def to_text(parts: List[Part], headers: bool = False) -> str:
    """The text an admin edits: one section per part, headed '=== label ===' when there are several (or always:
    headers=True, so that the page number of a single scanned page of a PDF is kept)."""
    parts = [p for p in parts or [] if _body(p).strip()]
    if len(parts) == 1 and not headers:
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


def _cells(line: str) -> List[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def section_parts(s: dict) -> List[Part]:
    """One verified section as parts: Markdown tables become table rows, the rest stays text."""
    out, buf = [], []
    lines = s["text"].splitlines()

    def flush():
        body = "\n".join(buf).strip()
        if body:
            out.append(Part(s["label"], text=body, page=s["page"], verified=True))
        buf.clear()

    i = 0
    while i < len(lines):
        if (lines[i].strip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip())):
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            tp = table_part(s["label"], [_cells(lines[i])] + [_cells(x) for x in lines[i + 2:j]])
            if tp:
                flush()
                tp.page, tp.verified = s["page"], True
                out.append(tp)
                i = j
                continue
        buf.append(lines[i])
        i += 1
    flush()
    return out


# ------------------------------------------------------------------ store
def get(sha: str) -> Optional[dict]:
    with closing(_db()) as db:
        r = db.execute("SELECT * FROM kb_verified WHERE sha=?", (sha,)).fetchone()
    if r is None:
        return None
    return {**dict(r), "sections": json.loads(r["sections"]), "changes": json.loads(r["changes"] or "{}"),
            "scope": r["scope"] or ALL, "checked": json.loads(r["checked"] or "{}")}


def _parts(v: dict) -> List[Part]:
    return [p for s in v["sections"] for p in section_parts(s)]


_UNSET = object()


def apply(sha: str, machine: Optional[List[Part]], v=_UNSET) -> Optional[List[Part]]:
    """The text the knowledge base uses for this content: the machine extraction with the verified text applied.
    v: get(sha) when the caller already has it."""
    v = get(sha) if v is _UNSET else v
    if not v:
        return machine
    verified = _parts(v)
    if v["scope"] != PAGES or machine is None:
        return verified
    merged = [p for p in machine if not p.ocr] + verified
    return sorted(merged, key=lambda p: p.page or 0)              # stable: a page's own order is kept


def needs_machine(v: Optional[dict]) -> bool:
    """Whether apply() needs the machine extraction (no verified text, or only its OCR'd pages are verified)."""
    return v is None or v["scope"] == PAGES


def parts_for(sha: str) -> Optional[List[Part]]:
    """The verified text of this content as extraction parts, or None."""
    v = get(sha)
    return _parts(v) if v else None


def machine_parts(sha: str) -> Optional[List[Part]]:
    """The machine extraction (text layer and OCR) of this content, ignoring any verified text."""
    from app.services import kb_versions
    return kb_versions.cached_parts(sha, verified=False)


ocr_parts = machine_parts                                   # earlier name


def _mixed(machine) -> bool:
    """A PDF with real text (or tables) besides OCR'd pages."""
    return any(p.ocr for p in machine or []) and any(not p.ocr and _body(p).strip() for p in machine or [])


def _scope(sha: str, machine) -> str:
    v = get(sha)
    if v and v["scope"] == ALL and _mixed(machine):
        return ALL                                          # verified before page scopes existed: keep it whole
    return PAGES if _mixed(machine) else ALL


def editable_ocr(machine, scope: str) -> List[Part]:
    return [p for p in machine or [] if p.ocr] if scope == PAGES else list(machine or [])


def figure_changes(before: str, after: str) -> dict:
    """Figures (rates, amounts, codes, PIN codes) that the correction removed or added."""
    from app.services.kb_review import _figure_set
    b, a = _figure_set(before), _figure_set(after)
    return {"removed": [b[k] for k in b if k not in a][:100], "added": [a[k] for k in a if k not in b][:100]}


def _archive(db, sha: str, by: str, action: str):
    db.execute("INSERT INTO kb_verified_history(sha, sections, by, at, changes, scope, checked, replaced_at, "
               "replaced_by, action) SELECT sha, sections, by, at, changes, scope, checked, ?, ?, ? FROM kb_verified "
               "WHERE sha=?", (_now(), by, action, sha))


def save(sha: str, text: str, by: str = "", *, checked: Optional[List[str]] = None, base_at=False,
         accept_warnings: bool = True) -> dict:
    """Save an admin's verified text. checked: ids of the ocr_review items the admin ticked (all are required when
    the review is ready). base_at: the verified-at time the admin's screen loaded (None = not verified then);
    False = do not check. accept_warnings: save even with suspicious figures in the text."""
    from app.services import ocr_review
    text = (text or "").replace("\r\n", "\n")
    if len(text) > MAX_CHARS:
        raise VerifyError("The text is too long.")
    sections = parse(text)
    if not sections:
        raise VerifyError("Enter the text of the document.")
    current = get(sha)
    if base_at is not False and (current["at"] if current else None) != base_at:
        raise VerifyError(f"{current['by'] if current else 'Someone'} changed the verified text of this file "
                          f"{'at ' + current['at'] if current else 'after you opened it'}. Reload to see it.", 409,
                          {"code": "conflict"})
    review = ocr_review.get(sha)
    if review and review["status"] == ocr_review.RUNNING:
        raise VerifyError("The checks of this file are still running. Wait for them to finish.", 409,
                          {"code": "review_running"})
    required = ocr_review.required_items(review)
    missing = [it for it in required if it["id"] not in set(checked or [])]
    if missing:
        raise VerifyError(f"{len(missing)} of {len(required)} flagged items are not checked yet.", 409,
                          {"code": "unchecked", "items": [it["id"] for it in missing]})
    problems = ocr_review.lint(text)
    if any(p.get("blocking") for p in problems):
        raise VerifyError(next(p["detail"] for p in problems if p.get("blocking")).capitalize() + ".", 400,
                          {"code": "unreadable", "warnings": problems})
    if problems and not accept_warnings:
        raise VerifyError(f"{len(problems)} figure(s) in the text look misread. Check them, or save anyway.", 422,
                          {"code": "warnings", "warnings": problems})

    machine = machine_parts(sha) or []
    scope = _scope(sha, machine)
    if scope == PAGES and any(s["page"] is None for s in sections):
        raise VerifyError("Keep the “=== Page N ===” line above each page's text: only the scanned pages of this PDF "
                          "are replaced, by page number.", 400, {"code": "pages"})
    changes = figure_changes(to_text(editable_ocr(machine, scope), headers=scope == PAGES), text)
    record = {"items": len(required), "checked": len(required), "warnings_accepted": [p["text"] for p in problems],
              "reader": (review or {}).get("data", {}).get("reader") if review and review.get("data") else None,
              "review": review["status"] if review else None}
    with closing(_db()) as db:
        _archive(db, sha, by, "replaced")
        db.execute("INSERT OR REPLACE INTO kb_verified(sha, sections, by, at, changes, scope, checked) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (sha, json.dumps(sections, ensure_ascii=False), by, _now(), json.dumps(changes, ensure_ascii=False),
                    scope, json.dumps(record, ensure_ascii=False)))
        db.commit()
    return {"sections": len(sections), "changes": changes, "scope": scope, "checked": record}


def remove(sha: str, by: str = "") -> bool:
    with closing(_db()) as db:
        _archive(db, sha, by, "removed")
        n = db.execute("DELETE FROM kb_verified WHERE sha=?", (sha,)).rowcount
        db.commit()
    return bool(n)


def history(sha: str) -> List[dict]:
    with closing(_db()) as db:
        rows = db.execute("SELECT by, at, changes, replaced_at, replaced_by, action FROM kb_verified_history "
                          "WHERE sha=? ORDER BY replaced_at DESC, rowid DESC LIMIT 50", (sha,)).fetchall()
    return [{**dict(r), "changes": json.loads(r["changes"] or "{}")} for r in rows]


# ------------------------------------------------------------------ kinds
def kind(parts: List[Part], suffix: str = "") -> str:
    """'verified' (an admin's text), 'ocr' (image or scanned pages, not verified) or 'text'."""
    if any(p.verified for p in parts or []):
        return VERIFIED
    if suffix.lower() in IMAGE_EXTENSIONS or any(p.ocr for p in parts or []):
        return OCR
    return TEXT


def _review_view(review: Optional[dict]) -> dict:
    if review is None:
        return {"status": "OFF", "pages": [], "items": []}
    data = review.get("data") or {}
    pages = [{k: p[k] for k in ("page", "label", "width", "height", "boxes", "second", "second_error", "error")}
             for p in data.get("pages", [])]
    return {"status": review["status"], "error": review.get("error"), "at": review.get("at"),
            "reader": data.get("reader"), "low_confidence": data.get("low_confidence"), "pages": pages,
            "items": [it for p in data.get("pages", []) for it in p["items"]]}


def editor(sha: str, name: str, path: Optional[Path] = None) -> dict:
    """What the verification screen shows: the text to edit (verified if saved, else OCR), the checks to tick
    (starting them when needed), the OCR's figures, who verified it and the earlier verified texts."""
    from app.services import ocr_review
    from app.services.kb_review import _figure_set
    machine = machine_parts(sha) or []
    scope = _scope(sha, machine)
    ocr_text = to_text(editable_ocr(machine, scope), headers=scope == PAGES)
    v = get(sha)
    review = ocr_review.start(sha, path) if path is not None else ocr_review.get(sha)
    return {"name": name, "image": Path(name).suffix.lower() in IMAGE_EXTENSIONS, "pdf": name.lower().endswith(".pdf"),
            "scope": scope,
            "text": "\n\n".join((f"=== {s['label']} ===\n" if len(v["sections"]) > 1 or scope == PAGES else "") + s["text"]
                                for s in v["sections"]) if v else ocr_text,
            "ocr_text": ocr_text, "ocr_available": bool(ocr_text.strip()),
            "figures": list(_figure_set(ocr_text).values())[:200],
            "verified": {"by": v["by"], "at": v["at"], "changes": v["changes"], "checked": v["checked"]} if v else None,
            "review": _review_view(review), "history": history(sha)}
