"""Sponsored cards ("Ads") shown under knowledge-base answers, created by admins in the console.

How an ad is chosen, after an answer has been written:
  - only after "Ask anything" (knowledge-base) answers that found something: never in or after a lender
    recommendation, a follow-up about one, a "not found" reply or an error, so an ad can never look like it
    influenced which lender was recommended;
  - at most one ad per answer, and no more often than every ADS_EVERY_N_ANSWERS answers per user;
  - matched on the topic the question was about (banks, products and keywords, detected on the server with the
    same vocabulary as retrieval). An ad with targeting is shown only when the question matches it; an ad
    without targeting can run anywhere. Ads a user hid are never shown to them again.
The answer, the retrieval and the LLM never see ads, and the question text never leaves the server: advertisers
see impression and click counts only.
"""
import io
import json
import logging
import random
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from app.core.config import get_settings
from app.services import db
from app.rag import entities

log = logging.getLogger(__name__)
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
MAX_IMAGE_BYTES = 3 * 1024 * 1024
FIELDS = ("advertiser", "title", "body", "link", "cta", "banks", "products", "keywords", "starts", "ends",
          "priority", "active")
REPORT_REASONS = ("misleading", "offensive", "irrelevant", "other")


class AdError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def _now():
    return datetime.now(timezone.utc)


def _iso(t=None):
    return (t or _now()).isoformat(timespec="seconds")


def _conn():
    c = sqlite3.connect(get_settings().audit_db, timeout=db.BUSY_TIMEOUT_S)
    c.row_factory = sqlite3.Row
    return db.tune(c)


def init_db():
    with closing(_conn()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS ads(id INTEGER PRIMARY KEY AUTOINCREMENT, advertiser TEXT NOT NULL, title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '', link TEXT NOT NULL, cta TEXT NOT NULL DEFAULT '', image TEXT,
            banks TEXT NOT NULL DEFAULT '[]', products TEXT NOT NULL DEFAULT '[]', keywords TEXT NOT NULL DEFAULT '[]',
            starts TEXT, ends TEXT, priority INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            created TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS ad_events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ad_id INTEGER, user_id INTEGER,
            kind TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS ad_events_ad ON ad_events(ad_id, kind);
        CREATE INDEX IF NOT EXISTS ad_events_user ON ad_events(user_id, kind, ts);
        CREATE TABLE IF NOT EXISTS ad_hidden(user_id INTEGER, ad_id INTEGER, ts TEXT, PRIMARY KEY(user_id, ad_id));
        CREATE TABLE IF NOT EXISTS ad_pacing(user_id INTEGER PRIMARY KEY, answers_since INTEGER NOT NULL DEFAULT 0);
        """)
        c.commit()


def _image_dir() -> Path:
    return Path(get_settings().attachment_dir).parent / "ads"


# ------------------------------------------------------------------ admin: create, edit, list
def _list_field(v) -> List[str]:
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    return [x.strip()[:80] for x in (v or []) if str(x).strip()][:30]


def _clean(data: dict, partial: bool = False) -> dict:
    out = {}
    for k in FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k in ("banks", "products", "keywords"):
            out[k] = json.dumps(_list_field(v))
        elif k == "priority":
            out[k] = max(0, min(int(v or 0), 100))
        elif k == "active":
            out[k] = int(bool(v))
        elif k in ("starts", "ends"):
            out[k] = (str(v).strip()[:25] or None) if v else None
        else:
            out[k] = str(v or "").strip()
    limits = {"advertiser": 80, "title": 90, "body": 220, "cta": 30, "link": 500}
    for k, n in limits.items():
        if k in out and len(out[k]) > n:
            raise AdError(f"{k.capitalize()} is too long (at most {n} characters).")
    for k in ("advertiser", "title", "link"):
        if (not partial or k in out) and not out.get(k):
            raise AdError(f"Enter the {k}.")
    if "link" in out and not re.match(r"^https?://[^\s]+$", out["link"]):
        raise AdError("The link must start with http:// or https://.")
    if out.get("starts") and out.get("ends") and out["ends"] < out["starts"]:
        raise AdError("The end date is before the start date.")
    return out


def _view(r, stats: Optional[dict] = None) -> dict:
    d = dict(r)
    for k in ("banks", "products", "keywords"):
        d[k] = json.loads(d[k] or "[]")
    d["active"] = bool(d["active"])
    d["image_url"] = f"/api/ads/{d['id']}/image" if d.get("image") else None
    d.pop("image", None)
    if stats is not None:
        s = stats.get(d["id"], {})
        imp, clk = s.get("impression", 0), s.get("click", 0)
        d["stats"] = {"impressions": imp, "clicks": clk, "ctr": round(clk / imp * 100, 2) if imp else 0.0,
                      "hides": s.get("hide", 0), "reports": s.get("report", 0)}
    return d


def _stats(c, days: Optional[int] = None) -> dict:
    sql, args = "SELECT ad_id, kind, COUNT(*) AS n FROM ad_events", []
    if days:
        sql += " WHERE ts >= ?"
        args.append(_iso(_now() - timedelta(days=days)))
    out: dict = {}
    for r in c.execute(sql + " GROUP BY ad_id, kind", args):
        out.setdefault(r["ad_id"], {})[r["kind"]] = r["n"]
    return out


def list_ads(days: Optional[int] = None) -> List[dict]:
    with closing(_conn()) as c:
        st = _stats(c, days)
        return [_view(r, st) for r in c.execute("SELECT * FROM ads ORDER BY active DESC, priority DESC, id DESC")]


def get(ad_id: int) -> Optional[dict]:
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
        return _view(r, _stats(c)) if r else None


def create(data: dict) -> dict:
    row = _clean(data)
    row.setdefault("active", 1)
    with closing(_conn()) as c:
        cols = ", ".join(row)
        cur = c.execute(f"INSERT INTO ads({cols}, created, updated) VALUES ({', '.join('?' * len(row))}, ?, ?)",
                        (*row.values(), _iso(), _iso()))
        c.commit()
        return get(cur.lastrowid)


def update(ad_id: int, data: dict) -> dict:
    row = _clean(data, partial=True)
    with closing(_conn()) as c:
        if not c.execute("SELECT 1 FROM ads WHERE id=?", (ad_id,)).fetchone():
            raise AdError("Ad not found.", 404)
        if row:
            c.execute(f"UPDATE ads SET {', '.join(f'{k}=?' for k in row)}, updated=? WHERE id=?", (*row.values(), _iso(), ad_id))
            c.commit()
    return get(ad_id)


def delete(ad_id: int) -> bool:
    with closing(_conn()) as c:
        r = c.execute("SELECT image FROM ads WHERE id=?", (ad_id,)).fetchone()
        if r is None:
            return False
        c.execute("DELETE FROM ads WHERE id=?", (ad_id,))
        c.execute("DELETE FROM ad_hidden WHERE ad_id=?", (ad_id,))
        c.commit()
    if r["image"]:
        Path(r["image"]).unlink(missing_ok=True)
    return True


def set_image(ad_id: int, data: bytes) -> dict:
    """Store the ad's picture, re-encoded (strips anything that is not image data), at most 1200 px wide."""
    if len(data) > MAX_IMAGE_BYTES:
        raise AdError("The image is larger than 3 MB.", 413)
    from PIL import Image, ImageOps
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        img.load()
    except Exception:
        raise AdError("That file is not an image (use PNG, JPEG or WebP).")
    if not get(ad_id):
        raise AdError("Ad not found.", 404)
    img.thumbnail((1200, 1200))
    d = _image_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"ad-{ad_id}.png"
    img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(path, "PNG", optimize=True)
    with closing(_conn()) as c:
        c.execute("UPDATE ads SET image=?, updated=? WHERE id=?", (str(path), _iso(), ad_id))
        c.commit()
    return get(ad_id)


def image_path(ad_id: int) -> Optional[Path]:
    with closing(_conn()) as c:
        r = c.execute("SELECT image FROM ads WHERE id=?", (ad_id,)).fetchone()
    p = Path(r["image"]) if r and r["image"] else None
    return p if p and p.exists() else None


def daily(ad_id: int, days: int = 30) -> List[dict]:
    with closing(_conn()) as c:
        rows = c.execute("SELECT substr(ts, 1, 10) AS day, kind, COUNT(*) AS n FROM ad_events WHERE ad_id=? AND ts>=? "
                         "GROUP BY day, kind ORDER BY day", (ad_id, _iso(_now() - timedelta(days=days)))).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["day"], {"day": r["day"], "impression": 0, "click": 0, "hide": 0, "report": 0})[r["kind"]] = r["n"]
    return list(out.values())


def reports(ad_id: int, limit: int = 50) -> List[dict]:
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute("SELECT ts, detail FROM ad_events WHERE ad_id=? AND kind='report' "
                                           "ORDER BY id DESC LIMIT ?", (ad_id, limit))]


# ------------------------------------------------------------------ serving
def _running(r, now: str) -> bool:
    return bool(r["active"]) and (not r["starts"] or r["starts"][:10] <= now[:10]) and (not r["ends"] or r["ends"][:10] >= now[:10])


def _topic(question: str, history: Optional[list]) -> dict:
    """What the question is about, from the same vocabulary retrieval uses (no LLM, nothing sent anywhere)."""
    text = question + " " + " ".join(str(m.get("text", "")) for m in (history or [])[-2:] if m.get("role", "user") == "user")
    return {"banks": entities.find_banks(question) or entities.find_banks(text),
            "products": entities.find_products(question) or entities.find_products(text),
            "text": text.lower()}


def _score(r, topic: dict) -> Optional[tuple]:
    """(score, reason) when the ad fits the topic, else None."""
    banks, products, keywords = (json.loads(r[k] or "[]") for k in ("banks", "products", "keywords"))
    if not (banks or products or keywords):
        return 1 + r["priority"], ""
    norm = lambda xs: {x.lower() for x in xs}
    hit_b = [b for b in topic["banks"] if b.lower() in norm(banks) or any(a in norm(banks) for a in entities.bank_aliases(b))]
    hit_p = [p for p in topic["products"] if p.lower() in norm(products) or any(a in norm(products) for a in entities.product_aliases(p))]
    hit_k = [k for k in keywords if re.search(r"(?<!\w)" + re.escape(k.lower()) + r"(?!\w)", topic["text"])]
    # A keyword match is enough. Otherwise every targeted dimension must match: an "HDFC + Home Loan" ad is not
    # shown for an HDFC personal-loan question, nor for an ICICI home-loan one.
    fits = bool(hit_k) or ((bool(banks) or bool(products)) and (not banks or bool(hit_b)) and (not products or bool(hit_p)))
    if not fits:
        return None
    reason = " · ".join(hit_b + hit_p + hit_k[:2])
    return 10 * len(hit_b) + 5 * len(hit_p) + 3 * len(hit_k) + r["priority"], reason


def pick(user_id: int, question: str, history: Optional[list] = None) -> Optional[dict]:
    """The ad to show after this answer, or None. Records the impression and advances the user's pacing."""
    s = get_settings()
    if not s.ads_enabled:
        return None
    every = max(1, s.ads_every_n_answers)
    with closing(_conn()) as c:
        row = c.execute("SELECT answers_since FROM ad_pacing WHERE user_id=?", (user_id,)).fetchone()
        since = (row["answers_since"] if row else every - 1) + 1
        c.execute("INSERT INTO ad_pacing(user_id, answers_since) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                  "answers_since=excluded.answers_since", (user_id, since))
        c.commit()
        if since < every:
            return None
        now, topic = _iso(), _topic(question, history)
        hidden = {r["ad_id"] for r in c.execute("SELECT ad_id FROM ad_hidden WHERE user_id=?", (user_id,))}
        scored = []
        for r in c.execute("SELECT * FROM ads"):
            if r["id"] in hidden or not _running(r, now):
                continue
            sc = _score(r, topic)
            if sc:
                scored.append((sc[0], random.random(), r, sc[1]))
        if not scored:
            return None
        best = max(scored, key=lambda x: (x[0], x[1]))
        _, _, r, reason = best
        c.execute("INSERT INTO ad_events(ts, ad_id, user_id, kind, detail) VALUES (?,?,?,?,?)",
                  (now, r["id"], user_id, "impression", reason[:200]))
        c.execute("UPDATE ad_pacing SET answers_since=0 WHERE user_id=?", (user_id,))
        c.commit()
    v = _view(r)
    return {"id": v["id"], "advertiser": v["advertiser"], "title": v["title"], "body": v["body"], "cta": v["cta"],
            "image_url": v["image_url"], "click_url": f"/api/ads/{v['id']}/click", "domain": _domain(v["link"]),
            "why": (f"Shown because your question was about {reason}." if reason else
                    "Shown to everyone using the copilot. It was not chosen from your conversation.") +
                   " Ads never affect answers or lender recommendations."}


def _domain(link: str) -> str:
    m = re.match(r"^https?://([^/:?#]+)", link or "")
    return (m.group(1) if m else "").removeprefix("www.")


def record(ad_id: int, user_id: int, kind: str, detail: str = "") -> Optional[str]:
    """Click / hide / report. Returns the ad's link for a click."""
    with closing(_conn()) as c:
        r = c.execute("SELECT link FROM ads WHERE id=?", (ad_id,)).fetchone()
        if r is None:
            return None
        c.execute("INSERT INTO ad_events(ts, ad_id, user_id, kind, detail) VALUES (?,?,?,?,?)",
                  (_iso(), ad_id, user_id, kind, detail[:300]))
        if kind == "hide":
            c.execute("INSERT OR IGNORE INTO ad_hidden(user_id, ad_id, ts) VALUES (?,?,?)", (user_id, ad_id, _iso()))
        c.commit()
    return r["link"]
