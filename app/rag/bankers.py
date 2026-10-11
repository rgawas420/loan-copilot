"""Banker / RM matching for a customer profile: the app, not the model, picks who to approach.

The RM lists hold thousands of rows per city (one list per month, plus copies), far more than the model can be
shown, and the table lookup refuses such broad matches. So here the rows are read straight from the keyword row
index (kb_rows) and:

1. filtered: ACTIVE document versions only; the row's city is the customer's city; its loan type is the one
   asked for ("Strong"), or the row names no loan type ("Good");
2. parsed into lender / banker / phone / email / branch, from the labelled columns ("Lender Name: ...");
   rows without a lender, a name and a phone are skipped (nothing is guessed from unlabelled columns);
3. de-duplicated by lender + banker + phone, keeping the most recent list (its effective date, else the month
   in the file name, then the knowledge-base authority priority). The same banker with different phones keeps
   the number from the most recent list; two numbers in equally recent lists are marked "verify";
4. ranked: Strong before Good; then the most recent list; then how many monthly lists the banker appears in
   (a contact kept from January to August is more likely current); then how many RMs the lender has for this
   product in this city; one banker per lender before a second one, so the top 10 cover 10 lenders when the
   lists have them. The rest of the lenders go to "Also check".

Bank-specific eligibility (each lender's own CIBIL / income / FOIR rules) is not in the RM lists and no
structured lender policies are loaded, so it does not change the ranking; the eligibility snapshot is the app's
indicative calculation for the customer.

Steps 1-3 are done once per sync by the banker directory (app/services/banker_directory.py: normalised lender,
city and product names, identical files skipped, indexed tables). Until the directory matches the current
knowledge base, the rows are read and parsed here, as before; both paths are ranked by rank_records().
"""
import json
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document

from app.rag import entities

log = logging.getLogger(__name__)
TOP = 10
ALSO_CHECK = 8
MAX_ROWS_READ = 60000

NOT_FOUND = ("No matching banker was found in the knowledge base for this loan type and location. Try another city, "
             "lender, or loan category, or update the knowledge base with the latest RM list.")

_KEYS = {
    "lender": ("lender name", "lender", "bank name", "bank", "nbfc name", "financier"),
    "name": ("bank/ nbfc rm name", "bank/nbfc rm name", "rm name", "banker name", "banker / rm", "banker", "contact person",
             "sm name", "asm name", "manager name", "name"),
    "phone": ("bank/ nbfc rm contact no", "bank/nbfc rm contact no", "rm contact no", "contact no", "contact number",
              "mobile no", "mobile number", "mobile", "phone", "phone no"),
    "email": ("email", "email id", "mail id", "e-mail"),
    "city": ("city*", "city", "location"),
    "loan_type": ("type of loan", "loan type", "product"),
    "branch": ("branch", "branch name", "area"),
    "designation": ("designation", "role"),
}
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _fields(text: str) -> Dict[str, str]:
    """'Lender Name: X | City*: Pune | ...' -> {'lender': 'X', 'city': 'Pune', ...} (labelled columns only)."""
    raw = {}
    for part in text.split("|"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if v and k not in raw:
            raw[k] = v
    out = {}
    for field_, names in _KEYS.items():
        for n in names:
            if raw.get(n):
                out[field_] = raw[n]
                break
    return out


def _recency(path: str, title: str, effective_from: Optional[str]) -> int:
    """yyyymm of the list: its effective date, else the month in its name ('Aug 2026', 'Jun-26'); 0 if unknown."""
    if effective_from and re.match(r"\d{4}-\d{2}", effective_from):
        return int(effective_from[:4] + effective_from[5:7])
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*[-. ]?\s*'?(20\d{2}|\d{2})\b",
                  f"{title} {path}".lower())
    if not m:
        return 0
    year = int(m.group(2)) + (2000 if len(m.group(2)) == 2 else 0)
    return year * 100 + _MONTHS[m.group(1)]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _digits(s: str) -> str:
    """The comparable phone number: digits only, without +91 or a leading 0."""
    d = re.sub(r"\D", "", s or "")
    if len(d) == 12 and d.startswith("91"):
        return d[2:]
    return d[1:] if len(d) == 11 and d.startswith("0") else d


def _phone_note(phone: str) -> str:
    return "" if len(_digits(phone)) == 10 else "verify number"


@dataclass
class Banker:
    lender: str
    name: str
    phone: str
    match: str                    # Strong | Good
    path: str
    title: str
    recency: int
    priority: int
    email: str = ""
    branch: str = ""
    designation: str = ""
    loan_type: str = ""
    notes: List[str] = field(default_factory=list)
    ref: str = ""                 # [S#] once the passages are numbered
    lists: int = 1                # monthly lists the banker appears in
    presence: int = 1             # the lender's RMs for this product in this city
    periods: frozenset = frozenset()   # yyyymm of every list naming this record (default: its own recency)
    occurrences: int = 1          # rows this record stands for

    def row(self) -> str:
        bits = [f"Lender Name: {self.lender}", f"Banker / RM: {self.name}", f"Contact No: {self.phone}"]
        bits += [f"{k}: {v}" for k, v in (("Designation", self.designation), ("Email", self.email),
                                          ("Branch", self.branch), ("Type of Loan", self.loan_type),
                                          ("Note", "; ".join(self.notes))) if v]
        return " | ".join(bits)


@dataclass
class Match:
    loan_type: str
    city: str
    top: List[Banker] = field(default_factory=list)
    also: List[str] = field(default_factory=list)          # more lenders, names only
    rows_read: int = 0
    duplicates: int = 0
    conflicts: int = 0
    lenders_filter: List[str] = field(default_factory=list)  # only these lenders (a question naming a bank)
    source: str = "row index"                                 # "directory" when read from the banker directory
    total: int = 0                                            # distinct bankers matched (before the top TOP)

    @property
    def lenders(self) -> List[str]:
        return list(dict.fromkeys(b.lender for b in self.top))

    def documents(self) -> List[Document]:
        """One passage per source list, with the chosen bankers' rows (so the answer can cite [S#])."""
        by_path: Dict[str, List[Banker]] = {}
        for b in self.top:
            by_path.setdefault(b.path, []).append(b)
        docs = []
        for i, (path, rows) in enumerate(by_path.items()):
            at = f" at {', '.join(self.lenders_filter)}" if self.lenders_filter else ""
            lines = [f"Source: {path} | Banker match", f"Document: {rows[0].title}",
                     f"BANKER MATCH: {self.loan_type or 'loan'} bankers{at} in {self.city} chosen by the app from this list "
                     f"(de-duplicated, newest list first; {self.total} distinct bankers matched, the best {len(self.top)} "
                     f"shown across all lists)."]
            if self.lenders_filter:
                # "bajaj housing finance" asked, "Bajaj Finserv" listed: without this the model refused, judging the
                # names different, although the app matched them on purpose
                lines.append(f"The lender named in the question was matched to {', '.join(self.lenders_filter)} by the "
                             "app's lender names (group company, short or former name of the same lender): these are "
                             "the bankers for the lender asked about.")
            lines += [b.row() for b in rows]
            docs.append(Document(id=f"bankers-{i}", page_content="\n".join(lines),
                                 metadata={"source": path, "section": "Banker match", "part": "Banker match",
                                           "lookup": True, "corpus": "urban_money"}))
        return docs

    def table(self) -> str:
        if not self.top:
            return NOT_FOUND
        out = ["| # | Bank / Lender | Banker / RM | Phone | Match |", "|---|---|---|---|---|"]
        for i, b in enumerate(self.top, 1):
            note = f" ({', '.join(b.notes)})" if b.notes else ""
            out.append(f"| {i} | {b.lender} | {b.name} {b.ref}".rstrip() + f" | {b.phone}{note} | {b.match} |")
        return "\n".join(out)

    def trace(self) -> dict:
        return {"loan_type": self.loan_type, "city": self.city, "source": self.source,
                "lenders_filter": self.lenders_filter, "rows_read": self.rows_read,
                "duplicates": self.duplicates, "conflicts": self.conflicts, "also_check": self.also,
                "top": [{"lender": b.lender, "name": b.name, "phone": b.phone, "match": b.match, "list": b.title,
                         "recency": b.recency, "lists": b.lists, "presence": b.presence, "notes": b.notes}
                        for b in self.top]}


def _rows(db, expr: str, active: Optional[dict]) -> List[Tuple[str, str]]:
    out = []
    for path, sha, text in db.execute("SELECT r.path, f.sha, r.text FROM kb_rows r JOIN kb_row_files f ON f.path = r.path "
                                      "WHERE kb_rows MATCH ? LIMIT ?", (expr, MAX_ROWS_READ)):
        if active is None or (path in active and active[path] == sha):
            out.append((path, text))
    return out


def find(loan_type: str, city: str, lenders: Optional[List[str]] = None) -> Match:
    """The bankers to approach for this loan type in this city, optionally only at these lenders (canonical
    names, e.g. from entities.find_banks). See the module docstring."""
    from app.services import banker_directory
    from app.services.knowledge_base import _db
    res = Match(loan_type=loan_type, city=city, lenders_filter=list(lenders or []))
    if not city:
        return res
    try:
        with closing(_db()) as db:
            if banker_directory.ready(db):
                recs = banker_directory.records(db, city, loan_type, lenders)
                res.source, res.rows_read = "directory", len(recs)
                return rank_records(res, [_from_directory(r) for r in recs])
    except sqlite3.Error:
        log.exception("Banker directory unreadable; reading the row index")
    return _find_in_rows(res, loan_type, city)


def _from_directory(r: dict) -> "Banker":
    periods = frozenset(int(p) for p in str(r["periods"]).split(",") if p and p != "0")
    return Banker(lender=r["lender"], name=r["name"], phone=r["phone"], match="Strong" if r["product"] else "Good",
                  path=r["source_path"], title=r["source_title"], recency=r["last_seen"], priority=r["priority"],
                  email=r["email"], branch=r["branch"], designation=r["designation"], loan_type=r["raw_product"],
                  periods=periods or frozenset({r["last_seen"]}), occurrences=r["occurrences"])


def _find_in_rows(res: Match, loan_type: str, city: str) -> Match:
    from app.rag import lookup
    from app.services import kb_versions
    from app.services.knowledge_base import _db
    aliases = entities.product_aliases(loan_type) if loan_type else []
    try:
        active = kb_versions.active_versions()
    except Exception:
        active = None
    try:
        authority = kb_versions.authority_map()
    except Exception:
        authority = {}
    try:
        with closing(_db()) as db:
            product = lookup._any(aliases) if aliases else ""
            rows = _rows(db, f"{lookup._phrase(city)} AND {product}" if product else lookup._phrase(city), active)
            if product and not rows:                   # no row names the product: the city's unlabelled RMs
                rows = _rows(db, lookup._phrase(city), active)
            metas = {p: json.loads(m) for p, m in db.execute("SELECT path, meta FROM kb_row_files")}
    except sqlite3.Error:
        log.exception("Banker rows unreadable")
        return res
    return rank(res, rows, metas, authority)


def rank(res: Match, rows: List[Tuple[str, str]], metas: Dict[str, dict], authority: Dict[str, dict]) -> Match:
    """Filter, parse, de-duplicate and rank (path, row text) pairs into res (steps 1-4 of the module docstring)."""
    city_n = _norm(res.city)
    aliases = entities.product_aliases(res.loan_type) if res.loan_type else []
    alias_n = [_norm(a) for a in aliases if len(_norm(a)) > 2] or ([_norm(res.loan_type)] if res.loan_type else [])
    res.rows_read = len(rows)

    records: List[Banker] = []
    for path, text in rows:
        f = _fields(text)
        if not (f.get("lender") and f.get("name") and f.get("phone")):
            continue
        if city_n not in _norm(f.get("city") or ""):
            continue
        lt = _norm(f.get("loan_type") or "")
        if lt and alias_n and not any(a in lt for a in alias_n):
            continue                                            # another product's RM
        meta = metas.get(path, {})
        auth = authority.get(path, {})
        b = Banker(lender=f["lender"], name=f["name"], phone=f["phone"], match="Strong" if lt else "Good",
                   path=path, title=meta.get("title") or path, recency=_recency(path, meta.get("title", ""), auth.get("effective_from")),
                   priority=int(auth.get("priority") or 0), email=f.get("email", ""), branch=f.get("branch", ""),
                   designation=f.get("designation", ""), loan_type=f.get("loan_type", ""))
        records.append(b)
    return rank_records(res, records)


def rank_records(res: Match, records: List[Banker]) -> Match:
    """De-duplicate (one record per lender + banker in this city: the most recent list's) and rank (steps 3-4)."""
    if res.lenders_filter:
        from app.services.banker_directory import canonical_lender, key
        keep = {key(n) for n in res.lenders_filter}
        records = [b for b in records if key(canonical_lender(b.lender)[0]) in keep]
    best: Dict[Tuple[str, str], List[Banker]] = {}           # (lender, banker) -> records, best first
    for b in records:
        best.setdefault((_norm(b.lender), _norm(b.name)), []).append(b)
    chosen: List[Banker] = []
    for recs in best.values():
        recs.sort(key=lambda b: (b.match != "Strong", -b.recency, -b.priority, b.path))
        first = recs[0]
        first.lists = len(set().union(*(r.periods or {r.recency} for r in recs)))    # undated lists count once
        same_phone = [r for r in recs if _digits(r.phone) == _digits(first.phone)]
        res.duplicates += sum(r.occurrences for r in same_phone) - 1
        rival = [r for r in recs if _digits(r.phone) != _digits(first.phone)
                 and (r.recency, r.priority) == (first.recency, first.priority)]
        if rival:
            res.conflicts += 1
            first.notes.append(f"verify: also listed as {rival[0].phone}")
        if note := _phone_note(first.phone):
            first.notes.append(note)
        chosen.append(first)

    presence: Dict[str, int] = {}
    for b in chosen:
        presence[_norm(b.lender)] = presence.get(_norm(b.lender), 0) + 1
    for b in chosen:
        b.presence = presence[_norm(b.lender)]
    chosen.sort(key=lambda b: (b.match != "Strong", -b.recency, -b.lists, -b.presence, -b.priority,
                               _norm(b.lender), _norm(b.name)))
    seen, firsts, seconds = set(), [], []
    for b in chosen:                                           # one banker per lender first
        (seconds if _norm(b.lender) in seen else firsts).append(b)
        seen.add(_norm(b.lender))
    ranked = firsts + seconds
    res.total = len(ranked)
    res.top = ranked[:TOP]
    shown = {_norm(b.lender) for b in res.top}
    res.also = list(dict.fromkeys(b.lender for b in ranked[TOP:] if _norm(b.lender) not in shown))[:ALSO_CHECK]
    return res


def number(match: Match, refs: List[dict]) -> None:
    """Give each chosen banker the [S#] of its source list's passage."""
    by_source = {r.get("source"): r.get("ref") for r in refs if r.get("ref")}
    for b in match.top:
        b.ref = f"[{by_source[b.path]}]" if by_source.get(b.path) else ""
