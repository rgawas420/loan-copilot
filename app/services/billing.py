"""Billing service: plans, subscriptions and their lifecycle, checkouts, payments, webhooks and the admin's
billing settings.

Every user always has exactly one current subscription (status trialing, active or past_due). A user without a
paid plan is on the default (free) plan, which renews by itself. Lifecycle:

    free ── trial ──> trialing ── trial ends without payment ──> expired ──> free
    free ── checkout + verified payment ──> active ── renewal payment ──> active (next period)
    active ── period ends unpaid ──> past_due ── grace days pass ──> expired ──> free
    active ── cancel ──> (access to the end of the paid period) ──> canceled ──> free

A paid plan is activated only by an event the server has verified: a signed provider webhook, or a checkout the
server confirmed with the provider's API. Never by the browser saying it paid.

Lifecycle changes are applied when a subscription is read (current()), so no scheduler is needed; every change
runs inside a write transaction. Ending a subscription never deletes chats or usage history.

Money is stored as integer minor units (paise). The API shows rupees.
"""
import calendar
import json
import logging
import secrets
from datetime import timedelta
from typing import List, Optional

from app.services import payments
from app.services.db import iso, now, parse, read, transaction

log = logging.getLogger(__name__)

LIVE = ("trialing", "active", "past_due")
INTERVALS = ("monthly", "yearly")

DEFAULT_SETTINGS = {
    "warn_thresholds": [75, 90],          # % of the token allowance at which users are warned; 100% = limit
    "usd_to_inr": 88.0,                   # converts provider cost (USD) for reports
    "grace_days": 3,                      # past_due keeps access this long after the period ends unpaid
    "near_limit_policy": "block",         # block | allow: a request expected to need more than what is left
    "anonymous_daily_tokens": 20000,      # AI tokens per day for the classic page without login (0 = log in)
    "anonymous_max_concurrent": 4,
}

PLAN_DEFAULTS = {
    "description": "", "price_minor": 0, "currency": "INR", "billing_interval": "monthly", "token_limit": 10000,
    "allowed_models": [], "max_requests": 0, "requests_per_minute": 10, "max_concurrent": 2,
    "max_context_tokens": 16000, "max_output_tokens": 1200, "rag_enabled": True, "max_file_mb": 5,
    "max_files_per_month": 10, "allow_overage": False, "overage_price_per_1k_minor": 0, "max_overage_tokens": 0,
    "trial_days": 0, "features": [], "external_ids": {}, "is_default": False, "is_active": True, "sort_order": 0,
}
JSON_FIELDS = ("allowed_models", "features", "external_ids")
BOOL_FIELDS = ("rag_enabled", "allow_overage", "is_default", "is_active")

# First-run plans. Everything here is data: admins change it in the admin console.
SEED_PLANS = [
    {"code": "free", "name": "Free", "price_minor": 0, "token_limit": 10_000, "is_default": True, "sort_order": 0,
     "allowed_models": ["openai/gpt-oss-20b"], "requests_per_minute": 10, "max_concurrent": 1,
     "max_output_tokens": 800, "max_file_mb": 2, "max_files_per_month": 5, "max_context_tokens": 8000,
     "features": ["Basic model", "Knowledge-base answers", "Lender matching"]},
    {"code": "starter", "name": "Starter", "price_minor": 29_900, "token_limit": 100_000, "sort_order": 1,
     "allowed_models": ["openai/gpt-oss-20b"], "requests_per_minute": 20, "max_concurrent": 2,
     "max_file_mb": 5, "max_files_per_month": 30,
     "features": ["Standard model", "Knowledge-base answers", "Lender matching", "File attachments"]},
    {"code": "pro", "name": "Pro", "price_minor": 99_900, "token_limit": 500_000, "sort_order": 2, "trial_days": 7,
     "allowed_models": ["openai/gpt-oss-20b", "openai/gpt-oss-120b"], "requests_per_minute": 30, "max_concurrent": 3,
     "max_file_mb": 15, "max_files_per_month": 200, "max_context_tokens": 24000,
     "features": ["Advanced model", "Knowledge-base answers", "Lender matching", "File attachments", "7-day free trial"]},
    {"code": "business", "name": "Business", "price_minor": 299_900, "token_limit": 2_000_000, "sort_order": 3,
     "allowed_models": [], "requests_per_minute": 60, "max_concurrent": 5, "max_file_mb": 15,
     "max_files_per_month": 1000, "max_context_tokens": 32000, "allow_overage": True,
     "overage_price_per_1k_minor": 200, "max_overage_tokens": 1_000_000,
     "features": ["Advanced models", "Priority processing", "Overage at ₹2 per 1,000 tokens", "File attachments"]},
]


class BillingError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ schema
def init_db():
    with read() as c:                      # executescript commits by itself: not inside transaction()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', price_minor INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'INR', billing_interval TEXT NOT NULL DEFAULT 'monthly',
            token_limit INTEGER NOT NULL, allowed_models TEXT NOT NULL DEFAULT '[]',
            max_requests INTEGER NOT NULL DEFAULT 0, requests_per_minute INTEGER NOT NULL DEFAULT 10,
            max_concurrent INTEGER NOT NULL DEFAULT 2, max_context_tokens INTEGER NOT NULL DEFAULT 16000,
            max_output_tokens INTEGER NOT NULL DEFAULT 1200, rag_enabled INTEGER NOT NULL DEFAULT 1,
            max_file_mb INTEGER NOT NULL DEFAULT 5, max_files_per_month INTEGER NOT NULL DEFAULT 10,
            allow_overage INTEGER NOT NULL DEFAULT 0, overage_price_per_1k_minor INTEGER NOT NULL DEFAULT 0,
            max_overage_tokens INTEGER NOT NULL DEFAULT 0, trial_days INTEGER NOT NULL DEFAULT 0,
            features TEXT NOT NULL DEFAULT '[]', external_ids TEXT NOT NULL DEFAULT '{}',
            is_default INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0, created TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, plan_id INTEGER NOT NULL REFERENCES plans(id),
            status TEXT NOT NULL, billing_cycle TEXT NOT NULL, is_trial INTEGER NOT NULL DEFAULT 0,
            start_date TEXT NOT NULL, current_period_start TEXT NOT NULL, current_period_end TEXT NOT NULL,
            paid_period_start TEXT, cancel_at_period_end INTEGER NOT NULL DEFAULT 0, pending_plan_id INTEGER,
            payment_provider TEXT NOT NULL DEFAULT 'none', external_subscription_id TEXT,
            ended_at TEXT, created TEXT, updated TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_one_live ON subscriptions(user_id)
            WHERE status IN ('trialing','active','past_due');
        CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_external ON subscriptions(payment_provider, external_subscription_id)
            WHERE external_subscription_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS checkouts(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            plan_id INTEGER NOT NULL, provider TEXT NOT NULL, external_id TEXT, status TEXT NOT NULL,
            created TEXT, updated TEXT);
        CREATE INDEX IF NOT EXISTS checkouts_external ON checkouts(provider, external_id);
        CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, subscription_id INTEGER,
            provider TEXT NOT NULL, external_payment_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'subscription',
            amount_minor INTEGER NOT NULL DEFAULT 0, refunded_minor INTEGER NOT NULL DEFAULT 0, currency TEXT,
            status TEXT NOT NULL, description TEXT, created TEXT, updated TEXT, UNIQUE(provider, external_payment_id));
        CREATE TABLE IF NOT EXISTS charges(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, subscription_id INTEGER,
            kind TEXT NOT NULL, period_start TEXT, period_end TEXT, tokens INTEGER, amount_minor INTEGER, currency TEXT,
            status TEXT NOT NULL, created TEXT, updated TEXT, UNIQUE(subscription_id, kind, period_start));
        CREATE TABLE IF NOT EXISTS webhook_events(id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
            event_id TEXT NOT NULL, event_type TEXT, raw_type TEXT, status TEXT, detail TEXT, received TEXT,
            UNIQUE(provider, event_id));
        CREATE TABLE IF NOT EXISTS billing_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
    with transaction() as c:
        if c.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0:
            for p in SEED_PLANS:
                _insert_plan(c, {**PLAN_DEFAULTS, **p})


# ------------------------------------------------------------------ settings
def get_settings_all(c=None) -> dict:
    if c is None:
        with read() as c:
            return get_settings_all(c)
    stored = {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key, value FROM billing_settings")}
    return {**DEFAULT_SETTINGS, **stored}


def update_settings(values: dict) -> dict:
    clean = {}
    for k, v in values.items():
        if k not in DEFAULT_SETTINGS or v is None:
            continue
        if k == "warn_thresholds":
            v = sorted({int(x) for x in v if 0 < int(x) < 100})
        elif k == "near_limit_policy" and v not in ("block", "allow"):
            raise BillingError("near_limit_policy must be block or allow.")
        elif k == "usd_to_inr" and not 0 < float(v) < 10000:
            raise BillingError("Enter a valid USD to INR rate.")
        elif k in ("grace_days", "anonymous_daily_tokens", "anonymous_max_concurrent") and int(v) < 0:
            raise BillingError(f"{k} cannot be negative.")
        clean[k] = v
    with transaction() as c:
        for k, v in clean.items():
            c.execute("INSERT INTO billing_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, json.dumps(v)))
        return get_settings_all(c)


# ------------------------------------------------------------------ plans
def plan_view(r, admin: bool = False) -> dict:
    p = {k: r[k] for k in r.keys()}
    for k in JSON_FIELDS:
        p[k] = json.loads(p[k] or ("{}" if k == "external_ids" else "[]"))
    for k in BOOL_FIELDS:
        p[k] = bool(p[k])
    p["price"] = p["price_minor"] / 100
    p["overage_price_per_1k"] = p["overage_price_per_1k_minor"] / 100
    if not admin:
        p.pop("external_ids", None)
    return p


def _insert_plan(c, p: dict) -> int:
    t = iso(now())
    cols = ["code", "name"] + [k for k in PLAN_DEFAULTS]
    vals = [json.dumps(p[k]) if k in JSON_FIELDS else int(p[k]) if k in BOOL_FIELDS else p[k] for k in cols]
    cur = c.execute(f"INSERT INTO plans({','.join(cols)},created,updated) VALUES ({','.join('?' * len(cols))},?,?)",
                    (*vals, t, t))
    if p.get("is_default"):
        c.execute("UPDATE plans SET is_default=0 WHERE id<>?", (cur.lastrowid,))
    return cur.lastrowid


def _validate_plan(p: dict):
    if not str(p.get("name", "")).strip():
        raise BillingError("Enter a plan name.")
    if p["billing_interval"] not in INTERVALS:
        raise BillingError("Billing interval must be monthly or yearly.")
    for k in ("price_minor", "token_limit", "max_requests", "requests_per_minute", "max_concurrent", "max_context_tokens",
              "max_output_tokens", "max_file_mb", "max_files_per_month", "overage_price_per_1k_minor",
              "max_overage_tokens", "trial_days"):
        if int(p[k]) < 0:
            raise BillingError(f"{k} cannot be negative.")
    if p["requests_per_minute"] < 1 or p["max_concurrent"] < 1 or p["max_output_tokens"] < 50:
        raise BillingError("Requests per minute and concurrent requests must be at least 1, output tokens at least 50.")
    if p["is_default"] and (p["price_minor"] or not p["is_active"]):
        raise BillingError("The default plan must be free and active.")


def list_plans(include_inactive: bool = False, admin: bool = False) -> List[dict]:
    with read() as c:
        rows = c.execute("SELECT * FROM plans" + ("" if include_inactive else " WHERE is_active=1")
                         + " ORDER BY sort_order, price_minor").fetchall()
    return [plan_view(r, admin) for r in rows]


def get_plan(c, plan_id: int) -> Optional[dict]:
    r = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    return plan_view(r, admin=True) if r else None


def default_plan(c) -> dict:
    r = c.execute("SELECT * FROM plans WHERE is_default=1 AND is_active=1 ORDER BY id LIMIT 1").fetchone() \
        or c.execute("SELECT * FROM plans WHERE price_minor=0 AND is_active=1 ORDER BY sort_order LIMIT 1").fetchone()
    if r is None:
        raise BillingError("No free default plan is configured.", 500)
    return plan_view(r, admin=True)


def save_plan(data: dict, plan_id: Optional[int] = None) -> dict:
    with transaction() as c:
        if plan_id is None:
            code = str(data.get("code") or data.get("name") or "").strip().lower().replace(" ", "-")[:40]
            if not code or c.execute("SELECT 1 FROM plans WHERE code=?", (code,)).fetchone():
                raise BillingError("Choose a unique plan code.")
            p = {**PLAN_DEFAULTS, **{k: v for k, v in data.items() if k in PLAN_DEFAULTS or k == "name"}, "code": code}
            _validate_plan(p)
            pid = _insert_plan(c, p)
        else:
            cur = get_plan(c, plan_id)
            if cur is None:
                raise BillingError("Plan not found.", 404)
            p = {**cur, **{k: v for k, v in data.items() if k in PLAN_DEFAULTS or k == "name"}}
            _validate_plan(p)
            if cur["is_default"] and (not p["is_default"]):
                raise BillingError("Make another free plan the default first.")
            sets = ["name"] + list(PLAN_DEFAULTS)
            vals = [json.dumps(p[k]) if k in JSON_FIELDS else int(p[k]) if k in BOOL_FIELDS else p[k] for k in sets]
            c.execute(f"UPDATE plans SET {', '.join(f'{k}=?' for k in sets)}, updated=? WHERE id=?",
                      (*vals, iso(now()), plan_id))
            if p["is_default"]:
                c.execute("UPDATE plans SET is_default=0 WHERE id<>?", (plan_id,))
            pid = plan_id
        return get_plan(c, pid)


# ------------------------------------------------------------------ periods
def add_interval(anchor, interval: str, n: int):
    """anchor + n months (or years), keeping the anchor's day where the month has it (31 Jan -> 28 Feb -> 31 Mar)."""
    months = n * (12 if interval == "yearly" else 1)
    y, m = divmod(anchor.month - 1 + months, 12)
    year, month = anchor.year + y, m + 1
    return anchor.replace(year=year, month=month, day=min(anchor.day, calendar.monthrange(year, month)[1]))


def _next_end(sub: dict, plan: dict):
    """End of the period after the current one, counted from the subscription's start date (no drift)."""
    anchor, cur_end = parse(sub["start_date"]), parse(sub["current_period_end"])
    n = 1
    while add_interval(anchor, plan["billing_interval"], n) <= cur_end:
        n += 1
    return add_interval(anchor, plan["billing_interval"], n)


# ------------------------------------------------------------------ subscriptions
def _sub(c, sub_id: int) -> dict:
    return dict(c.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone())


def _live(c, user_id: int) -> Optional[dict]:
    r = c.execute("SELECT * FROM subscriptions WHERE user_id=? AND status IN ('trialing','active','past_due')",
                  (user_id,)).fetchone()
    return dict(r) if r else None


def _create(c, user_id: int, plan: dict, start, status: str = "active", provider: str = "none",
            external_id: Optional[str] = None, paid: bool = False, trial_days: int = 0) -> dict:
    end = start + timedelta(days=trial_days) if trial_days else add_interval(start, plan["billing_interval"], 1)
    t = iso(now())
    cur = c.execute("""INSERT INTO subscriptions(user_id, plan_id, status, billing_cycle, is_trial, start_date,
                       current_period_start, current_period_end, paid_period_start, payment_provider,
                       external_subscription_id, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (user_id, plan["id"], status, plan["billing_interval"], 1 if trial_days else 0, iso(start),
                     iso(start), iso(end), iso(start) if paid else None, provider, external_id, t, t))
    return _sub(c, cur.lastrowid)


def _end(c, sub: dict, status: str, at):
    c.execute("UPDATE subscriptions SET status=?, ended_at=?, updated=? WHERE id=?", (status, iso(at), iso(now()), sub["id"]))


def _close_period(c, sub: dict, plan: dict):
    """Turn the period's billable overage into a charge (due). Nothing is taken from the customer automatically."""
    if not plan["allow_overage"] or not plan["overage_price_per_1k_minor"]:
        return
    r = c.execute("SELECT overage_tokens FROM usage_totals WHERE subscription_id=? AND period_start=?",
                  (sub["id"], sub["current_period_start"])).fetchone()
    tokens = r["overage_tokens"] if r else 0
    if tokens > 0:
        amount = -(-tokens * plan["overage_price_per_1k_minor"] // 1000)          # round up to the paisa
        c.execute("""INSERT OR IGNORE INTO charges(user_id, subscription_id, kind, period_start, period_end, tokens,
                     amount_minor, currency, status, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                  (sub["user_id"], sub["id"], "overage", sub["current_period_start"], sub["current_period_end"], tokens,
                   amount, plan["currency"], "due", iso(now()), iso(now())))


def _roll(c, sub: dict, plan: dict, paid: bool = False) -> dict:
    """Start the next period of the same subscription (renewal). A scheduled plan change takes effect now."""
    _close_period(c, sub, plan)
    new_start, new_plan = sub["current_period_end"], plan
    if sub["pending_plan_id"]:
        new_plan = get_plan(c, sub["pending_plan_id"]) or plan
    end = _next_end(sub, new_plan) if new_plan["billing_interval"] == plan["billing_interval"] else \
        add_interval(parse(new_start), new_plan["billing_interval"], 1)
    c.execute("""UPDATE subscriptions SET plan_id=?, billing_cycle=?, current_period_start=?, current_period_end=?,
                 pending_plan_id=NULL, status='active', paid_period_start=?, updated=? WHERE id=?""",
              (new_plan["id"], new_plan["billing_interval"], new_start, iso(end),
               new_start if paid else sub["paid_period_start"], iso(now()), sub["id"]))
    return _sub(c, sub["id"])


def _to_default(c, sub: dict, status: str, at) -> dict:
    _end(c, sub, status, at)
    return _create(c, sub["user_id"], default_plan(c), at)


def _advance(c, sub: dict, t) -> dict:
    """Apply everything that became due by time t: renewals of free/manual plans, trial ends, cancellations
    at period end, unpaid periods (past_due, then expired after the grace days)."""
    grace = timedelta(days=int(get_settings_all(c)["grace_days"]))
    for _ in range(1000):
        plan = get_plan(c, sub["plan_id"])
        end = parse(sub["current_period_end"])
        if t < end:
            return sub
        if sub["status"] == "trialing":
            sub = _to_default(c, sub, "expired", end)
        elif sub["cancel_at_period_end"]:
            _close_period(c, sub, plan)
            sub = _to_default(c, sub, "canceled", end)
        elif sub["payment_provider"] in ("none", "manual") or plan["price_minor"] == 0:
            sub = _roll(c, sub, plan, paid=True)           # free or admin-assigned: renews without payment
        elif sub["status"] == "active":
            c.execute("UPDATE subscriptions SET status='past_due', updated=? WHERE id=?", (iso(now()), sub["id"]))
            sub = _sub(c, sub["id"])
        elif sub["status"] == "past_due" and t >= end + grace:
            _close_period(c, sub, plan)
            sub = _to_default(c, sub, "expired", end + grace)
        else:
            return sub                                       # past_due within the grace days
    return sub


def current(c, user_id: int) -> dict:
    """The user's current subscription with its plan, after applying any lifecycle change that is due.
    Must run inside a write transaction (it may renew, expire or create the free subscription)."""
    sub = _live(c, user_id)
    if sub is None:
        sub = _create(c, user_id, default_plan(c), now())
    sub = _advance(c, sub, now())
    return {**sub, "plan": get_plan(c, sub["plan_id"])}


def subscription_view(sub: dict, c=None) -> dict:
    plan = sub["plan"]
    out = {k: sub[k] for k in ("id", "status", "billing_cycle", "start_date", "current_period_start",
                                "current_period_end", "payment_provider", "created", "updated")}
    pending = get_plan(c, sub["pending_plan_id"]) if c is not None and sub.get("pending_plan_id") else None
    pub = {k: v for k, v in plan.items() if k != "external_ids"}
    return {**out, "plan": pub, "is_trial": bool(sub["is_trial"]), "renewal_date": sub["current_period_end"],
            "cancel_at_period_end": bool(sub["cancel_at_period_end"]), "is_free": plan["price_minor"] == 0,
            "pending_plan": {"id": pending["id"], "name": pending["name"]} if pending else None,
            "renews": not sub["cancel_at_period_end"] and not sub["is_trial"] and plan["price_minor"] > 0}


def get_subscription(user_id: int) -> dict:
    with transaction() as c:
        sub = current(c, user_id)
        view = subscription_view(sub, c)
        trial_used = c.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND is_trial=1", (user_id,)).fetchone()
        pays = [dict(r) for r in c.execute("""SELECT id, provider, external_payment_id, kind, amount_minor, refunded_minor,
                currency, status, description, created FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 50""", (user_id,))]
        charges = [dict(r) for r in c.execute("""SELECT id, kind, period_start, period_end, tokens, amount_minor, currency,
                   status, created FROM charges WHERE user_id=? ORDER BY id DESC LIMIT 50""", (user_id,))]
        pending = c.execute("""SELECT id, plan_id, status, created FROM checkouts WHERE user_id=? AND status='created'
                               ORDER BY created DESC LIMIT 1""", (user_id,)).fetchone()
    for p in pays:
        p["amount"], p["refunded"] = p.pop("amount_minor") / 100, p.pop("refunded_minor") / 100
    for ch in charges:
        ch["amount"] = ch.pop("amount_minor") / 100
    return {"subscription": view, "trial_available": not trial_used, "payments": pays, "charges": charges,
            "pending_checkout": dict(pending) if pending else None, "provider": _provider_name()}


def _provider_name() -> str:
    from app.core.config import get_settings
    return get_settings().payment_provider


# ------------------------------------------------------------------ user actions
def _target_plan(c, plan_id: int) -> dict:
    plan = get_plan(c, plan_id)
    if plan is None or not plan["is_active"]:
        raise BillingError("This plan is not available.", 404)
    return plan


def start_trial(user: dict, plan_id: int) -> dict:
    with transaction() as c:
        sub, plan = current(c, user["id"]), _target_plan(c, plan_id)
        if plan["trial_days"] <= 0:
            raise BillingError("This plan has no free trial.")
        if c.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND is_trial=1", (user["id"],)).fetchone():
            raise BillingError("You have already used your free trial.")
        if sub["plan"]["price_minor"] > 0:
            raise BillingError("Trials are for accounts on the free plan.")
        _end(c, sub, "canceled", now())
        new = _create(c, user["id"], plan, now(), status="trialing", trial_days=plan["trial_days"])
        return subscription_view({**new, "plan": plan}, c)


def start_checkout(user: dict, plan_id: int, direction: Optional[str] = None) -> dict:
    """Begin paying for a plan. The plan becomes active only after the payment is verified (webhook or a
    server-side check with the provider), never on the browser's word."""
    with transaction() as c:
        sub, plan = current(c, user["id"]), _target_plan(c, plan_id)
        if plan["price_minor"] <= 0:
            raise BillingError("Free plans need no checkout. Use downgrade to move to a free plan.")
        cur_price = sub["plan"]["price_minor"] if not sub["is_trial"] else 0
        if sub["plan_id"] == plan["id"] and not sub["is_trial"] and sub["status"] == "active":
            raise BillingError("You are already on this plan.")
        if direction == "upgrade" and plan["price_minor"] <= cur_price and not sub["is_trial"]:
            raise BillingError("Choose a plan priced above your current plan to upgrade.")
    provider = payments.get_provider()
    checkout_id = secrets.token_hex(12)
    try:
        out = provider.create_checkout(user, plan, checkout_id)
    except payments.PaymentError as e:
        raise BillingError(str(e), 502)
    with transaction() as c:
        c.execute("UPDATE checkouts SET status='abandoned', updated=? WHERE user_id=? AND status='created'",
                  (iso(now()), user["id"]))
        c.execute("INSERT INTO checkouts(id, user_id, plan_id, provider, external_id, status, created, updated) VALUES (?,?,?,?,?,?,?,?)",
                  (checkout_id, user["id"], plan["id"], provider.name, out["external_id"], "created", iso(now()), iso(now())))
    return out["client"]


def verify_checkout(user: dict, checkout_id: str, payload: dict) -> dict:
    """The browser reports a finished checkout: confirm it with the provider before anything changes."""
    with read() as c:
        co = c.execute("SELECT * FROM checkouts WHERE id=? AND user_id=?", (checkout_id, user["id"])).fetchone()
    if co is None:
        raise BillingError("Checkout not found.", 404)
    provider = payments.get_provider(co["provider"])
    try:
        events = provider.verify_checkout(payload, co["external_id"])
    except payments.SignatureError as e:
        raise BillingError(str(e), 400)
    except payments.PaymentError as e:
        raise BillingError(str(e), 402)
    for ev in events:
        process_event(provider, ev)
    return get_subscription(user["id"])


def downgrade(user: dict, plan_id: int) -> dict:
    """Move to a cheaper plan at the end of the paid period (no refunds for the rest of the period)."""
    with transaction() as c:
        sub, plan = current(c, user["id"]), _target_plan(c, plan_id)
        if plan["price_minor"] >= sub["plan"]["price_minor"] and not sub["is_trial"]:
            raise BillingError("Choose a plan priced below your current plan to downgrade.")
        ext, prov = sub["external_subscription_id"], sub["payment_provider"]
    if plan["price_minor"] == 0:
        return cancel(user)
    if ext:
        try:
            payments.get_provider(prov).change_plan(ext, plan)
        except payments.PaymentError as e:
            raise BillingError(str(e), 502)
    with transaction() as c:
        c.execute("UPDATE subscriptions SET pending_plan_id=?, updated=? WHERE id=?", (plan["id"], iso(now()), sub["id"]))
        return subscription_view(current(c, user["id"]), c)


def cancel(user: dict) -> dict:
    with transaction() as c:
        sub = current(c, user["id"])
        if sub["plan"]["price_minor"] == 0:
            raise BillingError("You are on the free plan; there is nothing to cancel.")
        ext, prov = sub["external_subscription_id"], sub["payment_provider"]
    if ext and prov not in ("none", "manual"):
        try:
            payments.get_provider(prov).cancel(ext, at_period_end=True)
        except payments.PaymentError as e:
            raise BillingError(str(e), 502)
    with transaction() as c:
        c.execute("UPDATE subscriptions SET cancel_at_period_end=1, pending_plan_id=NULL, updated=? WHERE id=?",
                  (iso(now()), sub["id"]))
        return subscription_view(current(c, user["id"]), c)


def assign_plan(user_id: int, plan_id: int) -> dict:
    """Admin: put a user on a plan without payment (complimentary, or a deal paid outside the app)."""
    with transaction() as c:
        if c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
            raise BillingError("User not found.", 404)
        sub, plan = current(c, user_id), _target_plan(c, plan_id)
        _end(c, sub, "canceled", now())
        new = _create(c, user_id, plan, now(), provider="manual" if plan["price_minor"] else "none", paid=True)
        return subscription_view({**new, "plan": plan}, c)


# ------------------------------------------------------------------ webhooks and payment events
def handle_webhook(provider_name: str, headers: dict, body: bytes) -> List[dict]:
    provider = payments.get_provider(provider_name)
    events = provider.parse_webhook({k.lower(): v for k, v in headers.items()}, body)   # raises SignatureError
    return [process_event(provider, ev) for ev in events]


def process_event(provider, ev: payments.Event) -> dict:
    """Apply one verified event exactly once: the event id is recorded in the same transaction as its effects,
    so a repeated delivery is ignored and a failed one is retried by the provider."""
    after = []
    with transaction() as c:
        if c.execute("SELECT 1 FROM webhook_events WHERE provider=? AND event_id=?", (provider.name, ev.id)).fetchone():
            return {"event": ev.id, "status": "duplicate"}
        try:
            detail = _apply(c, provider.name, ev, after)
            status = "ignored" if detail.startswith("ignored") else "processed"
        except BillingError as e:
            status, detail = "ignored", str(e)
        c.execute("""INSERT INTO webhook_events(provider, event_id, event_type, raw_type, status, detail, received)
                     VALUES (?,?,?,?,?,?,?)""", (provider.name, ev.id, ev.type, ev.raw_type, status, detail[:300], iso(now())))
    for fn in after:                                    # calls to the provider, outside the database lock
        try:
            fn()
        except Exception:
            log.exception("Payment provider follow-up failed")
    return {"event": ev.id, "status": status, "detail": detail}


def _by_external(c, provider: str, ext: Optional[str]) -> Optional[dict]:
    if not ext:
        return None
    r = c.execute("SELECT * FROM subscriptions WHERE payment_provider=? AND external_subscription_id=?", (provider, ext)).fetchone()
    return dict(r) if r else None


def _activate(c, provider: str, ext: str, paid: bool, after: list) -> dict:
    """First verified event for a checkout: the new plan replaces the current subscription from now."""
    existing = _by_external(c, provider, ext)
    if existing:
        return existing
    co = c.execute("SELECT * FROM checkouts WHERE provider=? AND external_id=?", (provider, ext)).fetchone()
    if co is None:
        raise BillingError(f"ignored: no checkout for subscription {ext}")
    plan = get_plan(c, co["plan_id"])
    old = current(c, co["user_id"])
    _end(c, old, "canceled", now())
    if old["external_subscription_id"] and old["payment_provider"] not in ("none", "manual"):
        prov, old_ext = old["payment_provider"], old["external_subscription_id"]
        after.append(lambda: payments.get_provider(prov).cancel(old_ext, at_period_end=False))
    c.execute("UPDATE checkouts SET status='completed', updated=? WHERE id=?", (iso(now()), co["id"]))
    return _create(c, co["user_id"], plan, now(), provider=provider, external_id=ext, paid=paid)


def _record_payment(c, provider: str, ev: payments.Event, sub: Optional[dict], status: str) -> bool:
    """Store a payment once (by provider payment id). Returns False when it was already recorded."""
    if not ev.external_payment_id:
        return True
    user_id = sub["user_id"] if sub else None
    if user_id is None:                                  # e.g. the first payment of a checkout failed
        co = c.execute("SELECT user_id FROM checkouts WHERE provider=? AND external_id=?",
                       (provider, ev.external_subscription_id)).fetchone()
        user_id = co["user_id"] if co else None
    cur = c.execute("""INSERT OR IGNORE INTO payments(user_id, subscription_id, provider, external_payment_id, kind,
                       amount_minor, currency, status, description, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (user_id, sub["id"] if sub else None, provider, ev.external_payment_id,
                     "subscription", ev.amount_minor or 0, (ev.currency or "INR").upper(), status,
                     ev.raw_type, iso(now()), iso(now())))
    return cur.rowcount == 1


def _apply(c, provider: str, ev: payments.Event, after: list) -> str:
    sub = _by_external(c, provider, ev.external_subscription_id)
    if ev.type == "subscription.activated":
        sub = _activate(c, provider, ev.external_subscription_id, paid=False, after=after)
        return f"subscription {sub['id']} active"
    if ev.type == "payment.success":
        if ev.external_payment_id and c.execute("SELECT 1 FROM payments WHERE provider=? AND external_payment_id=?",
                                                (provider, ev.external_payment_id)).fetchone():
            return "ignored: payment already recorded"
        if sub is None:
            sub = _activate(c, provider, ev.external_subscription_id, paid=True, after=after)
        elif sub["status"] in LIVE:
            plan = get_plan(c, sub["plan_id"])
            if sub["paid_period_start"] == sub["current_period_start"]:
                sub = _roll(c, sub, plan, paid=True)                       # renewal: next period
            else:
                c.execute("UPDATE subscriptions SET paid_period_start=current_period_start, status='active', updated=? WHERE id=?",
                          (iso(now()), sub["id"]))
                sub = _sub(c, sub["id"])
        _record_payment(c, provider, ev, sub, "captured")
        return f"payment recorded, subscription {sub['id']} paid to {sub['current_period_end']}"
    if ev.type == "payment.failed":
        _record_payment(c, provider, ev, sub, "failed")
        if sub and sub["status"] == "active":
            c.execute("UPDATE subscriptions SET status='past_due', updated=? WHERE id=?", (iso(now()), sub["id"]))
        return "payment failed" + (f", subscription {sub['id']} past due" if sub else "")
    if ev.type == "refund.created":
        r = c.execute("SELECT * FROM payments WHERE provider=? AND external_payment_id=?", (provider, ev.external_payment_id)).fetchone()
        if r is None:
            return "ignored: refund for an unknown payment"
        refunded = min(r["amount_minor"], r["refunded_minor"] + (ev.amount_minor or r["amount_minor"]))
        c.execute("UPDATE payments SET refunded_minor=?, status=?, updated=? WHERE id=?",
                  (refunded, "refunded" if refunded >= r["amount_minor"] else "partially_refunded", iso(now()), r["id"]))
        return f"refund of {(ev.amount_minor or 0) / 100:.2f} recorded"
    if sub is None:
        return f"ignored: {ev.type} for an unknown subscription"
    if sub["status"] not in LIVE:
        return f"ignored: subscription {sub['id']} already {sub['status']}"
    if ev.type == "subscription.past_due":
        c.execute("UPDATE subscriptions SET status='past_due', updated=? WHERE id=?", (iso(now()), sub["id"]))
        return f"subscription {sub['id']} past due"
    if ev.type == "subscription.canceled":
        c.execute("UPDATE subscriptions SET cancel_at_period_end=1, updated=? WHERE id=?", (iso(now()), sub["id"]))
        return f"subscription {sub['id']} ends at the end of the paid period"
    if ev.type == "subscription.expired":
        _to_default(c, sub, "expired", now())
        return f"subscription {sub['id']} expired, moved to the free plan"
    if ev.type == "subscription.updated" and ev.external_plan_id:
        for p in c.execute("SELECT * FROM plans"):
            if json.loads(p["external_ids"] or "{}").get(provider) == ev.external_plan_id and p["id"] != sub["plan_id"]:
                c.execute("UPDATE subscriptions SET plan_id=?, pending_plan_id=NULL, updated=? WHERE id=?",
                          (p["id"], iso(now()), sub["id"]))
                return f"subscription {sub['id']} moved to {p['name']}"
        return "ignored: plan unchanged"
    return f"ignored: {ev.type}"


# ------------------------------------------------------------------ development: simulated payments (mock provider)
def mock_complete(user: dict, checkout_id: str, outcome: str) -> dict:
    with read() as c:
        co = c.execute("SELECT * FROM checkouts WHERE id=? AND user_id=? AND provider='mock'", (checkout_id, user["id"])).fetchone()
        plan = get_plan(c, co["plan_id"]) if co else None
    if co is None:
        raise BillingError("Checkout not found.", 404)
    mock = payments.get_provider("mock")
    if outcome == "success":
        evs = [mock.event("subscription.activated", subscription_id=co["external_id"]),
               mock.event("payment.success", subscription_id=co["external_id"], payment_id=f"pay_mock_{secrets.token_hex(6)}",
                          amount_minor=plan["price_minor"], currency=plan["currency"])]
    else:
        evs = [mock.event("payment.failed", subscription_id=co["external_id"], payment_id=f"pay_mock_{secrets.token_hex(6)}",
                          amount_minor=plan["price_minor"], currency=plan["currency"])]
        with transaction() as c:
            c.execute("UPDATE checkouts SET status='failed', updated=? WHERE id=?", (iso(now()), checkout_id))
    for headers, body in evs:
        handle_webhook("mock", headers, body)
    return get_subscription(user["id"])


def mock_event(subscription_id: int, kind: str) -> List[dict]:
    """Admin, development only: send a signed test webhook for a mock subscription (renewal, failed payment,
    cancellation or expiry) through the normal webhook path."""
    with read() as c:
        sub = c.execute("SELECT * FROM subscriptions WHERE id=? AND payment_provider='mock'", (subscription_id,)).fetchone()
        plan = get_plan(c, sub["plan_id"]) if sub else None
    if sub is None:
        raise BillingError("Mock subscription not found.", 404)
    mock = payments.get_provider("mock")
    pay = {"payment_id": f"pay_mock_{secrets.token_hex(6)}", "amount_minor": plan["price_minor"], "currency": plan["currency"]}
    types = {"renew": ("payment.success", pay), "fail": ("payment.failed", pay), "cancel": ("subscription.canceled", {}),
             "expire": ("subscription.expired", {})}
    if kind not in types:
        raise BillingError("Unknown test event.")
    t, extra = types[kind]
    headers, body = mock.event(t, subscription_id=sub["external_subscription_id"], **extra)
    return handle_webhook("mock", headers, body)


# ------------------------------------------------------------------ admin reports
def admin_payments(limit: int = 100) -> List[dict]:
    with read() as c:
        rows = c.execute("""SELECT p.*, u.email, u.name FROM payments p LEFT JOIN users u ON u.id=p.user_id
                            ORDER BY p.id DESC LIMIT ?""", (limit,)).fetchall()
    return [{**dict(r), "amount": r["amount_minor"] / 100, "refunded": r["refunded_minor"] / 100} for r in rows]


def admin_webhooks(limit: int = 100) -> List[dict]:
    with read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM webhook_events ORDER BY id DESC LIMIT ?", (limit,))]


def admin_charges(status: Optional[str] = None) -> List[dict]:
    with read() as c:
        rows = c.execute("""SELECT ch.*, u.email, u.name FROM charges ch LEFT JOIN users u ON u.id=ch.user_id"""
                         + (" WHERE ch.status=?" if status else "") + " ORDER BY ch.id DESC LIMIT 200",
                         (status,) if status else ()).fetchall()
    return [{**dict(r), "amount": r["amount_minor"] / 100} for r in rows]


def set_charge_status(charge_id: int, status: str) -> dict:
    if status not in ("due", "paid", "waived"):
        raise BillingError("Status must be due, paid or waived.")
    with transaction() as c:
        if c.execute("UPDATE charges SET status=?, updated=? WHERE id=?", (status, iso(now()), charge_id)).rowcount == 0:
            raise BillingError("Charge not found.", 404)
        return dict(c.execute("SELECT * FROM charges WHERE id=?", (charge_id,)).fetchone())


def user_subscriptions(user_id: int) -> List[dict]:
    with read() as c:
        rows = c.execute("""SELECT s.*, p.name AS plan_name FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                            WHERE s.user_id=? ORDER BY s.id DESC LIMIT 50""", (user_id,)).fetchall()
    return [dict(r) for r in rows]
