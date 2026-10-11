"""Usage, subscriptions, plans, payment webhooks and the admin billing console.

Users only ever see their own usage, subscription and payments: the user id always comes from the session,
never from the request. Token counts and costs are computed on the server from the provider's usage reports.
"""
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import admin_user, current_user
from app.core.config import get_settings
from app.rag.llm import model_for
from app.services import accounts, billing, payments, usage

router = APIRouter(prefix="/api")


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except billing.BillingError as e:
        raise HTTPException(e.code, str(e))
    except payments.PaymentError:              # e.g. the payment provider is not configured yet
        raise HTTPException(503, "Online payments are not available right now. Contact your administrator to "
                                 "change your plan.")


# ------------------------------------------------------------------ usage (own data only)
Range = Literal["7d", "30d", "90d", "period"]


@router.get("/usage/current")
def usage_current(user=Depends(current_user)):
    return usage.current_usage(user["id"])


@router.get("/usage/history")
def usage_history(limit: int = 50, offset: int = 0, date_from: Optional[str] = None, date_to: Optional[str] = None,
                  model: Optional[str] = None, conversation_id: Optional[str] = None, user=Depends(current_user)):
    return usage.history(user["id"], limit=limit, offset=offset, date_from=date_from, date_to=date_to, model=model,
                         conversation_id=conversation_id)


@router.get("/usage/daily")
def usage_daily(range: Range = "7d", user=Depends(current_user)):
    return usage.daily(user["id"], range)


@router.get("/usage/by-model")
def usage_by_model(range: Range = "period", user=Depends(current_user)):
    return usage.by_model(user["id"], range)


# ------------------------------------------------------------------ plans and subscription
class PlanChoice(BaseModel):
    plan_id: int


class VerifyIn(BaseModel):
    checkout_id: str = Field(max_length=64)
    payload: Dict[str, str] = Field(default_factory=dict)


class MockPayIn(BaseModel):
    checkout_id: str = Field(max_length=64)
    outcome: Literal["success", "fail"] = "success"


@router.get("/plans")
def plans_list(_=Depends(current_user)):
    return {"plans": billing.list_plans(), "provider": get_settings().payment_provider}


@router.get("/subscription")
def subscription_get(user=Depends(current_user)):
    return billing.get_subscription(user["id"])


@router.post("/subscription/checkout")
def subscription_checkout(body: PlanChoice, user=Depends(current_user)):
    client = _guard(billing.start_checkout, user, body.plan_id)
    accounts.log(user["id"], "checkout_started", f"plan {body.plan_id}")
    return {"checkout": client}


@router.post("/subscription/upgrade")
def subscription_upgrade(body: PlanChoice, user=Depends(current_user)):
    client = _guard(billing.start_checkout, user, body.plan_id, "upgrade")
    accounts.log(user["id"], "checkout_started", f"upgrade to plan {body.plan_id}")
    return {"checkout": client}


@router.post("/subscription/trial")
def subscription_trial(body: PlanChoice, user=Depends(current_user)):
    sub = _guard(billing.start_trial, user, body.plan_id)
    accounts.log(user["id"], "trial_started", sub["plan"]["name"])
    return {"subscription": sub}


@router.post("/subscription/verify")
def subscription_verify(body: VerifyIn, user=Depends(current_user)):
    """The browser's checkout finished: the server confirms the payment with the provider before activating."""
    return _guard(billing.verify_checkout, user, body.checkout_id, body.payload)


@router.post("/subscription/downgrade")
def subscription_downgrade(body: PlanChoice, user=Depends(current_user)):
    sub = _guard(billing.downgrade, user, body.plan_id)
    accounts.log(user["id"], "plan_downgrade_scheduled", f"plan {body.plan_id}")
    return {"subscription": sub}


@router.post("/subscription/cancel")
def subscription_cancel(user=Depends(current_user)):
    sub = _guard(billing.cancel, user)
    accounts.log(user["id"], "subscription_canceled", sub["plan"]["name"])
    return {"subscription": sub}


@router.post("/subscription/mock/complete")
def subscription_mock_complete(body: MockPayIn, user=Depends(current_user)):
    """Development only: finish a test checkout. Sends signed test webhooks through the normal webhook path."""
    if get_settings().app_env == "production":
        raise HTTPException(404, "Not found.")
    return _guard(billing.mock_complete, user, body.checkout_id, body.outcome)


# ------------------------------------------------------------------ provider webhooks
@router.post("/webhooks/payment")
async def payment_webhook(request: Request):
    """Signed events from the payment provider. Each event is applied once (idempotent by event id)."""
    body = await request.body()
    if len(body) > 512 * 1024:
        raise HTTPException(413, "Payload too large.")
    try:
        results = billing.handle_webhook(get_settings().payment_provider, dict(request.headers), body)
    except payments.SignatureError as e:
        raise HTTPException(400, str(e))
    except payments.PaymentError as e:
        raise HTTPException(503, str(e))
    except ValueError:
        raise HTTPException(400, "Malformed webhook payload.")
    return {"ok": True, "results": [{"event": r["event"], "status": r["status"]} for r in results]}


# ------------------------------------------------------------------ admin: billing and usage
class PlanIn(BaseModel):
    code: Optional[str] = Field(default=None, max_length=40)
    name: Optional[str] = Field(default=None, max_length=60)
    description: Optional[str] = Field(default=None, max_length=500)
    price: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    currency: Optional[str] = Field(default=None, max_length=3)
    billing_interval: Optional[Literal["monthly", "yearly"]] = None
    token_limit: Optional[int] = Field(default=None, ge=0)
    allowed_models: Optional[List[str]] = None
    max_requests: Optional[int] = Field(default=None, ge=0)
    requests_per_minute: Optional[int] = Field(default=None, ge=1, le=10_000)
    max_concurrent: Optional[int] = Field(default=None, ge=1, le=100)
    max_context_tokens: Optional[int] = Field(default=None, ge=100)
    max_output_tokens: Optional[int] = Field(default=None, ge=50, le=100_000)
    rag_enabled: Optional[bool] = None
    max_file_mb: Optional[int] = Field(default=None, ge=0, le=1000)
    max_files_per_month: Optional[int] = Field(default=None, ge=0)
    allow_overage: Optional[bool] = None
    overage_price_per_1k: Optional[float] = Field(default=None, ge=0)
    max_overage_tokens: Optional[int] = Field(default=None, ge=0)
    trial_days: Optional[int] = Field(default=None, ge=0, le=365)
    features: Optional[List[str]] = None
    external_ids: Optional[Dict[str, str]] = None
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None

    def values(self) -> dict:
        d = self.model_dump(exclude_none=True)
        if "price" in d:
            d["price_minor"] = round(d.pop("price") * 100)
        if "overage_price_per_1k" in d:
            d["overage_price_per_1k_minor"] = round(d.pop("overage_price_per_1k") * 100)
        if "currency" in d:
            d["currency"] = d["currency"].upper()
        return d


class PriceIn(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=120)
    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    currency: str = Field(default="USD", max_length=3)


class SettingsIn(BaseModel):
    warn_thresholds: Optional[List[int]] = None
    usd_to_inr: Optional[float] = None
    grace_days: Optional[int] = None
    near_limit_policy: Optional[Literal["block", "allow"]] = None
    anonymous_daily_tokens: Optional[int] = None
    anonymous_max_concurrent: Optional[int] = None


class ChargePatch(BaseModel):
    status: Literal["due", "paid", "waived"]


@router.get("/admin/billing/overview")
def admin_overview(_=Depends(admin_user)):
    return usage.admin_overview()


@router.get("/admin/billing/daily")
def admin_daily(_=Depends(admin_user)):
    return usage.daily(None, "30d")


@router.get("/admin/billing/users")
def admin_billing_users(q: str = "", _=Depends(admin_user)):
    return {"users": usage.admin_users(q[:100])}


@router.get("/admin/billing/users/{user_id}")
def admin_billing_user(user_id: int, _=Depends(admin_user)):
    return _guard(usage.admin_user_detail, user_id)


@router.post("/admin/billing/users/{user_id}/plan")
def admin_assign_plan(user_id: int, body: PlanChoice, admin=Depends(admin_user)):
    sub = _guard(billing.assign_plan, user_id, body.plan_id)
    accounts.log(admin["id"], "admin_assigned_plan", f"user {user_id}: {sub['plan']['name']}")
    return {"subscription": sub}


@router.get("/admin/billing/ledger")
def admin_ledger(user_id: Optional[int] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                 model: Optional[str] = None, provider: Optional[str] = None, conversation_id: Optional[str] = None,
                 subscription_id: Optional[int] = None, feature: Optional[str] = None, limit: int = 100,
                 offset: int = 0, _=Depends(admin_user)):
    return usage.history(user_id, admin=True, limit=limit, offset=offset, date_from=date_from, date_to=date_to,
                         model=model, provider=provider, conversation_id=conversation_id,
                         subscription_id=subscription_id, feature=feature)


@router.get("/admin/billing/plans")
def admin_plans(_=Depends(admin_user)):
    return {"plans": billing.list_plans(include_inactive=True, admin=True)}


@router.post("/admin/billing/plans")
def admin_plan_create(body: PlanIn, admin=Depends(admin_user)):
    plan = _guard(billing.save_plan, body.values())
    accounts.log(admin["id"], "admin_plan_saved", plan["name"])
    return {"plan": plan}


@router.put("/admin/billing/plans/{plan_id}")
def admin_plan_update(plan_id: int, body: PlanIn, admin=Depends(admin_user)):
    plan = _guard(billing.save_plan, body.values(), plan_id)
    accounts.log(admin["id"], "admin_plan_saved", plan["name"])
    return {"plan": plan}


@router.get("/admin/billing/prices")
def admin_prices(_=Depends(admin_user)):
    return {"prices": usage.list_prices()}


@router.post("/admin/billing/prices")
def admin_price_set(body: PriceIn, admin=Depends(admin_user)):
    p = _guard(usage.set_price, body.provider, body.model, body.input_per_1m, body.output_per_1m, body.currency)
    accounts.log(admin["id"], "admin_price_set", f"{body.provider}/{body.model}")
    return {"price": p}


@router.get("/admin/billing/settings")
def admin_settings(_=Depends(admin_user)):
    s = get_settings()
    return {"settings": billing.get_settings_all(), "provider": s.payment_provider,
            "models": sorted({model_for("fast"), model_for("smart"), model_for("rag")}),
            "mock_tools": s.app_env != "production" and s.payment_provider == "mock"}


@router.put("/admin/billing/settings")
def admin_settings_update(body: SettingsIn, admin=Depends(admin_user)):
    out = _guard(billing.update_settings, body.model_dump(exclude_none=True))
    accounts.log(admin["id"], "admin_billing_settings")
    return {"settings": out}


@router.get("/admin/billing/payments")
def admin_payments(_=Depends(admin_user)):
    return {"payments": billing.admin_payments(), "charges": billing.admin_charges(),
            "webhooks": billing.admin_webhooks(50)}


@router.patch("/admin/billing/charges/{charge_id}")
def admin_charge_update(charge_id: int, body: ChargePatch, admin=Depends(admin_user)):
    ch = _guard(billing.set_charge_status, charge_id, body.status)
    accounts.log(admin["id"], "admin_charge_updated", f"charge {charge_id}: {body.status}")
    return {"charge": ch}


@router.post("/admin/billing/rebuild-totals")
def admin_rebuild_totals(admin=Depends(admin_user)):
    n = usage.rebuild_totals()
    accounts.log(admin["id"], "admin_rebuilt_usage_totals", f"{n} periods")
    return {"periods": n}


class MockEventIn(BaseModel):
    kind: Literal["renew", "fail", "cancel", "expire"]


@router.post("/admin/billing/mock/subscriptions/{subscription_id}")
def admin_mock_event(subscription_id: int, body: MockEventIn, _=Depends(admin_user)):
    """Development only: send a signed test webhook (renewal, failed payment, ...) for a test subscription."""
    if get_settings().app_env == "production":
        raise HTTPException(404, "Not found.")
    return {"results": _guard(billing.mock_event, subscription_id, body.kind)}
