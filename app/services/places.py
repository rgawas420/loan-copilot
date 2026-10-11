"""Lender branches in the customer's city, for the map and place cards under a customer-profile answer.

Data: OpenStreetMap, through Nominatim (no key). Its usage policy asks for an identifying User-Agent, at most one
request a second, and caching, so: one lookup per city (kept for a year), one search per lender and city (kept
for a week), requests spaced by a process-wide lock, short timeouts. A failure only means fewer places on the
map; it never affects the answer.

OpenStreetMap has most bank branches (Axis, ICICI, SBI, Central Bank...) but few housing-finance offices. A
lender with no branch on the map is reported in `missing`, and the apps show it as a card with a map search link.
"""
import json
import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import closing
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote

from app.core.config import get_settings
from app.services import db as dbtune

log = logging.getLogger(__name__)
NOMINATIM = "https://nominatim.openstreetmap.org/search"
CITY_TTL = 365 * 86400
BRANCH_TTL = 7 * 86400
MIN_GAP_S = 1.1                       # Nominatim: at most one request a second
TIMEOUT_S = 8
MAX_LENDERS = 10                      # the banker table's top 10
PER_LENDER = 2
MAX_PLACES = 10
ATTRIBUTION = "© OpenStreetMap contributors"

_lock = threading.Lock()
_last = 0.0
_SUFFIX = re.compile(r"\b(ltd|limited|pvt|private|co|company|inc|llp)\b\.?", re.I)
_BRANCH_TYPES = {("amenity", "bank"), ("office", "financial"), ("office", "company"), ("office", "financial_advisor"),
                 ("office", "yes")}


def _db():
    path = get_settings().places_cache_db
    path.parent.mkdir(parents=True, exist_ok=True)
    db = dbtune.tune(sqlite3.connect(path, timeout=10))
    db.execute("CREATE TABLE IF NOT EXISTS places_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL)")
    return db


def _cached(key: str, ttl: float):
    try:
        with closing(_db()) as db:
            row = db.execute("SELECT value, ts FROM places_cache WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        log.exception("Places cache unreadable")
        return None
    return json.loads(row[0]) if row and time.time() - row[1] < ttl else None


def _store(key: str, value) -> None:
    try:
        with closing(_db()) as db, db:
            db.execute("INSERT OR REPLACE INTO places_cache (key, value, ts) VALUES (?, ?, ?)",
                       (key, json.dumps(value, ensure_ascii=False), time.time()))
    except sqlite3.Error:
        log.exception("Places cache not written")


def _search(params: dict) -> Optional[list]:
    """One Nominatim request, spaced from the previous one; None on failure (not cached)."""
    import httpx
    global _last
    with _lock:
        wait = MIN_GAP_S - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = httpx.get(NOMINATIM, params={"format": "jsonv2", "countrycodes": "in", "accept-language": "en", **params},
                          headers={"User-Agent": get_settings().places_user_agent}, timeout=TIMEOUT_S)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Place search failed for %s: %s", params.get("q"), e)
            return None
        finally:
            _last = time.monotonic()


def clean_lender(name: str) -> str:
    """'Muthoot Homefin India Ltd.' -> 'Muthoot Homefin India'; '(Tyger)' and the like are dropped."""
    s = re.sub(r"\([^)]*\)", " ", name or "")
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", re.sub(r"[^\w&' -]", " ", s)).strip()


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_lender(name).lower())


def city(name: str) -> Optional[dict]:
    """{name, lat, lon, viewbox} for an Indian city, or None."""
    key = f"city:{name.strip().lower()}"
    hit = _cached(key, CITY_TTL)
    if hit is not None:
        return hit or None
    rows = _search({"q": f"{name}, India", "limit": 1})
    if rows is None:
        return None
    if not rows:
        _store(key, {})
        return None
    r = rows[0]
    s, n, w, e = (float(x) for x in r["boundingbox"])
    out = {"name": r.get("name") or name, "lat": float(r["lat"]), "lon": float(r["lon"]), "viewbox": [w, n, e, s]}
    _store(key, out)
    return out


def _address(a: dict) -> str:
    parts = [a.get("road"), a.get("neighbourhood") or a.get("suburb") or a.get("city_district")]
    return ", ".join(dict.fromkeys(p for p in parts if p))


def _dist_km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    h = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


def branches(lender: str, where: dict) -> List[dict]:
    """The lender's branches inside the city, nearest the centre first (ATMs left out)."""
    q = clean_lender(lender)
    if not q:
        return []
    key = f"branches:{where['name'].lower()}:{_key(lender)}"
    hit = _cached(key, BRANCH_TTL)
    if hit is not None:
        return hit
    rows = _search({"q": q, "limit": 10, "viewbox": ",".join(str(x) for x in where["viewbox"]), "bounded": 1,
                    "addressdetails": 1, "extratags": 1})
    if rows is None:
        return []
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in {"bank", "of", "india", "the", "finance"}]
    out = []
    for r in rows:
        if (r.get("category"), r.get("type")) not in _BRANCH_TYPES:
            continue
        name = r.get("name") or ""
        # loosely matched (ICICI Bank for ICICI Home Finance; any bank for "Bank of India")
        if not (all(w in name.lower() for w in words) if words else q.lower() in name.lower()):
            continue
        lat, lon = float(r["lat"]), float(r["lon"])
        tags = r.get("extratags") or {}
        out.append({"name": name, "lat": lat, "lon": lon, "address": _address(r.get("address") or {}),
                    "hours": tags.get("opening_hours") or "", "phone": tags.get("phone") or tags.get("contact:phone") or "",
                    "km": round(_dist_km(where["lat"], where["lon"], lat, lon), 1),
                    "osm": f"https://www.openstreetmap.org/{r.get('osm_type', 'node')}/{r.get('osm_id')}"})
    out.sort(key=lambda b: b["km"])
    _store(key, out)
    return out


def _directions(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}"


def find(lenders: Iterable[str], city_name: str) -> Optional[dict]:
    """{city, center, places: [...], missing: [...], attribution} for the map, or None (nothing to show)."""
    if not get_settings().places_enabled or not city_name:
        return None
    names = list(dict.fromkeys(n.strip() for n in lenders if n and n.strip()))[:MAX_LENDERS]
    if not names:
        return None
    where = city(city_name)
    if not where:
        return None
    found: Dict[str, List[dict]] = {n: branches(n, where) for n in names}
    places = []
    for i in range(PER_LENDER):                       # round-robin: every lender's nearest branch first
        for n in names:
            if i < len(found[n]) and len(places) < MAX_PLACES:
                places.append({**found[n][i], "lender": n, "directions": _directions(found[n][i]["lat"], found[n][i]["lon"])})
    missing = [{"lender": n, "search": f"https://www.google.com/maps/search/?api=1&query={quote(clean_lender(n) + ' ' + where['name'])}"}
               for n in names if not found[n]]
    if not places and not missing:
        return None
    return {"city": where["name"], "center": [where["lat"], where["lon"]], "places": places, "missing": missing,
            "attribution": ATTRIBUTION}

