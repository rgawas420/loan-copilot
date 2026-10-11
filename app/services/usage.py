"""Usage service: the token ledger, quota reservations, provider cost and usage reports.

Source of truth: usage_ledger, one immutable row per LLM call (database triggers refuse UPDATE and DELETE). Each
row stores the token counts the provider reported (usage_source='provider'); a row is estimated only when the
provider returned no usage, e.g. a stream the user stopped before the provider's final usage chunk. Rows keep
their billing period, so a new period starts at zero while every old row stays for reporting and audit.

usage_totals is a per-period counter updated in the same transaction as each ledger row (for fast quota checks);
rebuild_totals() recomputes it from the ledger.

Quota is enforced with reservations (ai_requests), not with "if used < limit":

    reserve()  BEGIN IMMEDIATE: rate limit, concurrency, request count, then
               used + still-reserved + this estimate <= limit -> insert a reservation
    (LLM calls record ledger rows, each one lowering the reservation still held)
    settle()   the request ends: the reservation is released and only actual usage remains

The write lock is taken before the checks, so two simultaneous requests cannot both see the same remaining
allowance. A reservation that is never settled (crash) expires after USAGE_RESERVATION_TTL_MIN.
"""
import math
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional

from app.core.config import get_settings
from app.services import billing
from app.services.db import iso, now, parse, read, transaction

CHARS_PER_TOKEN = 3.5          # only for reservations and for streams that ended before the provider's usage
COMPONENTS = ("system", "history", "context", "question", "other")

# Provider list prices (USD per 1M tokens) on first run; admins update them in the console. Verify against the
# provider's current price list.
SEED_PRICES = [("groq", "openai/gpt-oss-20b", 0.075, 0.30), ("groq", "openai/gpt-oss-120b", 0.15, 0.60)]


class QuotaError(Exception):
    """Request refused before calling the model. status is the HTTP status; code is machine-readable."""

    def __init__(self, status: int, code: str, message: str, **details):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


@dataclass
class Reservation:
    id: str
    user_id: Optional[int]
    feature: str
    reserved: int
    subscription_id: Optional[int] = None
    plan: dict = field(default_factory=dict)
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None


def estimate_tokens(chars: int) -> int:
    return max(0, math.ceil(chars / CHARS_PER_TOKEN))


# ------------------------------------------------------------------ schema
def init_db():
    with read() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS usage_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
            user_id INTEGER, subscription_id INTEGER, plan_id INTEGER, conversation_id TEXT, message_id TEXT,
            feature TEXT NOT NULL, purpose TEXT, provider TEXT NOT NULL, model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
            system_tokens INTEGER, history_tokens INTEGER, context_tokens INTEGER, question_tokens INTEGER,
            other_tokens INTEGER, usage_source TEXT NOT NULL, input_cost REAL NOT NULL DEFAULT 0,
            output_cost REAL NOT NULL DEFAULT 0, total_cost REAL NOT NULL DEFAULT 0, cost_currency TEXT,
            price_id INTEGER, overage_tokens INTEGER NOT NULL DEFAULT 0, billing_period_start TEXT,
            billing_period_end TEXT, request_status TEXT NOT NULL, created TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS usage_ledger_user ON usage_ledger(user_id, created);
        CREATE INDEX IF NOT EXISTS usage_ledger_period ON usage_ledger(subscription_id, billing_period_start);
        CREATE INDEX IF NOT EXISTS usage_ledger_created ON usage_ledger(created);
        CREATE INDEX IF NOT EXISTS usage_ledger_request ON usage_ledger(request_id);   -- settle(), once per request
        CREATE TRIGGER IF NOT EXISTS usage_ledger_no_update BEFORE UPDATE ON usage_ledger
            BEGIN SELECT RAISE(ABORT, 'usage_ledger is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS usage_ledger_no_delete BEFORE DELETE ON usage_ledger
            BEGIN SELECT RAISE(ABORT, 'usage_ledger is append-only'); END;
        CREATE TABLE IF NOT EXISTS usage_totals(subscription_id INTEGER NOT NULL, period_start TEXT NOT NULL,
            user_id INTEGER, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0, overage_tokens INTEGER NOT NULL DEFAULT 0,
            cost REAL NOT NULL DEFAULT 0, PRIMARY KEY(subscription_id, period_start));
        CREATE TABLE IF NOT EXISTS ai_requests(id TEXT PRIMARY KEY, user_id INTEGER, subscription_id INTEGER,
            plan_id INTEGER, conversation_id TEXT, message_id TEXT, feature TEXT NOT NULL, period_start TEXT,
            period_end TEXT, reserved_tokens INTEGER NOT NULL, actual_tokens INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL, created TEXT NOT NULL, finished TEXT, expires TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ai_requests_user ON ai_requests(user_id, created);
        CREATE INDEX IF NOT EXISTS ai_requests_open ON ai_requests(status, expires);
        CREATE INDEX IF NOT EXISTS ai_requests_period ON ai_requests(subscription_id, period_start);
        CREATE TABLE IF NOT EXISTS model_prices(id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
            model TEXT NOT NULL, input_per_1m REAL NOT NULL, output_per_1m REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD', effective_from TEXT NOT NULL, effective_to TEXT, created TEXT);
        """)
    with transaction() as c:
        if c.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0] == 0:
            for prov, model, i, o in SEED_PRICES:
                c.execute("""INSERT INTO model_prices(provider, model, input_per_1m, output_per_1m, currency,
                             effective_from, created) VALUES (?,?,?,?,?,?,?)""",
                          (prov, model, i, o, "USD", "2020-01-01T00:00:00+00:00", iso(now())))


# ------------------------------------------------------------------ prices and cost
def price_for(c, provider: str, model: str, at: str):
    return c.execute("""SELECT * FROM model_prices WHERE provider=? AND model=? AND effective_from<=?
                        AND (effective_to IS NULL OR effective_to>?) ORDER BY effective_from DESC LIMIT 1""",
                     (provider, model, at, at)).fetchone()


def cost(price, input_tokens: int, output_tokens: int):
    if price is None:
        return 0.0, 0.0, 0.0
    i = input_tokens / 1_000_000 * price["input_per_1m"]
    o = output_tokens / 1_000_000 * price["output_per_1m"]
    return round(i, 8), round(o, 8), round(i + o, 8)


def list_prices() -> List[dict]:
    with read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM model_prices ORDER BY provider, model, effective_from DESC")]


def set_price(provider: str, model: str, input_per_1m: float, output_per_1m: float, currency: str = "USD") -> dict:
    """A new price applies from now; the previous one is closed, so old ledger rows keep the cost they had."""
    if input_per_1m < 0 or output_per_1m < 0:
        raise billing.BillingError("Prices cannot be negative.")
    t = iso(now())
    with transaction() as c:
        c.execute("UPDATE model_prices SET effective_to=? WHERE provider=? AND model=? AND effective_to IS NULL",
                  (t, provider, model))
        cur = c.execute("""INSERT INTO model_prices(provider, model, input_per_1m, output_per_1m, currency,
                           effective_from, created) VALUES (?,?,?,?,?,?,?)""",
                        (provider.strip(), model.strip(), input_per_1m, output_per_1m, currency.upper(), t, t))
        return dict(c.execute("SELECT * FROM model_prices WHERE id=?", (cur.lastrowid,)).fetchone())


# ------------------------------------------------------------------ reservations
def _period_used(c, sub_id: int, period_start: str) -> dict:
    r = c.execute("SELECT * FROM usage_totals WHERE subscription_id=? AND period_start=?", (sub_id, period_start)).fetchone()
    return dict(r) if r else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "overage_tokens": 0, "cost": 0.0}


def _held(c, where: str, args: tuple) -> int:
    """Tokens still held by open reservations (reserved minus what their calls have already recorded)."""
    return c.execute(f"SELECT COALESCE(SUM(MAX(reserved_tokens - actual_tokens, 0)), 0) FROM ai_requests "
                     f"WHERE status='reserved' AND {where}", args).fetchone()[0]


def reserve(user: Optional[dict], feature: str, input_chars: int = 0, conversation_id: Optional[str] = None,
            message_id: Optional[str] = None) -> Reservation:
    """Atomically check limits and reserve the tokens this request may use. Raises QuotaError."""
    s, t = get_settings(), now()
    overhead = {"chat": s.usage_estimate_chat, "recommend": s.usage_estimate_recommend,
                "intake": s.usage_estimate_intake}.get(feature, s.usage_estimate_chat)
    user_tokens = estimate_tokens(input_chars)
    rid = secrets.token_hex(12)
    with transaction() as c:
        c.execute("UPDATE ai_requests SET status='expired', finished=? WHERE status='reserved' AND expires<?",
                  (iso(t), iso(t)))
        if user is None:
            return _reserve_anonymous(c, rid, feature, overhead + user_tokens + 1200, t)
        sub = billing.current(c, user["id"])
        plan = sub["plan"]
        if user_tokens > plan["max_context_tokens"]:
            raise QuotaError(413, "request_too_large", f"This request is too large for the {plan['name']} plan "
                             f"(about {user_tokens:,} tokens; the limit is {plan['max_context_tokens']:,}). "
                             "Shorten the question or attach fewer files.", plan=plan["name"])
        minute_ago = iso(t - timedelta(seconds=60))
        if c.execute("SELECT COUNT(*) FROM ai_requests WHERE user_id=? AND created>?",
                     (user["id"], minute_ago)).fetchone()[0] >= plan["requests_per_minute"]:
            raise QuotaError(429, "rate_limited", f"Too many requests. The {plan['name']} plan allows "
                             f"{plan['requests_per_minute']} AI requests a minute. Wait a moment and try again.")
        if c.execute("SELECT COUNT(*) FROM ai_requests WHERE user_id=? AND status='reserved'",
                     (user["id"],)).fetchone()[0] >= plan["max_concurrent"]:
            raise QuotaError(429, "too_many_concurrent", f"You already have {plan['max_concurrent']} AI request"
                             f"{'' if plan['max_concurrent'] == 1 else 's'} running. Wait for "
                             f"{'it' if plan['max_concurrent'] == 1 else 'one'} to finish.")
        period = (sub["id"], sub["current_period_start"])
        if plan["max_requests"] and c.execute(
                "SELECT COUNT(*) FROM ai_requests WHERE subscription_id=? AND period_start=? AND status IN ('reserved','completed')",
                period).fetchone()[0] >= plan["max_requests"]:
            raise QuotaError(402, "request_limit", f"You have used all {plan['max_requests']:,} AI requests in this "
                             f"billing period of the {plan['name']} plan.", **_limit_details(sub, None, c))
        estimate = overhead + user_tokens + plan["max_output_tokens"]
        used = _period_used(c, *period)["total_tokens"]
        held = _held(c, "subscription_id=? AND period_start=?", period)
        limit = plan["token_limit"]
        if used + held + estimate > limit:
            cap = limit + plan["max_overage_tokens"] if plan["max_overage_tokens"] else None
            if plan["allow_overage"] and (cap is None or used + held + estimate <= cap):
                pass                                                   # billed as overage at the period's end
            elif used >= limit or (plan["allow_overage"] and cap is not None and used >= cap):
                raise QuotaError(402, "quota_exceeded", "You've reached your AI usage limit for this billing period.",
                                 **_limit_details(sub, used, c))
            elif billing.get_settings_all(c)["near_limit_policy"] == "block" or used + held >= limit:
                raise QuotaError(402, "insufficient_quota", f"This request may need about {estimate:,} tokens, more "
                                 f"than the {max(limit - used - held, 0):,} left in your allowance.",
                                 **_limit_details(sub, used, c))
        c.execute("""INSERT INTO ai_requests(id, user_id, subscription_id, plan_id, conversation_id, message_id, feature,
                     period_start, period_end, reserved_tokens, status, created, expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (rid, user["id"], sub["id"], plan["id"], conversation_id, message_id, feature, sub["current_period_start"],
                   sub["current_period_end"], estimate, "reserved", iso(t),
                   iso(t + timedelta(minutes=s.usage_reservation_ttl_min))))
    return Reservation(rid, user["id"], feature, estimate, sub["id"], plan, sub["current_period_start"],
                       sub["current_period_end"], conversation_id, message_id)


def _reserve_anonymous(c, rid: str, feature: str, estimate: int, t) -> Reservation:
    cfg = billing.get_settings_all(c)
    budget = int(cfg["anonymous_daily_tokens"])
    if budget <= 0:
        raise QuotaError(401, "login_required", "Log in to use AI features.")
    if c.execute("SELECT COUNT(*) FROM ai_requests WHERE user_id IS NULL AND status='reserved'").fetchone()[0] \
            >= int(cfg["anonymous_max_concurrent"]):
        raise QuotaError(429, "too_many_concurrent", "The service is busy. Try again in a moment.")
    day = iso(t.replace(hour=0, minute=0, second=0))
    used = c.execute("""SELECT COALESCE(SUM(total_tokens), 0) FROM usage_ledger WHERE user_id IS NULL
                        AND feature<>'system' AND created>=?""", (day,)).fetchone()[0]
    if used + _held(c, "user_id IS NULL", ()) + estimate > budget:
        raise QuotaError(402, "quota_exceeded", "Today's AI allowance for use without an account is used up. "
                         "Log in to continue.", limit=budget, used=used)
    c.execute("""INSERT INTO ai_requests(id, user_id, feature, reserved_tokens, status, created, expires)
                 VALUES (?,?,?,?,?,?,?)""",
              (rid, None, feature, estimate, "reserved", iso(t),
               iso(t + timedelta(minutes=get_settings().usage_reservation_ttl_min))))
    return Reservation(rid, None, feature, estimate)


def settle(res: Reservation, status: str = "completed") -> dict:
    """Release the reservation: from now on only the recorded (actual) usage counts. Idempotent."""
    with transaction() as c:
        c.execute("UPDATE ai_requests SET status=?, finished=? WHERE id=? AND status='reserved'",
                  (status, iso(now()), res.id))
        r = c.execute("""SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,
                         COALESCE(SUM(total_tokens),0) t, COUNT(*) n FROM usage_ledger WHERE request_id=?""",
                      (res.id,)).fetchone()
    return {"input_tokens": r["i"], "output_tokens": r["o"], "total_tokens": r["t"], "calls": r["n"]}


# ------------------------------------------------------------------ ledger
def split_input(input_tokens: int, chars: Optional[Dict[str, int]]) -> Dict[str, Optional[int]]:
    """Share the provider-reported input tokens between prompt components in proportion to their length.
    The total is exact; the split is an estimate (providers report one input count)."""
    if not chars or sum(chars.values()) <= 0:
        return {k: None for k in COMPONENTS}
    total = sum(chars.values())
    out = {k: int(input_tokens * chars.get(k, 0) / total) for k in COMPONENTS}
    biggest = max(COMPONENTS, key=lambda k: chars.get(k, 0))
    out[biggest] += input_tokens - sum(out.values())
    return out


def record(res: Optional[Reservation], *, provider: str, model: str, input_tokens: int, output_tokens: int,
           total_tokens: Optional[int] = None, source: str = "provider", purpose: str = "", status: str = "completed",
           breakdown: Optional[Dict[str, int]] = None, user_id: Optional[int] = None) -> dict:
    """Append one LLM call to the ledger, with its cost, and update the period counter (one transaction)."""
    input_tokens, output_tokens = max(0, int(input_tokens)), max(0, int(output_tokens))
    total = int(total_tokens) if total_tokens is not None else input_tokens + output_tokens
    t = iso(now())
    parts = split_input(input_tokens, breakdown)
    with transaction() as c:
        price = price_for(c, provider, model, t)
        ic, oc, tc = cost(price, input_tokens, output_tokens)
        overage = 0
        sub_id = res.subscription_id if res else None
        if res and sub_id:
            before = _period_used(c, sub_id, res.period_start)["total_tokens"]
            limit = res.plan["token_limit"]
            overage = max(0, before + total - limit) - max(0, before - limit)
            c.execute("""INSERT INTO usage_totals(subscription_id, period_start, user_id, input_tokens, output_tokens,
                         total_tokens, overage_tokens, cost) VALUES (?,?,?,?,?,?,?,?)
                         ON CONFLICT(subscription_id, period_start) DO UPDATE SET
                         input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens,
                         total_tokens=total_tokens+excluded.total_tokens, overage_tokens=overage_tokens+excluded.overage_tokens,
                         cost=cost+excluded.cost""",
                      (sub_id, res.period_start, res.user_id, input_tokens, output_tokens, total, overage, tc))
        if res:
            c.execute("UPDATE ai_requests SET actual_tokens=actual_tokens+? WHERE id=?", (total, res.id))
        c.execute("""INSERT INTO usage_ledger(request_id, user_id, subscription_id, plan_id, conversation_id, message_id,
                     feature, purpose, provider, model, input_tokens, output_tokens, total_tokens, system_tokens,
                     history_tokens, context_tokens, question_tokens, other_tokens, usage_source, input_cost,
                     output_cost, total_cost, cost_currency, price_id, overage_tokens, billing_period_start,
                     billing_period_end, request_status, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (res.id if res else None, res.user_id if res else user_id, sub_id,
                   res.plan.get("id") if res and res.plan else None, res.conversation_id if res else None,
                   res.message_id if res else None, res.feature if res else "system", purpose, provider, model,
                   input_tokens, output_tokens, total, parts["system"], parts["history"], parts["context"],
                   parts["question"], parts["other"], source, ic, oc, tc, price["currency"] if price else None,
                   price["id"] if price else None, overage, res.period_start if res else None,
                   res.period_end if res else None, status, t))
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total, "cost": tc}


def rebuild_totals() -> int:
    """Recompute every period counter from the ledger (the ledger is the source of truth)."""
    with transaction() as c:
        c.execute("DELETE FROM usage_totals")
        rows = c.execute("""SELECT l.subscription_id, l.billing_period_start, l.user_id, l.total_tokens, l.input_tokens,
                            l.output_tokens, l.total_cost, p.token_limit FROM usage_ledger l LEFT JOIN plans p ON p.id=l.plan_id
                            WHERE l.subscription_id IS NOT NULL ORDER BY l.id""").fetchall()
        acc: Dict[tuple, dict] = {}
        for r in rows:
            a = acc.setdefault((r["subscription_id"], r["billing_period_start"]),
                               {"user_id": r["user_id"], "i": 0, "o": 0, "t": 0, "ov": 0, "cost": 0.0})
            limit = r["token_limit"] or 0
            a["ov"] += max(0, a["t"] + r["total_tokens"] - limit) - max(0, a["t"] - limit)
            a["i"] += r["input_tokens"]; a["o"] += r["output_tokens"]; a["t"] += r["total_tokens"]; a["cost"] += r["total_cost"]
        for (sid, ps), a in acc.items():
            c.execute("""INSERT INTO usage_totals(subscription_id, period_start, user_id, input_tokens, output_tokens,
                         total_tokens, overage_tokens, cost) VALUES (?,?,?,?,?,?,?,?)""",
                      (sid, ps, a["user_id"], a["i"], a["o"], a["t"], a["ov"], a["cost"]))
        return len(acc)


# ------------------------------------------------------------------ what users see
def _warning(pct: float, cfg: dict, allow_overage: bool) -> Optional[dict]:
    if pct >= 100:
        return {"level": "overage" if allow_overage else "blocked", "threshold": 100,
                "message": "You've used your full AI token allowance for this billing period."
                           + (" Further usage is billed as overage." if allow_overage else "")}
    reached = [t for t in cfg["warn_thresholds"] if pct >= t]
    if not reached:
        return None
    top = max(reached)
    return {"level": "strong" if top == max(cfg["warn_thresholds"]) else "warning", "threshold": top,
            "message": f"You have used {top}% of your AI token allowance for this billing period."}


def _limit_details(sub: dict, used: Optional[int], c) -> dict:
    plan = sub["plan"]
    if used is None:
        used = _period_used(c, sub["id"], sub["current_period_start"])["total_tokens"]
    return {"plan": plan["name"], "limit": plan["token_limit"], "used": used,
            "period_end": sub["current_period_end"], "billing_interval": plan["billing_interval"]}


def summary(c, sub: dict) -> dict:
    plan = sub["plan"]
    period = (sub["id"], sub["current_period_start"])
    tot = _period_used(c, *period)
    held = _held(c, "subscription_id=? AND period_start=?", period)
    requests = c.execute("""SELECT COUNT(*) FROM ai_requests WHERE subscription_id=? AND period_start=?
                            AND status IN ('reserved','completed','stopped','failed') AND actual_tokens>0""", period).fetchone()[0]
    limit, used = plan["token_limit"], tot["total_tokens"]
    pct = round(used / limit * 100, 2) if limit else 100.0
    cfg = billing.get_settings_all(c)
    overage_amount = -(-tot["overage_tokens"] * plan["overage_price_per_1k_minor"] // 1000) / 100 if plan["allow_overage"] else 0
    blocked = used >= limit and not (plan["allow_overage"] and (not plan["max_overage_tokens"] or used < limit + plan["max_overage_tokens"]))
    return {"plan": plan["name"], "plan_id": plan["id"], "plan_code": plan["code"], "status": sub["status"],
            "is_trial": bool(sub["is_trial"]), "is_free": plan["price_minor"] == 0,
            "period_start": sub["current_period_start"], "period_end": sub["current_period_end"],
            "renewal_date": sub["current_period_end"], "cancel_at_period_end": bool(sub["cancel_at_period_end"]),
            "limit": limit, "used": used, "in_progress": held, "remaining": max(limit - used, 0), "percentage": pct,
            "input_tokens": tot["input_tokens"], "output_tokens": tot["output_tokens"], "requests": requests,
            "max_requests": plan["max_requests"], "allow_overage": plan["allow_overage"],
            "overage_tokens": tot["overage_tokens"] if plan["allow_overage"] else 0,
            "overage_price_per_1k": plan["overage_price_per_1k"], "overage_amount": overage_amount,
            "currency": plan["currency"], "warning": _warning(pct, cfg, plan["allow_overage"]), "blocked": blocked,
            "thresholds": cfg["warn_thresholds"]}


def current_usage(user_id: int) -> dict:
    with transaction() as c:
        return summary(c, billing.current(c, user_id))


def _range(c, user_id: int, rng: str):
    if rng == "period":
        sub = billing.current(c, user_id)
        return sub["current_period_start"], None
    days = {"7d": 7, "30d": 30, "90d": 90}.get(rng, 30)
    return iso(now().replace(hour=0, minute=0, second=0) - timedelta(days=days - 1)), None


def daily(user_id: Optional[int], rng: str = "30d") -> dict:
    with transaction() as c:
        start, _ = _range(c, user_id, rng) if user_id else (iso(now().replace(hour=0, minute=0, second=0) - timedelta(days=29)), None)
        where, args = ("user_id=? AND " if user_id else "") + "created>=?", ((user_id,) if user_id else ()) + (start,)
        rows = c.execute(f"""SELECT substr(created,1,10) day, SUM(input_tokens) i, SUM(output_tokens) o, SUM(total_tokens) t,
                             SUM(total_cost) cost FROM usage_ledger WHERE {where} GROUP BY day ORDER BY day""", args).fetchall()
    by_day = {r["day"]: r for r in rows}
    out, d, end = [], parse(start).date(), now().date()
    while d <= end:
        r = by_day.get(d.isoformat())
        out.append({"date": d.isoformat(), "input_tokens": r["i"] if r else 0, "output_tokens": r["o"] if r else 0,
                    "total_tokens": r["t"] if r else 0, **({"cost": round(r["cost"], 6) if r else 0} if not user_id else {})})
        d += timedelta(days=1)
    return {"range": rng, "start": start, "days": out}


def by_model(user_id: int, rng: str = "period") -> dict:
    with transaction() as c:
        start, _ = _range(c, user_id, rng)
        rows = c.execute("""SELECT model, SUM(input_tokens) i, SUM(output_tokens) o, SUM(total_tokens) t, COUNT(*) calls
                            FROM usage_ledger WHERE user_id=? AND created>=? GROUP BY model ORDER BY t DESC""",
                         (user_id, start)).fetchall()
    return {"range": rng, "start": start, "models": [{"model": r["model"], "input_tokens": r["i"], "output_tokens": r["o"],
                                                      "total_tokens": r["t"], "calls": r["calls"]} for r in rows]}


PURPOSES = {"answer": "Answer", "answer_check": "Checking an answer", "query_rewrite": "Search preparation", "intake": "Reading a case note",
            "explain": "Explaining a match", "grade_kb": "Checking evidence", "rewrite_query": "Search preparation",
            "recommend_from_text": "Recommendation from documents", "web_guidance": "General guidance", "": "AI call"}
FEATURES = {"chat": "Question", "recommend": "Lender match", "intake": "Case description", "system": "System"}


def history(user_id: Optional[int] = None, *, admin: bool = False, limit: int = 50, offset: int = 0,
            date_from: Optional[str] = None, date_to: Optional[str] = None, model: Optional[str] = None,
            provider: Optional[str] = None, conversation_id: Optional[str] = None,
            subscription_id: Optional[int] = None, feature: Optional[str] = None) -> dict:
    """Ledger rows, newest first. Users get their own rows without cost or prompt breakdown."""
    where, args = [], []
    for col, val in (("l.user_id", user_id), ("l.model", model), ("l.provider", provider),
                     ("l.conversation_id", conversation_id), ("l.subscription_id", subscription_id), ("l.feature", feature)):
        if val not in (None, ""):
            where.append(f"{col}=?")
            args.append(val)
    if date_from:
        where.append("l.created>=?"); args.append(date_from[:10] + "T00:00:00+00:00")
    if date_to:
        where.append("l.created<?"); args.append(iso(parse(date_to[:10] + "T00:00:00+00:00") + timedelta(days=1)))
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    with read() as c:
        total = c.execute(f"""SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,
                              COALESCE(SUM(total_tokens),0) t, COALESCE(SUM(total_cost),0) cost
                              FROM usage_ledger l{sql_where}""", args).fetchone()
        rows = c.execute(f"""SELECT l.*, u.email, u.name AS user_name, p.name AS plan_name FROM usage_ledger l
                             LEFT JOIN users u ON u.id=l.user_id LEFT JOIN plans p ON p.id=l.plan_id{sql_where}
                             ORDER BY l.id DESC LIMIT ? OFFSET ?""", (*args, max(1, min(limit, 500)), max(0, offset))).fetchall()
    user_cols = ("id", "created", "feature", "purpose", "model", "input_tokens", "output_tokens", "total_tokens",
                 "conversation_id", "request_status", "usage_source")
    items = []
    for r in rows:
        d = dict(r) if admin else {k: r[k] for k in user_cols}
        d["feature_label"] = FEATURES.get(r["feature"], r["feature"])
        d["purpose_label"] = PURPOSES.get(r["purpose"] or "", r["purpose"])
        items.append(d)
    totals = {"count": total["n"], "input_tokens": total["i"], "output_tokens": total["o"], "total_tokens": total["t"]}
    if admin:
        totals["cost"] = round(total["cost"], 6)
    return {"items": items, "totals": totals, "limit": limit, "offset": offset}


# ------------------------------------------------------------------ admin reports
def admin_overview() -> dict:
    t = now()
    month = iso(t.replace(day=1, hour=0, minute=0, second=0))
    with transaction() as c:
        cfg = billing.get_settings_all(c)
        fx = float(cfg["usd_to_inr"])
        users = c.execute("SELECT id FROM users").fetchall()
        for u in users:                                        # apply due renewals / expiries before counting
            billing.current(c, u["id"])
        one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        subs = c.execute("""SELECT s.status, s.is_trial, s.payment_provider, p.price_minor, p.billing_interval FROM subscriptions s
                            JOIN plans p ON p.id=s.plan_id WHERE s.status IN ('trialing','active','past_due')""").fetchall()
        paid = [s for s in subs if s["price_minor"] > 0 and not s["is_trial"]]
        mrr = sum(s["price_minor"] / (12 if s["billing_interval"] == "yearly" else 1) for s in paid
                  if s["payment_provider"] not in ("manual", "none"))
        led = c.execute("""SELECT COALESCE(SUM(total_tokens),0) t, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,
                           COALESCE(SUM(total_cost),0) cost, COUNT(*) calls, COUNT(DISTINCT user_id) users
                           FROM usage_ledger WHERE created>=?""", (month,)).fetchone()
        comp = c.execute("""SELECT COALESCE(SUM(system_tokens),0) system, COALESCE(SUM(history_tokens),0) history,
                            COALESCE(SUM(context_tokens),0) context, COALESCE(SUM(question_tokens),0) question,
                            COALESCE(SUM(other_tokens),0) other, COALESCE(SUM(output_tokens),0) output
                            FROM usage_ledger WHERE created>=? AND system_tokens IS NOT NULL""", (month,)).fetchone()
        revenue = one("""SELECT COALESCE(SUM(amount_minor - refunded_minor),0) FROM payments WHERE status IN
                         ('captured','partially_refunded','refunded') AND created>=?""", month)
        overage_paid = one("SELECT COALESCE(SUM(amount_minor),0) FROM charges WHERE kind='overage' AND status='paid'")
        overage_due = one("SELECT COALESCE(SUM(amount_minor),0) FROM charges WHERE kind='overage' AND status='due'")
        accruing = one("""SELECT COALESCE(SUM(t.overage_tokens * p.overage_price_per_1k_minor / 1000.0),0) FROM usage_totals t
                          JOIN subscriptions s ON s.id=t.subscription_id AND s.current_period_start=t.period_start
                          JOIN plans p ON p.id=s.plan_id WHERE s.status IN ('trialing','active','past_due') AND p.allow_overage=1""")
        top = c.execute("""SELECT l.user_id, u.name, u.email, SUM(l.total_tokens) t, SUM(l.total_cost) cost, COUNT(*) calls
                           FROM usage_ledger l LEFT JOIN users u ON u.id=l.user_id WHERE l.created>=?
                           GROUP BY l.user_id ORDER BY t DESC LIMIT 10""", (month,)).fetchall()
        models = c.execute("""SELECT provider, model, SUM(input_tokens) i, SUM(output_tokens) o, SUM(total_tokens) t,
                              SUM(total_cost) cost, COUNT(*) calls FROM usage_ledger WHERE created>=?
                              GROUP BY provider, model ORDER BY t DESC""", (month,)).fetchall()
        features = c.execute("""SELECT feature, SUM(total_tokens) t, SUM(total_cost) cost, COUNT(*) calls FROM usage_ledger
                                WHERE created>=? GROUP BY feature ORDER BY t DESC""", (month,)).fetchall()
        estimated = one("SELECT COUNT(*) FROM usage_ledger WHERE created>=? AND usage_source='estimated'", month)
    user_count = len(users)
    return {
        "month_start": month, "usd_to_inr": fx,
        "total_users": user_count, "active_subscribers": len(paid),
        "trialing": sum(1 for s in subs if s["is_trial"]), "past_due": sum(1 for s in subs if s["status"] == "past_due"),
        "free_users": user_count - len(paid),
        "monthly_revenue": revenue / 100, "mrr": round(mrr / 100, 2),
        "provider_cost_usd": round(led["cost"], 4), "provider_cost_inr": round(led["cost"] * fx, 2),
        "total_tokens": led["t"], "input_tokens": led["i"], "output_tokens": led["o"], "llm_calls": led["calls"],
        "avg_tokens_per_user": round(led["t"] / led["users"]) if led["users"] else 0,
        "overage_revenue": overage_paid / 100, "overage_due": overage_due / 100, "overage_accruing": round(accruing / 100, 2),
        "estimated_calls": estimated,
        "components": dict(comp) if comp else {},
        "top_users": [{"user_id": r["user_id"], "name": r["name"] or ("Anonymous / system" if r["user_id"] is None else "Deleted user"),
                       "email": r["email"], "total_tokens": r["t"], "cost_usd": round(r["cost"], 4),
                       "cost_inr": round(r["cost"] * fx, 2), "calls": r["calls"]} for r in top],
        "models": [{"provider": r["provider"], "model": r["model"], "input_tokens": r["i"], "output_tokens": r["o"],
                    "total_tokens": r["t"], "cost_usd": round(r["cost"], 4), "cost_inr": round(r["cost"] * fx, 2),
                    "calls": r["calls"]} for r in models],
        "features": [{"feature": r["feature"], "label": FEATURES.get(r["feature"], r["feature"]), "total_tokens": r["t"],
                      "cost_inr": round(r["cost"] * fx, 2), "calls": r["calls"]} for r in features],
    }


def admin_users(q: str = "") -> List[dict]:
    """Every user with plan and current-period usage (search by name or email)."""
    like = f"%{q.strip().lower()}%"
    with transaction() as c:
        fx = float(billing.get_settings_all(c)["usd_to_inr"])
        rows = c.execute("""SELECT id, name, email, role, active FROM users WHERE lower(name) LIKE ? OR lower(email) LIKE ?
                            ORDER BY name LIMIT 200""", (like, like)).fetchall()
        out = []
        for u in rows:
            sub = billing.current(c, u["id"])
            s = summary(c, sub)
            cost_ = _period_used(c, sub["id"], sub["current_period_start"])["cost"]
            out.append({"user_id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"],
                        "active": bool(u["active"]), "plan": s["plan"], "status": s["status"], "is_trial": s["is_trial"],
                        "period_start": s["period_start"], "period_end": s["period_end"], "used": s["used"],
                        "limit": s["limit"], "percentage": s["percentage"], "requests": s["requests"],
                        "cost_inr": round(cost_ * fx, 2)})
    return out


def admin_user_detail(user_id: int) -> dict:
    with transaction() as c:
        u = c.execute("SELECT id, name, email, role, active, created FROM users WHERE id=?", (user_id,)).fetchone()
        if u is None:
            raise billing.BillingError("User not found.", 404)
        fx = float(billing.get_settings_all(c)["usd_to_inr"])
        sub = billing.current(c, user_id)
        s = summary(c, sub)
        period = (sub["id"], sub["current_period_start"])
        tot = _period_used(c, *period)
        comp = c.execute("""SELECT COALESCE(SUM(system_tokens),0) system, COALESCE(SUM(history_tokens),0) history,
                            COALESCE(SUM(context_tokens),0) context, COALESCE(SUM(question_tokens),0) question,
                            COALESCE(SUM(other_tokens),0) other FROM usage_ledger
                            WHERE subscription_id=? AND billing_period_start=? AND system_tokens IS NOT NULL""", period).fetchone()
        models = c.execute("""SELECT model, SUM(total_tokens) t, SUM(total_cost) cost, COUNT(*) calls FROM usage_ledger
                              WHERE subscription_id=? AND billing_period_start=? GROUP BY model ORDER BY t DESC""", period).fetchall()
        sub_view = billing.subscription_view(sub, c)
    return {"user": dict(u), "usage": s, "subscription": sub_view,
            "provider_cost_usd": round(tot["cost"], 6), "provider_cost_inr": round(tot["cost"] * fx, 2),
            "components": dict(comp), "models": [{"model": r["model"], "total_tokens": r["t"], "calls": r["calls"],
                                                  "cost_inr": round(r["cost"] * fx, 2)} for r in models],
            "subscriptions": billing.user_subscriptions(user_id)}


def attachments_this_period(user_id: int) -> tuple:
    """(files attached in the current billing period, plan) for the plan's attachment limits."""
    with transaction() as c:
        sub = billing.current(c, user_id)
        n = c.execute("SELECT COUNT(*) FROM activity WHERE user_id=? AND action='attachment' AND ts>=?",
                      (user_id, sub["current_period_start"])).fetchone()[0]
    return n, sub["plan"]


def plan_for(user_id: int) -> dict:
    with transaction() as c:
        return billing.current(c, user_id)["plan"]
