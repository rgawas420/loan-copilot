"""Token usage, quotas, subscriptions, payments and webhooks."""
import json
import sqlite3
import threading
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

import app.rag.chat as chat
import app.rag.retrieval as retrieval
from app.main import app
from app.rag import metering
from app.services import accounts, billing, payments, usage
from app.services.db import iso, now, transaction


class UsageLLM(BaseChatModel):
    """A chat model that reports provider usage like Groq: on the response, or on the last stream chunk."""
    pieces: list = ["Axis code ", "is 6159MUM [S1]."]
    input_tokens: int = 1250
    output_tokens: int = 680
    model: str = "openai/gpt-oss-120b"
    report_usage: bool = True

    @property
    def _llm_type(self):
        return "usage-fake"

    def _usage(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens}

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        msg = AIMessage(content="".join(self.pieces), usage_metadata=self._usage(),
                        response_metadata={"model_name": self.model})
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        for p in self.pieces:
            yield ChatGenerationChunk(message=AIMessageChunk(content=p))
        if self.report_usage:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=self._usage(),
                                                             response_metadata={"model_name": self.model}))


DOCS = [Document(page_content="Source: Code Book.xlsx\nBANK NAME: Axis Bank | HL: 6159MUM",
                 metadata={"source": "Code Book.xlsx", "corpus": "urban_money"}, id="um-1")]


def _kb(monkeypatch, llm):
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt: [(d, 0.8) for d in DOCS])
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: [])
    monkeypatch.setattr(chat, "get_llm", lambda kind="fast": llm)


def _user(name="Bill"):
    c = TestClient(app)
    r = c.post("/api/auth/register", json={"email": f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com",
                                           "name": name, "password": "correct-horse"})
    return c, r.json()["user"]


def _admin():
    c, u = _user("Admin")
    accounts.update_user(u["id"], -1, role="admin")
    return c, u


def _plan(**kw):
    base = {"name": f"T{uuid.uuid4().hex[:6]}", "price_minor": 0, "token_limit": 100_000, "allowed_models": [],
            "requests_per_minute": 1000, "max_concurrent": 100, "max_output_tokens": 1000, "max_context_tokens": 50_000}
    return billing.save_plan({**base, **kw})


def _events(text):
    out = []
    for block in text.strip().split("\n\n"):
        kind = block.split("\n")[0].removeprefix("event: ")
        out.append((kind, json.loads(block.split("\n", 1)[1].removeprefix("data: "))))
    return out


def _ledger(user_id):
    with transaction() as c:
        return [dict(r) for r in c.execute("SELECT * FROM usage_ledger WHERE user_id=? ORDER BY id", (user_id,))]


def _age_period(user_id, days):
    """Move the user's current period into the past (time travel for lifecycle tests)."""
    with transaction() as c:
        sub = billing._live(c, user_id)
        start, end = billing.parse(sub["current_period_start"]), billing.parse(sub["current_period_end"])
        paid = sub["paid_period_start"] == sub["current_period_start"]
        c.execute("""UPDATE subscriptions SET start_date=?, current_period_start=?, current_period_end=?,
                     paid_period_start=? WHERE id=?""",
                  (iso(start - timedelta(days=days)), iso(start - timedelta(days=days)), iso(end - timedelta(days=days)),
                   iso(start - timedelta(days=days)) if paid else sub["paid_period_start"], sub["id"]))
        c.execute("UPDATE usage_totals SET period_start=? WHERE subscription_id=? AND period_start=?",
                  (iso(start - timedelta(days=days)), sub["id"], sub["current_period_start"]))
        return sub["id"]


# ------------------------------------------------------------------ ledger: provider-reported usage
def test_stream_records_provider_usage_cost_and_rag_split(monkeypatch):
    _kb(monkeypatch, UsageLLM(callbacks=[metering.callback]))
    c, u = _user()
    conv = c.post("/api/conversations", json={"title": "x"}).json()
    r = c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?",
                                         "conversation_id": conv["id"], "message_id": "m1"})
    ev = _events(r.text)
    final = dict(ev)["usage"]
    assert final["request"] == {"input_tokens": 1250, "output_tokens": 680, "total_tokens": 1930, "calls": 1}
    assert final["usage"]["used"] == 1930 and final["usage"]["plan"] == "Free"
    row = _ledger(u["id"])[-1]
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"]) == (1250, 680, 1930)
    assert row["usage_source"] == "provider" and row["model"] == "openai/gpt-oss-120b" and row["purpose"] == "answer"
    assert row["conversation_id"] == conv["id"] and row["message_id"] == "m1"
    assert row["total_cost"] == pytest.approx(1250 / 1e6 * 0.15 + 680 / 1e6 * 0.60)
    parts = [row[f"{k}_tokens"] for k in usage.COMPONENTS]
    assert sum(parts) == 1250 and row["system_tokens"] > 0 and row["context_tokens"] > 0
    with transaction() as db:                                # reservation released, actual usage kept
        req = db.execute("SELECT * FROM ai_requests WHERE user_id=?", (u["id"],)).fetchone()
    assert req["status"] == "completed" and req["actual_tokens"] == 1930


def test_conversation_id_of_another_user_is_not_recorded(monkeypatch):
    _kb(monkeypatch, UsageLLM(callbacks=[metering.callback]))
    other, _ = _user("Other")
    conv = other.post("/api/conversations", json={"title": "x"}).json()
    c, u = _user()
    c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?", "conversation_id": conv["id"]})
    assert _ledger(u["id"])[-1]["conversation_id"] is None


def test_stream_without_provider_usage_is_estimated(monkeypatch):
    _kb(monkeypatch, UsageLLM(callbacks=[metering.callback], report_usage=False))
    c, u = _user()
    c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?"})
    row = _ledger(u["id"])[-1]
    assert row["usage_source"] == "estimated" and row["input_tokens"] > 0 and row["output_tokens"] > 0


def test_ledger_is_append_only():
    usage.record(None, provider="groq", model="m", input_tokens=1, output_tokens=1)
    with pytest.raises(sqlite3.DatabaseError):
        with transaction() as c:
            c.execute("UPDATE usage_ledger SET total_tokens=0")
    with pytest.raises(sqlite3.DatabaseError):
        with transaction() as c:
            c.execute("DELETE FROM usage_ledger")


# ------------------------------------------------------------------ quota
def test_reservation_is_reconciled_with_actual_usage():
    _, u = _user()
    billing.assign_plan(u["id"], _plan(token_limit=100_000)["id"])
    res = usage.reserve(u, "chat", 0)
    assert res.reserved == 4000 + 1000
    assert usage.current_usage(u["id"])["in_progress"] == 5000
    usage.record(res, provider="groq", model="m", input_tokens=2000, output_tokens=400)
    assert usage.current_usage(u["id"])["in_progress"] == 2600            # 5000 held - 2400 already recorded
    usage.settle(res)
    cur = usage.current_usage(u["id"])
    assert (cur["used"], cur["in_progress"], cur["remaining"]) == (2400, 0, 97_600)


def test_concurrent_requests_cannot_overspend():
    """10 simultaneous requests, room for exactly 3 reservations: exactly 3 get through."""
    _, u = _user()
    billing.assign_plan(u["id"], _plan(token_limit=15_000)["id"])         # each reservation: 4000 + 1000
    ok, refused, gate = [], [], threading.Barrier(10)

    def go():
        gate.wait()
        try:
            ok.append(usage.reserve(u, "chat", 0))
        except usage.QuotaError as e:
            refused.append(e.code)
    threads = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ok) == 3 and refused == ["insufficient_quota"] * 7


def test_limit_reached_blocks_with_upgrade_details(monkeypatch):
    _kb(monkeypatch, UsageLLM(callbacks=[metering.callback]))
    c, u = _user()
    billing.assign_plan(u["id"], _plan(token_limit=6000)["id"])
    assert c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?"}).status_code == 200
    _spend(u["id"], 5000)
    r = c.post("/api/chat/stream", json={"mode": "kb", "question": "Axis home loan code?"})
    assert r.status_code == 402
    d = r.json()["detail"]
    assert d["code"] == "quota_exceeded" and d["limit"] == 6000 and d["used"] >= 6000
    assert usage.current_usage(u["id"])["blocked"] is True


def test_rate_concurrency_and_context_limits():
    _, u = _user()
    billing.assign_plan(u["id"], _plan(requests_per_minute=2, max_concurrent=5, max_context_tokens=1000)["id"])
    with pytest.raises(usage.QuotaError) as e:
        usage.reserve(u, "chat", 10_000)
    assert e.value.code == "request_too_large"
    usage.settle(usage.reserve(u, "chat", 0))
    usage.settle(usage.reserve(u, "chat", 0))
    with pytest.raises(usage.QuotaError) as e:
        usage.reserve(u, "chat", 0)
    assert e.value.code == "rate_limited" and e.value.status == 429

    _, v = _user()
    billing.assign_plan(v["id"], _plan(max_concurrent=1)["id"])
    held = usage.reserve(v, "chat", 0)
    with pytest.raises(usage.QuotaError) as e:
        usage.reserve(v, "chat", 0)
    assert e.value.code == "too_many_concurrent"
    usage.settle(held)
    usage.settle(usage.reserve(v, "chat", 0))


def _spend(user_id, tokens):
    """Usage recorded in the user's current period (as an already-approved request would)."""
    with transaction() as c:
        s = billing.current(c, user_id)
    res = usage.Reservation(f"t{uuid.uuid4().hex[:8]}", user_id, "chat", 0, s["id"], s["plan"],
                            s["current_period_start"], s["current_period_end"])
    usage.record(res, provider="groq", model="m", input_tokens=tokens, output_tokens=0)


def test_warning_thresholds():
    _, u = _user()
    billing.assign_plan(u["id"], _plan(token_limit=10_000)["id"])
    assert usage.current_usage(u["id"])["warning"] is None
    _spend(u["id"], 7600)
    assert usage.current_usage(u["id"])["warning"]["threshold"] == 75
    _spend(u["id"], 1500)
    w = usage.current_usage(u["id"])["warning"]
    assert w["threshold"] == 90 and w["level"] == "strong"


def test_overage_is_recorded_and_charged_at_period_end():
    _, u = _user()
    plan = _plan(token_limit=5000, allow_overage=True, overage_price_per_1k_minor=200, max_overage_tokens=0)
    billing.assign_plan(u["id"], plan["id"])
    res = usage.reserve(u, "chat", 0)                                     # 5000 needed: fits
    usage.record(res, provider="groq", model="m", input_tokens=4000, output_tokens=0)
    usage.settle(res)
    res = usage.reserve(u, "chat", 0)                                     # over the limit, allowed as overage
    usage.record(res, provider="groq", model="m", input_tokens=3500, output_tokens=0)
    usage.settle(res)
    cur = usage.current_usage(u["id"])
    assert cur["used"] == 7500 and cur["overage_tokens"] == 2500 and cur["overage_amount"] == 5.0
    _age_period(u["id"], 40)
    usage.current_usage(u["id"])                                          # period rolls: overage becomes a charge
    with transaction() as c:
        ch = c.execute("SELECT * FROM charges WHERE user_id=?", (u["id"],)).fetchone()
    assert ch["tokens"] == 2500 and ch["amount_minor"] == 500 and ch["status"] == "due"


def test_new_period_starts_at_zero_and_keeps_history():
    _, u = _user()
    res = usage.reserve(u, "chat", 0)
    usage.record(res, provider="groq", model="m", input_tokens=900, output_tokens=100)
    usage.settle(res)
    assert usage.current_usage(u["id"])["used"] == 1000
    _age_period(u["id"], 31)
    cur = usage.current_usage(u["id"])
    assert cur["used"] == 0 and cur["plan"] == "Free"
    assert sum(r["total_tokens"] for r in _ledger(u["id"])) == 1000      # the ledger keeps the old period


# ------------------------------------------------------------------ subscriptions, payments, webhooks
def _paid_plan(**kw):
    return _plan(price_minor=99_900, token_limit=500_000, **kw)


def test_checkout_activates_only_after_verified_payment_event():
    c, u = _user()
    plan = _paid_plan()
    co = c.post("/api/subscription/checkout", json={"plan_id": plan["id"]}).json()["checkout"]
    assert c.get("/api/subscription").json()["subscription"]["plan"]["name"] == "Free"   # not active yet
    r = c.post("/api/subscription/mock/complete", json={"checkout_id": co["checkout_id"], "outcome": "success"})
    sub = r.json()["subscription"]
    assert sub["plan"]["id"] == plan["id"] and sub["status"] == "active"
    pays = c.get("/api/subscription").json()["payments"]
    assert len(pays) == 1 and pays[0]["amount"] == 999.0 and pays[0]["status"] == "captured"
    assert usage.current_usage(u["id"])["limit"] == 500_000


def test_failed_first_payment_keeps_free_plan():
    c, u = _user()
    co = c.post("/api/subscription/checkout", json={"plan_id": _paid_plan()["id"]}).json()["checkout"]
    c.post("/api/subscription/mock/complete", json={"checkout_id": co["checkout_id"], "outcome": "fail"})
    data = c.get("/api/subscription").json()
    assert data["subscription"]["plan"]["name"] == "Free" and data["payments"][0]["status"] == "failed"


def test_webhook_signature_and_idempotency():
    c, u = _user()
    co = c.post("/api/subscription/checkout", json={"plan_id": _paid_plan()["id"]}).json()["checkout"]
    with transaction() as db:
        ext = db.execute("SELECT external_id FROM checkouts WHERE id=?", (co["checkout_id"],)).fetchone()[0]
    mock = payments.get_provider("mock")
    headers, body = mock.event("payment.success", subscription_id=ext, payment_id="pay_x1", amount_minor=99_900, currency="INR")
    bad = TestClient(app).post("/api/webhooks/payment", content=body, headers={"x-mock-signature": "0" * 64})
    assert bad.status_code == 400
    first = TestClient(app).post("/api/webhooks/payment", content=body, headers=headers).json()
    again = TestClient(app).post("/api/webhooks/payment", content=body, headers=headers).json()
    assert first["results"][0]["status"] == "processed" and again["results"][0]["status"] == "duplicate"
    assert len(c.get("/api/subscription").json()["payments"]) == 1


def test_renewal_failed_payment_grace_and_expiry():
    c, u = _user()
    co = c.post("/api/subscription/checkout", json={"plan_id": _paid_plan()["id"]}).json()["checkout"]
    c.post("/api/subscription/mock/complete", json={"checkout_id": co["checkout_id"], "outcome": "success"})
    sub_id = _age_period(u["id"], 31)                                   # the paid period has ended
    assert c.get("/api/subscription").json()["subscription"]["status"] == "past_due"
    billing.mock_event(sub_id, "renew")                                 # renewal payment arrives
    s = c.get("/api/subscription").json()["subscription"]
    assert s["status"] == "active" and billing.parse(s["current_period_end"]) > now()

    _age_period(u["id"], 40)                                            # unpaid beyond the grace days
    s = c.get("/api/subscription").json()["subscription"]
    assert s["plan"]["name"] == "Free" and s["status"] == "active"


def test_cancel_keeps_access_until_period_end_then_free():
    c, u = _user()
    co = c.post("/api/subscription/checkout", json={"plan_id": _paid_plan()["id"]}).json()["checkout"]
    c.post("/api/subscription/mock/complete", json={"checkout_id": co["checkout_id"], "outcome": "success"})
    s = c.post("/api/subscription/cancel").json()["subscription"]
    assert s["cancel_at_period_end"] and s["plan"]["price"] == 999.0
    conv = c.post("/api/conversations", json={"title": "kept"}).json()
    _age_period(u["id"], 31)
    assert c.get("/api/subscription").json()["subscription"]["plan"]["name"] == "Free"
    assert c.get(f"/api/conversations/{conv['id']}").status_code == 200   # chats survive the downgrade


def test_trial_then_expiry():
    c, u = _user()
    plan = _paid_plan(trial_days=7)
    s = c.post("/api/subscription/trial", json={"plan_id": plan["id"]}).json()["subscription"]
    assert s["status"] == "trialing" and s["is_trial"]
    assert c.post("/api/subscription/trial", json={"plan_id": plan["id"]}).status_code == 400
    _age_period(u["id"], 8)
    assert c.get("/api/subscription").json()["subscription"]["plan"]["name"] == "Free"
    assert c.post("/api/subscription/trial", json={"plan_id": plan["id"]}).status_code == 400   # once per user


# ------------------------------------------------------------------ privacy and admin
def test_users_only_see_their_own_usage():
    a, ua = _user("A")
    b, ub = _user("B")
    res = usage.reserve(ua, "chat", 0)
    usage.record(res, provider="groq", model="m", input_tokens=10, output_tokens=5)
    usage.settle(res)
    assert b.get(f"/api/usage/history?user_id={ua['id']}").json()["items"] == []
    mine = a.get("/api/usage/history").json()["items"]
    assert len(mine) == 1 and "total_cost" not in mine[0] and "system_tokens" not in mine[0]
    assert b.get("/api/admin/billing/overview").status_code == 403


def test_admin_billing_console():
    admin, _ = _admin()
    _, u = _user("Rajesh")
    res = usage.reserve(u, "chat", 0)
    usage.record(res, provider="groq", model="openai/gpt-oss-20b", input_tokens=1000, output_tokens=500)
    usage.settle(res)
    ov = admin.get("/api/admin/billing/overview").json()
    assert ov["total_tokens"] >= 1500 and ov["total_users"] >= 2
    found = admin.get("/api/admin/billing/users?q=rajesh").json()["users"]
    assert any(x["user_id"] == u["id"] and x["used"] == 1500 for x in found)
    detail = admin.get(f"/api/admin/billing/users/{u['id']}").json()
    assert detail["usage"]["input_tokens"] == 1000 and detail["provider_cost_usd"] > 0
    led = admin.get(f"/api/admin/billing/ledger?user_id={u['id']}&model=openai/gpt-oss-20b").json()
    assert led["totals"]["total_tokens"] == 1500 and "total_cost" in led["items"][0]

    r = admin.post("/api/admin/billing/plans", json={"name": "Team", "price": 499, "token_limit": 250000})
    assert r.status_code == 200 and r.json()["plan"]["price_minor"] == 49_900
    pid = r.json()["plan"]["id"]
    assert admin.put(f"/api/admin/billing/plans/{pid}", json={"token_limit": 300000}).json()["plan"]["token_limit"] == 300000
    assert admin.post(f"/api/admin/billing/users/{u['id']}/plan", json={"plan_id": pid}).status_code == 200
    assert usage.current_usage(u["id"])["limit"] == 300000

    p = admin.post("/api/admin/billing/prices", json={"provider": "groq", "model": "openai/gpt-oss-20b",
                                                       "input_per_1m": 1, "output_per_1m": 2}).json()["price"]
    assert p["effective_to"] is None
    s = admin.put("/api/admin/billing/settings", json={"warn_thresholds": [50, 80]}).json()["settings"]
    assert s["warn_thresholds"] == [50, 80]
    admin.put("/api/admin/billing/settings", json={"warn_thresholds": [75, 90]})
