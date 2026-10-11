"""Review tools for knowledge-base versions: compare two versions, test a version before it is activated, and
search archived versions (admins only).

compare(old_parts, new_parts)
    Tables are compared row by row: rows are matched on their key columns (the first non-numeric columns whose
    values identify a row, e.g. BANK NAME, or BANK + PRODUCT), so the result lists the cells whose values changed
    (Axis Bank · HL: 6159MUM → 6160MUM), and the rows added and removed. Text is compared line by line, and every
    figure (amount, %, code, number) that appears in only one of the versions is listed.

knowledge_tests(doc, new_text)
    The golden tests (RAG quality tab) whose expected document is this document. Before activation the new version
    is not indexed yet, so each test checks that its expected answer is present in the new version's text (and in
    the current one). After activation the full golden run (retrieval + answer) runs automatically.

history_search(query)
    Keyword search over the text of ARCHIVED and DISABLED versions (from the extraction cache). Normal answers
    never see these versions; this is for auditing what an older version said.
"""
import difflib
import re
from typing import Dict, List, Optional, Tuple

from app.services import kb_versions

MAX_ROWS = 200          # changed rows listed per table
MAX_ADDED = 100         # added / removed rows listed per table
MAX_HUNKS = 60          # changed text passages listed
MAX_FIGURES = 80
MAX_TEST_TEXT = 2_000_000


# ------------------------------------------------------------------ helpers
def full_text(parts) -> str:
    out, n = [], 0
    for p in parts or []:
        t = p.text or "\n".join(x for x in [p.context, *p.rows] if x)
        out.append(t)
        n += len(t)
        if n > MAX_TEST_TEXT:
            break
    return "\n".join(out)


def _cells(row: str) -> Dict[str, str]:
    """'BANK NAME: Axis Bank | HL: 6159MUM' -> {'BANK NAME': 'Axis Bank', 'HL': '6159MUM'}."""
    out = {}
    for i, cell in enumerate(row.split(" | ")):
        k, sep, v = cell.partition(": ")
        if not sep:
            k, v = f"Column {i + 1}", cell
        k = k.strip()
        while k in out:
            k += "'"
        out[k] = v.strip()
    return out


_NUMERIC = re.compile(r"^[\s₹%.,/\-+()0-9]*\d[\s₹%.,/\-+()0-9a-z]*$", re.I)


def _key_columns(old: List[dict], new: List[dict]) -> List[str]:
    """The fewest leading non-numeric columns whose values tell the rows apart in both versions."""
    headers = []
    for r in old + new:
        for h in r:
            if h not in headers:
                headers.append(h)
    textual = [h for h in headers
               if sum(1 for r in old + new if r.get(h) and not _NUMERIC.match(r[h])) >= 0.6 * max(1, sum(1 for r in old + new if r.get(h)))]
    for n in range(1, min(3, len(textual)) + 1):
        key = textual[:n]
        if all(len({tuple(r.get(h, "") for h in key) for r in rows}) == len(rows) for rows in (old, new) if rows):
            return key
    return []


def _table_pairs(old_parts, new_parts) -> List[Tuple[str, List[str], List[str]]]:
    """(label, old rows, new rows) for each table, matched by sheet name, then by position."""
    o = [(p.label, p.rows) for p in old_parts if p.rows]
    n = [(p.label, p.rows) for p in new_parts if p.rows]
    pairs, used = [], set()
    for i, (label, rows) in enumerate(n):
        j = next((j for j, (l2, _) in enumerate(o) if l2 == label and j not in used), None)
        if j is not None:
            used.add(j)
            pairs.append((label, o[j][1], rows))
    rest_o = [x for j, x in enumerate(o) if j not in used]
    rest_n = [x for x in n if x[0] not in {p[0] for p in pairs}]
    for (lo, ro), (ln, rn) in zip(rest_o, rest_n):
        pairs.append((ln if ln == lo else f"{lo} → {ln}", ro, rn))
    for lo, ro in rest_o[len(rest_n):]:
        pairs.append((lo, ro, []))
    for ln, rn in rest_n[len(rest_o):]:
        pairs.append((ln, [], rn))
    return pairs


def _compare_table(label: str, old_rows: List[str], new_rows: List[str]) -> dict:
    old, new = [_cells(r) for r in old_rows], [_cells(r) for r in new_rows]
    key = _key_columns(old, new)
    out = {"sheet": label, "key": key, "changed": [], "added": [], "removed": [], "rows_old": len(old), "rows_new": len(new),
           "changed_total": 0, "added_total": 0, "removed_total": 0}
    if key:
        name = lambda r: " · ".join(r.get(h, "") for h in key)
        om = {tuple(r.get(h, "") for h in key): (r, raw) for r, raw in zip(old, old_rows)}
        nm = {tuple(r.get(h, "") for h in key): (r, raw) for r, raw in zip(new, new_rows)}
        for k, (r, raw) in nm.items():
            if k not in om:
                out["added_total"] += 1
                if len(out["added"]) < MAX_ADDED:
                    out["added"].append({"key": name(r), "row": raw})
                continue
            o = om[k][0]
            cells = [{"header": h, "old": o.get(h, ""), "new": r.get(h, "")}
                     for h in dict.fromkeys([*o, *r]) if h not in key and o.get(h, "") != r.get(h, "")]
            if cells:
                out["changed_total"] += 1
                if len(out["changed"]) < MAX_ROWS:
                    out["changed"].append({"key": name(r), "cells": cells})
        for k, (r, raw) in om.items():
            if k not in nm:
                out["removed_total"] += 1
                if len(out["removed"]) < MAX_ADDED:
                    out["removed"].append({"key": name(r), "row": raw})
    else:                                                # no key: whole rows added / removed
        so, sn = set(old_rows), set(new_rows)
        added, removed = [r for r in new_rows if r not in so], [r for r in old_rows if r not in sn]
        out.update(added_total=len(added), removed_total=len(removed),
                   added=[{"key": "", "row": r} for r in added[:MAX_ADDED]],
                   removed=[{"key": "", "row": r} for r in removed[:MAX_ADDED]])
    return out


def _lines(parts) -> List[str]:
    return [ln.strip() for p in parts if p.text for ln in p.text.splitlines() if ln.strip()]


def _figure_set(text: str) -> Dict[str, str]:
    """Normalised figure -> how it is written. Amounts compare by value (₹50 lakh == 50,00,000)."""
    from app.rag.grounding import _CODE, _figures
    amounts, percents, numbers = _figures(text)
    out = {}
    for surface, value, _, _ in amounts:
        out.setdefault(f"amt:{round(value, 2)}", surface)
    for surface, value in percents:
        out.setdefault(f"pct:{round(value, 4)}", surface)
    for raw, digits in numbers:
        out.setdefault(f"num:{digits}", raw)
    for code in _CODE.findall(text):
        if any(ch.isdigit() for ch in code) and any(ch.isalpha() for ch in code):
            out.setdefault(f"code:{code.upper()}", code)
    return out


def compare(old_parts, new_parts) -> dict:
    """What changed between two versions (see the module docstring)."""
    old_parts, new_parts = old_parts or [], new_parts or []
    tables = [_compare_table(*t) for t in _table_pairs(old_parts, new_parts)]
    ol, nl = _lines(old_parts), _lines(new_parts)
    hunks, total_hunks = [], 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ol, nl, autojunk=False).get_opcodes():
        if tag != "equal":
            total_hunks += 1
            if len(hunks) < MAX_HUNKS:
                hunks.append({"old": ol[i1:i2][:12], "new": nl[j1:j2][:12]})
    fo, fn = _figure_set(full_text(old_parts)), _figure_set(full_text(new_parts))
    summary = {"rows_changed": sum(t["changed_total"] for t in tables), "rows_added": sum(t["added_total"] for t in tables),
               "rows_removed": sum(t["removed_total"] for t in tables), "text_changes": total_hunks,
               "figures_added": len(set(fn) - set(fo)), "figures_removed": len(set(fo) - set(fn))}
    summary["identical"] = not any(summary.values())
    return {"summary": summary, "tables": [t for t in tables if t["changed"] or t["added"] or t["removed"]],
            "text": hunks,
            "figures": {"added": [fn[k] for k in fn if k not in fo][:MAX_FIGURES],
                        "removed": [fo[k] for k in fo if k not in fn][:MAX_FIGURES]}}


# ------------------------------------------------------------------ knowledge tests before activation
def _names(doc: dict, extra: List[str]) -> List[str]:
    names = [doc["path"], doc.get("title") or ""]
    names += [v["file"] for v in kb_versions.versions(doc["path"]) if v.get("file")]
    return [n for n in dict.fromkeys(names + extra) if n]


def linked_tests(doc: dict, extra_names: List[str] = ()) -> List[dict]:
    """Active golden tests whose expected document is this document (under any of its file names or titles)."""
    from app.services import rag_debug
    names = _names(doc, list(extra_names))
    return [g for g in rag_debug.golden_list(active_only=True) if g.get("expected_document")
            and any(rag_debug._doc_matches(g["expected_document"], n, n) for n in names)]


def knowledge_tests(doc: dict, new_text: str, old_text: str = "", extra_names: List[str] = ()) -> dict:
    """Each linked test: is its expected answer in the new version (and in the current one)?"""
    from app.services import rag_debug
    results = []
    for g in linked_tests(doc, extra_names):
        exp = (g.get("expected_answer") or "").strip()
        if not exp:
            results.append({"id": g["id"], "question": g["question"], "expected": "", "status": "N/A",
                            "detail": "No expected answer: checked by the full test run after activation"})
            continue
        in_new = rag_debug.answer_matches(exp, new_text)
        in_old = rag_debug.answer_matches(exp, old_text) if old_text else None
        detail = ("Found in the new version" if in_new else
                  "Not in the new version" + (" (it is in the current one: if the value really changed, update the test)"
                                               if in_old else ""))
        results.append({"id": g["id"], "question": g["question"], "expected": exp, "status": "PASS" if in_new else "FAIL",
                        "in_current": in_old, "detail": detail})
    failed = [r for r in results if r["status"] == "FAIL"]
    return {"tests": results, "total": len(results), "failed": len(failed),
            "passed": sum(1 for r in results if r["status"] == "PASS")}


# ------------------------------------------------------------------ historical search (admins)
def history_search(query: str, limit: int = 10) -> List[dict]:
    """Passages of archived / disabled versions that match the query best (keyword scoring)."""
    from contextlib import closing
    from app.rag import rerank
    from app.services.ingestion import chunk_parts, strip_header
    with closing(kb_versions._db()) as db:
        rows = [dict(r) for r in db.execute(
            "SELECT d.path, d.title, v.version, v.status, v.sha, v.file, v.label, v.effective_from, v.effective_until "
            "FROM kb_versions v JOIN kb_docs d ON d.doc_id=v.doc_id WHERE v.status<>? ORDER BY v.created DESC",
            (kb_versions.ACTIVE,))]
    hits = []
    for r in rows:
        parts = kb_versions.cached_parts(r["sha"])
        if not parts:
            continue
        docs = chunk_parts(parts, r["file"] or r["path"], {})
        scores = rerank.lexical_scores(query, [d.page_content for d in docs])
        for d, s in zip(docs, scores):
            if s > 0:
                hits.append({"score": s, "path": r["path"], "file": r["file"] or r["path"], "title": r["title"],
                             "version": r["version"], "status": r["status"], "label": r["label"] or "",
                             "effective_from": r["effective_from"], "effective_until": r["effective_until"],
                             "section": d.metadata.get("section") or "", "text": strip_header(d.page_content)[:700]})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def version_parts(path: str, version: Optional[int] = None):
    """Extracted parts of a version of a document (the ACTIVE one by default), or None."""
    for v in kb_versions.versions(path):
        if (version is None and v["status"] == kb_versions.ACTIVE) or v["version"] == version:
            return kb_versions.cached_parts(v["sha"]), v
    return None, None
