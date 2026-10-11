"""Table lookup: list / count / contact / code / payout questions answered from spreadsheet rows directly.

Passage retrieval sends the model the few best chunks, which cannot answer "RM list in Pune for personal loan"
(the matching rows are spread over many chunks) or "how many records in the RM list" (the model would have to
count rows it never sees). So every table row of the knowledge base is also kept, one row per entry, in a
full-text index (kb_rows). For a question that asks for rows, the filters are taken from the question

    bank (any alias)  AND  product (any alias)  AND  every other meaningful word that occurs in the tables
    ("pune", "delhi", a person's or company's name)

and the matching rows are counted and returned exactly, file by file. The rows go to the model as an extra
passage ([S1]...), with the count computed here, so the model lists rows rather than recalls or counts them.

If nothing matches with the product filter, it is dropped once (many RM lists do not name a product); the bank
and the other words are never dropped. No match means no lookup: the normal passage retrieval still answers.
"""
import json
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from langchain_core.documents import Document

from app.rag import entities

log = logging.getLogger(__name__)
ROW_INDEX_VERSION = "1"            # bump when the row text changes: the next sync rebuilds every file's rows
MAX_FILES = 3                      # files shown
MAX_ROWS = 40                      # rows shown in all
MAX_CHARS = 9000                   # characters of rows shown in all
MAX_TOTAL = 500                    # a non-count question matching more rows than this is too broad to list

# Asks for rows: people / contacts, lists, codes, pincodes, payout grids, counts.
_INTENT = re.compile(r"\b(rms?|sms?|asms?|bms?|managers?|banker|contacts?|phones?|mobiles?|numbers?|emails?|lists?|"
                     r"codes?|dsa|pin ?codes?|serviceab\w*|payouts?|slabs?|grid|how many|count|records?|entries|"
                     r"rows?|who is)\b", re.I)
_COUNT = re.compile(r"\bhow many\b|\bcount\b|\bnumber of\b|\btotal (no|number|count)\b", re.I)
# Words that describe the request, not the rows wanted.
_NOISE = set("""
a an the of in on at for to from by with and or any all me my our we you your i is are was be it its this that
these those what which who whom whose where when how why many much do does did can could would should will
please kindly pls plz give provide share send show tell find get need want list lists details detail detailed
rm rms sm sms asm asms bm bms manager managers relationship sales banker bankers contact contacts number numbers
phone phones mobile mobiles email emails name names code codes dsa pin pincode pincodes payout payouts slab slabs
grid count records record entries entry rows row total no location locations city area region branch branches
format formate below above table bank banks loan loans lender lenders product products related releted releletd
regarding about available current latest new person people team who whom data sheet file
""".split())


@dataclass
class Lookup:
    question: str
    filters: Dict[str, List[str]]
    relaxed: List[str]
    total: int
    count_question: bool
    file_count: int = 0                                 # files with a matching row
    files: List[dict] = field(default_factory=list)   # {path, title, meta, matched, rows} (the top MAX_FILES)

    def documents(self) -> List[Document]:
        """One passage per file: what was filtered, the exact count, and the rows."""
        docs = []
        about = "; ".join(f"{k}: {', '.join(v)}" for k, v in self.filters.items() if v) or "the question's words"
        for i, f in enumerate(self.files):
            shown = len(f["rows"])
            lines = [f"Source: {f['path']} | Table lookup", f"Document: {f['title']}",
                     f"TABLE LOOKUP: exact rows from this spreadsheet matching {about}."
                     + (f" (No row names the {', '.join(self.relaxed)}, so that filter was not applied.)" if self.relaxed else ""),
                     f"Matching rows in this file: {f['matched']}"
                     + (" (all shown)." if shown == f["matched"] else f" (only the first {shown} are shown here).")]
            if i == 0 and self.file_count > 1:
                lines.append(f"Matching rows across the knowledge base: {self.total} in {self.file_count} files"
                             + (f" (the {len(self.files)} files with most matches are shown)." if self.file_count > len(self.files) else "."))
            lines += f["rows"]
            docs.append(Document(id=f"lookup-{i}", page_content="\n".join(lines),
                                 metadata={**f["meta"], "source": f["path"], "section": "Table lookup",
                                           "part": "Table lookup", "lookup": True}))
        return docs

    @property
    def paths(self) -> set:
        return {f["path"] for f in self.files}

    def trace(self) -> dict:
        return {"filters": self.filters, "relaxed": self.relaxed, "total": self.total,
                "count_question": self.count_question, "file_count": self.file_count,
                "files": [{"path": f["path"], "matched": f["matched"], "shown": len(f["rows"])} for f in self.files]}


# ------------------------------------------------------------------ index (called by the knowledge-base sync)
SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_rows USING fts5(path UNINDEXED, part UNINDEXED, doc, text,
    tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS kb_row_files(path TEXT PRIMARY KEY, sha TEXT, meta TEXT, rows INTEGER);
"""


def index_rows(db, rel: str, parts, meta: dict) -> int:
    """Replace one file's rows. Only table parts have rows (spreadsheets, CSV); other files get none."""
    remove_rows(db, rel)
    doc_line = " | ".join(x for x in (meta.get("title"), meta.get("bank"), meta.get("product"), meta.get("category")) if x)
    rows = [(rel, p.label, " | ".join(x for x in (doc_line, p.label, p.context) if x), r)
            for p in parts for r in p.rows if r.strip()]
    if not rows:
        return 0
    db.executemany("INSERT INTO kb_rows(path, part, doc, text) VALUES (?,?,?,?)", rows)
    db.execute("INSERT OR REPLACE INTO kb_row_files VALUES (?,?,?,?)",
               (rel, meta.get("sha") or "", json.dumps(meta), len(rows)))
    return len(rows)


def remove_rows(db, rel: str):
    db.execute("DELETE FROM kb_rows WHERE path=?", (rel,))
    db.execute("DELETE FROM kb_row_files WHERE path=?", (rel,))


def indexed_paths(db) -> set:
    return {r[0] for r in db.execute("SELECT path FROM kb_row_files")}


# ------------------------------------------------------------------ query
def _phrase(s: str) -> str:
    return '"' + s.replace('"', "") + '"'


def _any(aliases) -> str:
    return "(" + " OR ".join(_phrase(a) for a in dict.fromkeys(aliases)) + ")"


def filter_words(question: str, banks: List[str], products: List[str]) -> List[str]:
    """Words of the question that should narrow the rows: not the bank / product names (filtered separately)
    and not the words that describe the request ("give me the RM list")."""
    t = entities._norm(question)
    for alias in [a for b in banks for a in entities.bank_aliases(b)] + [a for p in products for a in entities.product_aliases(p)]:
        t = entities._alias_re(alias).sub(" ", t)
    words = [w for w in re.findall(r"[a-z0-9]+", t) if w not in _NOISE and (len(w) >= 3 or w.isdigit())]
    return list(dict.fromkeys(words))[:6]


def _exists(db, word: str) -> bool:
    try:
        return db.execute("SELECT 1 FROM kb_rows WHERE kb_rows MATCH ? LIMIT 1", (_phrase(word),)).fetchone() is not None
    except sqlite3.Error:
        return False


def _counts(db, expr: str, active: Optional[dict]) -> Dict[str, int]:
    """Matching rows per active file. A file identical to another (the same list saved in two folders) is
    counted once, under the first path, so its rows are neither shown nor counted twice."""
    rows = db.execute("SELECT r.path, f.sha, count(*) FROM kb_rows r JOIN kb_row_files f ON f.path = r.path "
                      "WHERE kb_rows MATCH ? GROUP BY r.path ORDER BY r.path", (expr,)).fetchall()
    out, shas = {}, set()
    for p, sha, n in rows:
        if active is not None and not (p in active and active[p] == sha):
            continue
        if sha and sha in shas:
            continue
        shas.add(sha)
        out[p] = n
    return out


def _newest_first(db, counts: Dict[str, int]) -> List[str]:
    """Files to show: the most recent list first (its effective date, else the month in its name), then the
    document's authority priority, then the number of matching rows. Monthly lists repeat the same rows, so
    ordering by matches alone showed the biggest, often oldest, list."""
    from app.rag.bankers import _recency
    try:
        from app.services import kb_versions
        authority = kb_versions.authority_map()
    except Exception:
        authority = {}
    order = []
    for path in counts:
        r = db.execute("SELECT meta FROM kb_row_files WHERE path=?", (path,)).fetchone()
        title = json.loads(r[0]).get("title", "") if r else ""
        a = authority.get(path, {})
        order.append((-_recency(path, title, a.get("effective_from")), -int(a.get("priority") or 0), -counts[path], path))
    return [o[-1] for o in sorted(order)]


# The document types that hold the asked-for fact: a code question is answered from the code books, not from the
# RM lists or payout sheets that also name the bank (they would crowd the code book out, or exceed MAX_TOTAL).
_TYPES_FOR_METRIC = {"code": {"Codes"}, "payout": {"Payout"}, "pincode": {"Pincode list"}}


def _wanted_types(question: str) -> set:
    metrics = entities.find_metrics(question)
    if "code" in metrics and "payout" in metrics:
        return set()                                        # a payout sheet lists codes too: no preference
    return set().union(*(_TYPES_FOR_METRIC.get(m, set()) for m in metrics)) if metrics else set()


def _of_type(counts: Dict[str, int], wanted: set, strict: bool = False) -> Dict[str, int]:
    """Only the files of the wanted document types; when none match, every file (strict: nothing)."""
    if not wanted or not counts:
        return counts
    try:
        from app.services import kb_versions
        amap = kb_versions.authority_map()
    except Exception:
        return {} if strict else counts
    typed = {p: n for p, n in counts.items() if (amap.get(p) or {}).get("doc_type") in wanted}
    return typed if typed or strict else counts


def _older_editions(paths: List[str]) -> Dict[str, str]:
    try:
        from app.services import kb_versions
        return kb_versions.older_editions(paths)
    except Exception:
        log.exception("Could not compare document editions")
        return {}


def find(question: str, banks: List[str], products: List[str]) -> Optional[Lookup]:
    """The rows that answer the question, or None when it does not ask for rows or nothing matches."""
    from app.services import kb_versions
    from app.services.knowledge_base import _db
    count_q = bool(_COUNT.search(question))
    intent = count_q or bool(_INTENT.search(question))
    try:
        with closing(_db()) as db:
            if not db.execute("SELECT 1 FROM kb_row_files LIMIT 1").fetchone():
                return None
            words = filter_words(question, banks, products)
            unknown = [w for w in words if not _exists(db, w)]   # typos, filler: not in any table
            words = [w for w in words if w not in unknown]
            # A bare name ("Sachin Agarwal") is a row lookup too, when every word is found in the tables.
            if not intent and not (words and not unknown and len(re.findall(r"\w+", question)) <= 4):
                return None
            bank = _any(a for b in banks for a in entities.bank_aliases(b)) if banks else ""
            product = _any(a for p in products for a in entities.product_aliases(p)) if products else ""
            if not (bank or product or words):
                return None
            try:
                active = kb_versions.active_versions()
            except Exception:
                active = None
            attempts = [(product, [])] + ([("", ["product"])] if product else [])
            wanted = _wanted_types(question)
            # A code / payout / pincode question: first only the files of that type (for a code, rows with a code
            # column first), dropping the product filter there before looking at any other file.
            typed = [(prod, rel, extra) for extra in ([["code"], []] if "code" in entities.find_metrics(question) else [[]])
                     for prod, rel in attempts] if wanted else []
            counts, expr, relaxed = {}, "", []
            for prod, relaxed, extra in typed:
                # the extra term must be in the row itself, not in the file title every row carries ("CODES AND ...")
                expr = " AND ".join(x for x in [bank, prod, *(_phrase(w) for w in words),
                                                *(f"text : {_phrase(w)}" for w in extra)] if x)
                counts = _of_type(_counts(db, expr, active), wanted, strict=True)
                if counts:
                    break
            if not counts:
                for prod, relaxed in attempts:
                    expr = " AND ".join(x for x in [bank, prod, *(_phrase(w) for w in words)] if x)
                    counts = _counts(db, expr, active)
                    if counts:
                        break
            total = sum(counts.values())
            if not counts or (total > MAX_TOTAL and not count_q):
                return None
            filters = {"bank": banks, "product": products if not relaxed else [], "words": words}
            look = Lookup(question=question, filters=filters, relaxed=relaxed, total=total, count_question=count_q,
                          file_count=len(counts))
            budget_rows, budget_chars = MAX_ROWS, MAX_CHARS
            shown = _newest_first(db, counts)[:MAX_FILES]
            older = _older_editions(shown)
            for path in shown:
                meta = json.loads(db.execute("SELECT meta FROM kb_row_files WHERE path=?", (path,)).fetchone()[0])
                if older.get(path):
                    meta = {**meta, "newer_edition": older[path]}
                rows = []
                for part, text in db.execute("SELECT part, text FROM kb_rows WHERE kb_rows MATCH ? AND path=? "
                                             "ORDER BY rowid LIMIT ?", (expr, path, budget_rows)):
                    line = f"{part}: {text}" if part and part.startswith("Sheet") else text
                    if len(line) > budget_chars:
                        break
                    rows.append(line)
                    budget_chars -= len(line) + 1
                budget_rows -= len(rows)
                look.files.append({"path": path, "title": meta.get("title") or path, "meta": meta,
                                   "matched": counts[path], "rows": rows})
                if budget_rows <= 0 or budget_chars <= 200:
                    break
            return look
    except sqlite3.Error:
        log.exception("Table lookup failed")
        return None
