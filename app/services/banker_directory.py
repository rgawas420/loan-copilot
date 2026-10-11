"""Banker / RM directory: the RM lists normalised into tables, rebuilt from the row index after every sync.

The RM lists are monthly spreadsheets that repeat the same people (in October 2026, 371k rows held 158k distinct
lender + banker + phone records, and byte-identical copies of the lists sat in two folders). Parsing and
de-duplicating them on every question is slow and lets copies and old lists through, so they are parsed once:

    dir_lenders    one row per lender. The canonical name from entities.BANKS when the raw name is that bank and
                   nothing else ("HDFC LTD", "HDFC BANK" -> HDFC Bank; "HDFC Credila ..." stays as written)
    dir_bankers    one row per lender + banker + phone: the name without honorifics or an embedded number, the
                   phone as 10 digits, and whether it is a valid Indian mobile
    dir_coverage   where a banker works: city + product (both canonical; product '' = the list names none), the
                   newest list naming them there (last_seen, yyyymm), every month listed, how many rows repeated it,
                   and the file it came from
    dir_issues     what was left out or needs checking, and why (see ISSUES)

Only ACTIVE document versions are read, and a file identical to one already read is skipped. The build is a full
rebuild in one transaction, run only when the set of active row files changed (dir_meta 'fingerprint'). Until it
has run, bankers.find() reads the row index directly, so an out-of-date directory is never used.
"""
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple

from app.rag import entities

log = logging.getLogger(__name__)
DIRECTORY_VERSION = "2"            # bump when parsing / normalisation changes: the next sync rebuilds
_lock = threading.Lock()
_cities: Dict[str, List[str]] = {}  # fingerprint -> city keys (cached between questions)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dir_meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS dir_lenders(lender_id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
    canonical INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dir_bankers(banker_id INTEGER PRIMARY KEY,
    lender_id INTEGER NOT NULL REFERENCES dir_lenders(lender_id) ON DELETE CASCADE,
    name TEXT NOT NULL, name_key TEXT NOT NULL, phone TEXT NOT NULL, phone_valid INTEGER NOT NULL,
    email TEXT NOT NULL DEFAULT '', designation TEXT NOT NULL DEFAULT '', UNIQUE(lender_id, name_key, phone));
CREATE INDEX IF NOT EXISTS dir_bankers_phone ON dir_bankers(phone);
CREATE TABLE IF NOT EXISTS dir_coverage(banker_id INTEGER NOT NULL REFERENCES dir_bankers(banker_id) ON DELETE CASCADE,
    city TEXT NOT NULL, city_key TEXT NOT NULL, product TEXT NOT NULL, raw_product TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '', last_seen INTEGER NOT NULL, first_seen INTEGER NOT NULL, periods TEXT NOT NULL,
    occurrences INTEGER NOT NULL, priority INTEGER NOT NULL, source_path TEXT NOT NULL, source_title TEXT NOT NULL,
    PRIMARY KEY(banker_id, city_key, product));
CREATE INDEX IF NOT EXISTS dir_coverage_lookup ON dir_coverage(city_key, product, last_seen DESC);
CREATE TABLE IF NOT EXISTS dir_issues(id INTEGER PRIMARY KEY, issue TEXT NOT NULL, source_path TEXT NOT NULL,
    detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dir_issues_kind ON dir_issues(issue);
"""

ISSUES = {
    "duplicate_file": "identical to another file (skipped)",
    "no_labelled_columns": "contact file without lender / name / phone columns (not used)",
    "missing_lender": "row without a lender name (not used)",
    "missing_phone": "banker row without a phone number (not used)",
    "missing_city": "row without a city (not used)",
    "invalid_phone": "number is not a valid 10-digit Indian mobile (used, marked 'verify number')",
    "phone_in_name": "phone number written inside the name (cleaned)",
    "conflicting_phone": "same banker listed with different numbers in equally recent lists",
}
SKIP_CATEGORIES = {"Company list", "Pincode list"}         # large tables that never hold bankers
MAX_ISSUE_ROWS = 2000                                     # per kind; the totals are always counted

# Spellings that are the same city. Anything else is matched as the row index did: the asked-for city contained
# in the row's city ("Delhi" also finds "New Delhi", "Mumbai" also finds "Navi Mumbai").
CITY_ALIASES = {"bengaluru": "Bangalore", "banglore": "Bangalore", "bangaluru": "Bangalore", "gurugram": "Gurgaon",
                "bombay": "Mumbai", "poona": "Pune", "calcutta": "Kolkata", "madras": "Chennai",
                "trivandrum": "Thiruvananthapuram", "baroda": "Vadodara", "mysuru": "Mysore", "vizag": "Visakhapatnam"}
ADMIN_CITY_ALIASES: Dict[str, str] = {}   # alias (lower case) -> city, added by admins (app/services/aliases.py)
# Raw loan types the entity table does not read ("HLUC", "WC", "Auto Loan" ...).
PRODUCT_EXTRA = [(re.compile(r"\bhluc\b|\bhl\b|home|housing", re.I), "Home Loan"),
                 (re.compile(r"\blap\b|against property|mortgage", re.I), "LAP"),
                 (re.compile(r"\bwc\b|working capital|cash credit", re.I), "Working Capital"),
                 (re.compile(r"\bcar\b|\bauto\b", re.I), "Car Loan"),
                 (re.compile(r"\bdoctor|professional", re.I), "Professional Loan")]
_HONORIFIC = re.compile(r"^\s*(mr|mrs|ms|miss|dr|shri|smt|sri)\.?\s+", re.I)
_PHONE_RUN = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}")
_CORPORATE = re.compile(r"\b(the|ltd|limited|pvt|private|bank|banks|finance|financial|financiers?|services?|company|"
                        r"co|corp|corporation|india|housing|home|capital|nbfc|hfc|sfb|small|of)\b")


# ------------------------------------------------------------------ normalisation
def key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def digits(s: str) -> str:
    """The comparable phone number: digits only, without +91 or a leading 0."""
    d = re.sub(r"\D", "", s or "")
    if len(d) == 12 and d.startswith("91"):
        return d[2:]
    return d[1:] if len(d) == 11 and d.startswith("0") else d


def valid_mobile(phone: str) -> bool:
    return bool(re.fullmatch(r"[6-9]\d{9}", digits(phone)))


def clean_name(raw: str) -> Tuple[str, str]:
    """(name, phone found inside it): 'MR Jagdish Mali - 9352562316' -> ('Jagdish Mali', '9352562316')."""
    s = raw or ""
    m = _PHONE_RUN.search(s)
    phone = digits(m.group(0)) if m else ""
    if m:
        s = s[:m.start()] + s[m.end():]
    s = _HONORIFIC.sub("", s)
    s = re.sub(r"\s*[-–:/|,]+\s*$", "", re.sub(r"^\s*[-–:/|,]+\s*", "", s))
    return " ".join(s.split()), phone


def name_key(name: str) -> str:
    return key(_HONORIFIC.sub("", clean_name(name)[0]))


@lru_cache(maxsize=8192)
def canonical_lender(raw: str) -> Tuple[str, bool]:
    """(display name, True when it is a known bank). A raw name is mapped only when nothing but that bank's alias
    and corporate words remain, so 'HDFC Credila Financial Services' is not filed under HDFC Bank."""
    raw = " ".join((raw or "").split())
    found = entities.find_banks(raw)
    if len(found) == 1:
        rest = entities._norm(raw)
        for alias in sorted(entities.bank_aliases(found[0]), key=len, reverse=True):
            rest = entities._alias_re(alias).sub(" ", rest)
        if not re.sub(r"[^a-z0-9]+", "", _CORPORATE.sub(" ", rest)):
            return found[0], True
    return raw, False


@lru_cache(maxsize=8192)
def canonical_product(raw: str) -> str:
    """'Top Up-HL', 'BT-HL', 'HLUC', 'Affordable - HL' -> 'Home Loan'; '' when the list names no product. A loan
    type nothing recognises is kept as written (it then matches only itself)."""
    raw = " ".join((raw or "").split())
    if not raw:
        return ""
    found = entities.find_products(raw)
    if found:
        return found[0]
    for pat, product in PRODUCT_EXTRA:
        if pat.search(raw):
            return product
    return raw


@lru_cache(maxsize=8192)
def canonical_city(raw: str) -> str:
    raw = " ".join((raw or "").replace("*", " ").split()).strip(" ,.-")
    if not raw:
        return ""
    alias = ADMIN_CITY_ALIASES.get(raw.lower()) or CITY_ALIASES.get(raw.lower())
    if alias:
        return alias
    return raw.title() if raw.isupper() or raw.islower() else raw


# ------------------------------------------------------------------ build
def ensure_schema(db):
    db.executescript(SCHEMA)


def _active_row_files(db, active: Optional[dict]) -> List[Tuple[str, str, dict]]:
    out = []
    for path, sha, meta in db.execute("SELECT path, sha, meta FROM kb_row_files ORDER BY path"):
        if active is not None and active.get(path) != sha:
            continue
        out.append((path, sha, json.loads(meta or "{}")))
    return out


def fingerprint(files: Iterable[Tuple[str, str, dict]]) -> str:
    from app.rag import lookup
    from app.rag import entities
    aliases = json.dumps([entities._ADMIN, ADMIN_CITY_ALIASES], sort_keys=True)     # a new alias changes the parse
    h = hashlib.sha256(f"{DIRECTORY_VERSION}|{lookup.ROW_INDEX_VERSION}|{aliases}".encode())
    for path, sha, _ in sorted(files, key=lambda f: f[0]):
        h.update(f"\n{path}\t{sha}".encode())
    return h.hexdigest()


def _context():
    from app.services import kb_versions
    try:
        active = kb_versions.active_versions()
    except Exception:
        active = None
    try:
        authority = kb_versions.authority_map()
    except Exception:
        authority = {}
    return active, authority


def current_fingerprint(db) -> str:
    active, _ = _context()
    return fingerprint(_active_row_files(db, active))


def ready(db) -> bool:
    """True when the directory was built from exactly the knowledge base's current active row files."""
    try:
        r = db.execute("SELECT value FROM dir_meta WHERE key='fingerprint'").fetchone()
    except sqlite3.Error:
        return False
    return bool(r) and r[0] == current_fingerprint(db)


def rebuild(force: bool = False) -> dict:
    """Re-read the active RM lists into the directory tables. Returns a report (also stored in dir_meta)."""
    from app.rag import bankers
    from app.services.knowledge_base import _db
    active, authority = _context()
    with _lock, closing(_db()) as db:
        ensure_schema(db)
        files = _active_row_files(db, active)
        fp = fingerprint(files)
        if not force:
            r = db.execute("SELECT value FROM dir_meta WHERE key='fingerprint'").fetchone()
            if r and r[0] == fp:
                return {"skipped": "unchanged"}
        t0 = time.perf_counter()
        issues: List[Tuple[str, str, str]] = []
        counts: Dict[str, int] = {}

        def issue(kind, path, detail):
            counts[kind] = counts.get(kind, 0) + 1
            if counts[kind] <= MAX_ISSUE_ROWS:
                issues.append((kind, path, detail[:300]))

        wanted: Dict[str, dict] = {}
        first_of_sha: Dict[str, str] = {}
        for path, sha, meta in files:
            if meta.get("category") in SKIP_CATEGORIES:
                continue
            if sha and sha in first_of_sha:
                issue("duplicate_file", path, f"same content as {first_of_sha[sha]}")
                continue
            first_of_sha[sha] = path
            a = authority.get(path, {})
            wanted[path] = {"title": meta.get("title") or path, "category": meta.get("category") or "",
                            "bank": meta.get("bank") or "",
                            "recency": bankers._recency(path, meta.get("title", ""), a.get("effective_from")),
                            "priority": int(a.get("priority") or 0), "parsed": 0}

        lenders: Dict[str, Tuple[str, bool]] = {}
        cover: Dict[tuple, dict] = {}

        def add(f: dict, path: str, w: dict, text: str):
            """One person from one row into the coverage table (or an issue saying why not)."""
            if not f.get("lender"):
                issue("missing_lender", path, text)
                return
            name, phone_in_name = clean_name(f["name"])
            phone_raw = f.get("phone") or phone_in_name
            if phone_in_name:
                issue("phone_in_name", path, f["name"])
            phone = digits(re.split(r"[/,;]| or ", phone_raw)[0])
            if not name or not phone:
                return
            city = canonical_city(f.get("city") or "")
            if not city:
                issue("missing_city", path, text)
                return
            if not valid_mobile(phone):
                issue("invalid_phone", path, f"{f['lender']} / {name}: {phone_raw}")
            lender, known = canonical_lender(f["lender"])
            lkey = key(lender)
            lenders.setdefault(lkey, (lender, known))
            product = canonical_product(f.get("loan_type") or "")
            ck = (lkey, name_key(name), phone, key(city), product)
            w["parsed"] += 1
            c = cover.get(ck)
            if c is None:
                c = cover[ck] = {"name": name, "phone": phone, "city": city, "product": product, "periods": set(),
                                 "occurrences": 0, "best": None, "email": "", "designation": "", "branch": "",
                                 "raw_product": ""}
            c["occurrences"] += 1
            c["periods"].add(w["recency"])
            rank_key = (w["recency"], w["priority"], path)
            if c["best"] is None or rank_key > c["best"]:
                c["best"] = rank_key
                c.update(name=name, email=f.get("email", ""), designation=f.get("designation", ""),
                         branch=f.get("branch", ""), raw_product=f.get("loan_type", ""))

        rows_read = 0
        for path, text in db.execute("SELECT path, text FROM kb_rows"):
            w = wanted.get(path)
            if w is None or ":" not in text:
                continue
            rows_read += 1
            f = bankers._fields(text)
            has_person = f.get("name") and (f.get("phone") or _PHONE_RUN.search(f.get("name", "")))
            if has_person and f.get("lender"):
                records = [f]
            elif w["category"] == "Contacts" and (found := people_in_row(text, w["bank"])):
                records = found                     # several people per row / other column names (contact files)
            elif has_person:
                records = [f]                       # recorded as missing a lender
            else:
                if f.get("name") and f.get("lender") and w["category"] == "Contacts":
                    issue("missing_phone", path, text)
                continue
            for rec in records:
                add(rec, path, w, text)
        for path, w in wanted.items():
            if w["category"] == "Contacts" and not w["parsed"]:
                issue("no_labelled_columns", path, w["title"])

        # Same banker, city and product with two numbers in equally recent lists: flag, never pick silently.
        by_person: Dict[tuple, List[dict]] = {}
        for (lkey, nkey, _, ckey, product), c in cover.items():
            by_person.setdefault((lkey, nkey, ckey, product), []).append(c)
        for (lkey, nkey, ckey, product), group in by_person.items():
            if len(group) > 1:
                top = max(c["best"][:2] for c in group)
                tied = [c for c in group if c["best"][:2] == top]
                if len({c["phone"] for c in tied}) > 1:
                    issue("conflicting_phone", tied[0]["best"][2],
                          f"{lenders[lkey][0]} / {tied[0]['name']} ({tied[0]['city']}): "
                          + ", ".join(sorted({c['phone'] for c in tied})))

        db.execute("BEGIN IMMEDIATE")
        try:
            for t in ("dir_coverage", "dir_bankers", "dir_lenders", "dir_issues"):
                db.execute(f"DELETE FROM {t}")
            lender_ids = {}
            for lkey, (name, known) in lenders.items():
                lender_ids[lkey] = db.execute("INSERT INTO dir_lenders(name, name_key, canonical) VALUES (?,?,?)",
                                              (name, lkey, int(known))).lastrowid
            banker_ids: Dict[tuple, int] = {}
            for (lkey, nkey, phone, ckey, product), c in cover.items():
                bk = (lkey, nkey, phone)
                if bk not in banker_ids:
                    banker_ids[bk] = db.execute(
                        "INSERT INTO dir_bankers(lender_id, name, name_key, phone, phone_valid, email, designation) "
                        "VALUES (?,?,?,?,?,?,?)", (lender_ids[lkey], c["name"], nkey, phone, int(valid_mobile(phone)),
                                                   c["email"], c["designation"])).lastrowid
                recency, priority, path = c["best"]
                periods = sorted(p for p in c["periods"] if p)
                db.execute("INSERT OR REPLACE INTO dir_coverage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (banker_ids[bk], c["city"], ckey, product, c["raw_product"], c["branch"], recency,
                            periods[0] if periods else 0, ",".join(map(str, periods)) or "0", c["occurrences"],
                            priority, path, wanted[path]["title"]))
            db.executemany("INSERT INTO dir_issues(issue, source_path, detail) VALUES (?,?,?)", issues)
            report = {"files": len(wanted), "rows_read": rows_read, "lenders": len(lenders),
                      "bankers": len(banker_ids), "coverage": len(cover),
                      "rows_used": sum(w["parsed"] for w in wanted.values()), "issues": counts,
                      "seconds": round(time.perf_counter() - t0, 1)}
            db.execute("INSERT OR REPLACE INTO dir_meta VALUES ('fingerprint', ?)", (fp,))
            db.execute("INSERT OR REPLACE INTO dir_meta VALUES ('report', ?)", (json.dumps(report),))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    _cities.clear()
    log.info("Banker directory rebuilt: %s", report)
    return report


# ------------------------------------------------------------------ contact files with other layouts
# A label that names a person: "SM", "ASM", "RAH HEAD NAME", "CO DM/AGM", "NAME", "RM" ... (not a number, place or
# mail column). A label that holds a number: "Contact No.", "RAH HEAD CONTACT", "MOBILE".
_PERSON_LABEL = re.compile(r"\b(name|sm|asm|rsm|zsm|nsm|nm|bm|rm|abm|cm|manager|head|agm|dgm|dm|officer|person|"
                           r"executive|lead)\b", re.I)
_NOT_PERSON = re.compile(r"contact|mobile|phone|mail|e-?mail|\bno\b|number|office|circle|region|zone|branch|"
                         r"location|city|state|company|lender|bank|dp|code|sl|sr|designation|product|policy", re.I)
_PHONE_LABEL = re.compile(r"contact|mobile|phone|\bno\b|number|cell", re.I)
_PLACE_LABELS = ("city", "location", "regional office", "region", "branch", "circle office", "area")
_LENDER_LABELS = ("lender name", "lender", "bank name", "nbfc name", "company name", "financier")
_IN_BRACKETS = re.compile(r"\(([A-Za-z][A-Za-z .]{2,30})\)")


def _pairs(text: str) -> List[Tuple[str, str]]:
    """'A: 1 | B: 72765 | 30385' -> [('a', '1'), ('b', '72765 30385')]: an unlabelled part continues the value
    before it (a phone number split over 'First 5' / 'Last 5' columns)."""
    out: List[Tuple[str, str]] = []
    for part in text.split("|"):
        if ":" in part:
            k, v = part.split(":", 1)
            out.append((k.strip().lower().rstrip("*. "), v.strip()))
        elif out and part.strip():
            out[-1] = (out[-1][0], f"{out[-1][1]} {part.strip()}")
    return out


def people_in_row(text: str, file_bank: str = "") -> List[dict]:
    """Every person with a phone number in a contacts row whose columns are not the RM-list layout: each name
    column is paired with the next number column after it. The lender is the row's lender column, else the bank
    the file is about; the city is the row's place column, else a place in brackets after the name."""
    pairs = _pairs(text)
    lender = next((v for k, v in pairs if k in _LENDER_LABELS and v), "") or file_bank
    place = next((v for label in _PLACE_LABELS for k, v in pairs if k == label and v), "")
    out, person = [], None
    for k, v in pairs:
        if not v:
            continue
        if _PERSON_LABEL.search(k) and not _NOT_PERSON.search(k) and re.search(r"[A-Za-z]{2}", v) \
                and len(re.sub(r"\D", "", v)) < 5:
            person = {"name": v, "designation": k.upper()}
        elif k == "designation" and person is not None:
            person["designation"] = v
        elif _PHONE_LABEL.search(k) and person is not None and len(digits(v.replace(" ", ""))) >= 10:
            name = person["name"]
            city = place
            m = _IN_BRACKETS.search(name)
            if m:
                name, city = (name[:m.start()] + name[m.end():]).strip(), city or m.group(1)
            out.append({"lender": lender, "name": name, "phone": v.replace(" ", ""), "city": city,
                        "designation": person["designation"]})
            person = None
    return out


def rebuild_quietly():
    """After a sync: rebuild if the RM lists changed; a failure only means bankers.find() reads the row index."""
    try:
        return rebuild()
    except Exception:
        log.exception("Banker directory rebuild failed; banker matching reads the row index directly")
        return None


# ------------------------------------------------------------------ query
def _city_keys(db, city: str) -> List[str]:
    """Directory cities that match the asked-for one: the same spelling family, or containing it."""
    fp = db.execute("SELECT value FROM dir_meta WHERE key='fingerprint'").fetchone()
    fp = fp[0] if fp else ""
    if fp not in _cities:
        _cities.clear()
        _cities[fp] = [r[0] for r in db.execute("SELECT DISTINCT city_key FROM dir_coverage")]
    want = key(canonical_city(city))
    return [c for c in _cities[fp] if want and want in c]


def records(db, city: str, loan_type: str = "", lenders: Optional[List[str]] = None) -> List[dict]:
    """Coverage records for a city (and product: its own and the lists that name none), newest list first."""
    cities = _city_keys(db, city)
    if not cities:
        return []
    product = canonical_product(loan_type) if loan_type else ""
    db.row_factory = sqlite3.Row
    sql = ("SELECT l.name AS lender, l.name_key AS lender_key, b.name, b.name_key, b.phone, b.phone_valid, b.email, "
           "b.designation, c.city, c.product, c.raw_product, c.branch, c.last_seen, c.periods, c.occurrences, "
           "c.priority, c.source_path, c.source_title FROM dir_coverage c JOIN dir_bankers b USING(banker_id) "
           "JOIN dir_lenders l USING(lender_id) WHERE c.city_key IN (%s)" % ",".join("?" * len(cities)))
    args: list = list(cities)
    if product:
        sql += " AND c.product IN (?, '')"
        args.append(product)
    if lenders:
        keys = [key(n) for n in lenders]
        sql += " AND l.name_key IN (%s)" % ",".join("?" * len(keys))
        args += keys
    return [dict(r) for r in db.execute(sql + " ORDER BY c.last_seen DESC", args)]


_city_names: Dict[str, List[str]] = {}


def known_city(text: str) -> str:
    """The longest directory city named in text (as whole words), or ''. Only cities with at least one banker."""
    from app.services.knowledge_base import _db
    t = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    try:
        with closing(_db()) as db:
            if not ready(db):
                return ""
            fp = db.execute("SELECT value FROM dir_meta WHERE key='fingerprint'").fetchone()[0]
            if fp not in _city_names:
                _city_names.clear()
                _city_names[fp] = [r[0] for r in db.execute("SELECT DISTINCT city FROM dir_coverage")] + list(CITY_ALIASES) + list(ADMIN_CITY_ALIASES)
            names = _city_names[fp]
    except sqlite3.Error:
        return ""
    hits = [n for n in names if len(n) >= 3 and f" {re.sub(r'[^a-z0-9]+', ' ', n.lower()).strip()} " in t]
    return canonical_city(max(hits, key=len)) if hits else ""


def quality_report(limit: int = 20) -> dict:
    """For admins: the last build's totals and sample rows for each kind of issue."""
    from app.services.knowledge_base import _db
    with closing(_db()) as db:
        ensure_schema(db)
        r = db.execute("SELECT value FROM dir_meta WHERE key='report'").fetchone()
        out = {"built": json.loads(r[0]) if r else None, "current": ready(db), "issues": {}}
        for kind, label in ISSUES.items():
            rows = db.execute("SELECT source_path, detail FROM dir_issues WHERE issue=? LIMIT ?", (kind, limit)).fetchall()
            out["issues"][kind] = {"label": label, "count": (out["built"] or {}).get("issues", {}).get(kind, 0),
                                   "examples": [{"source": p, "detail": d} for p, d in rows]}
        return out
