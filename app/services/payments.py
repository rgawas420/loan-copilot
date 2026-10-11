"""Payment providers behind one interface, so the billing service never depends on a provider's API.

A provider creates checkouts, verifies a completed checkout server-side, verifies and translates webhooks into
the billing service's own events, and cancels, changes or refunds at the provider. The billing service decides
what each event means for a subscription; nothing here touches the database.

Providers:
  razorpay  Razorpay Subscriptions (INR). Webhook signature: HMAC-SHA256 of the raw body with the webhook secret.
  mock      Development only (refused when APP_ENV=production). Payments are simulated through signed webhook
            events that go through exactly the same verification and processing path as real ones.

Internal event types: payment.success, payment.failed, subscription.created, subscription.activated,
subscription.updated, subscription.past_due, subscription.canceled, subscription.expired, refund.created.
"""
import base64
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from app.core.config import get_settings


class PaymentError(Exception):
    pass


class SignatureError(PaymentError):
    pass


@dataclass
class Event:
    id: str                                    # provider's event id: the idempotency key
    type: str                                  # internal event type (see module docstring)
    external_subscription_id: Optional[str] = None
    external_payment_id: Optional[str] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    external_plan_id: Optional[str] = None
    raw_type: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class Provider:
    name = ""

    def create_checkout(self, user: dict, plan: dict, checkout_id: str) -> dict:
        """Start a subscription checkout. Returns {'external_id': ..., 'client': {...data for the browser}}."""
        raise NotImplementedError

    def verify_checkout(self, payload: dict, external_id: str) -> List[Event]:
        """Server-side verification of a checkout the browser reports as paid. Returns the events it proves."""
        raise NotImplementedError

    def parse_webhook(self, headers: dict, body: bytes) -> List[Event]:
        """Verify the signature (SignatureError if it fails) and translate the webhook into events."""
        raise NotImplementedError

    def cancel(self, external_id: str, at_period_end: bool = True):
        raise NotImplementedError

    def change_plan(self, external_id: str, plan: dict):
        raise NotImplementedError

    def refund(self, external_payment_id: str, amount_minor: Optional[int] = None) -> dict:
        raise NotImplementedError


# ------------------------------------------------------------------ mock (development)
_MOCK_SECRET = secrets.token_hex(16)


class MockProvider(Provider):
    name = "mock"

    @property
    def secret(self) -> str:
        return get_settings().mock_payment_secret or _MOCK_SECRET

    def create_checkout(self, user, plan, checkout_id):
        ext = f"mock_sub_{secrets.token_hex(8)}"
        return {"external_id": ext, "client": {"provider": "mock", "checkout_id": checkout_id, "plan": plan["name"],
                                               "amount": plan["price"], "currency": plan["currency"]}}

    def verify_checkout(self, payload, external_id):
        raise PaymentError("Test payments are completed with the test checkout, not verified from the browser.")

    def sign(self, event: dict) -> tuple:
        body = json.dumps(event).encode()
        return {"x-mock-signature": _hmac(self.secret, body)}, body

    def event(self, type_: str, **fields) -> tuple:
        """A signed test webhook (headers, body), as a provider would send it."""
        return self.sign({"id": f"evt_mock_{secrets.token_hex(8)}", "type": type_, **fields})

    def parse_webhook(self, headers, body):
        sig = headers.get("x-mock-signature") or ""
        if not hmac.compare_digest(sig, _hmac(self.secret, body)):
            raise SignatureError("Invalid webhook signature.")
        e = json.loads(body)
        return [Event(id=e["id"], type=e["type"], external_subscription_id=e.get("subscription_id"),
                      external_payment_id=e.get("payment_id"), amount_minor=e.get("amount_minor"),
                      currency=e.get("currency"), external_plan_id=e.get("plan_id"), raw_type=e["type"])]

    def cancel(self, external_id, at_period_end=True):
        return {"ok": True}

    def change_plan(self, external_id, plan):
        return {"ok": True}

    def refund(self, external_payment_id, amount_minor=None):
        return {"id": f"rfnd_mock_{secrets.token_hex(6)}"}


# ------------------------------------------------------------------ Razorpay
RAZORPAY_API = "https://api.razorpay.com/v1"
_RZP_EVENTS = {                       # Razorpay event -> internal event
    "subscription.authenticated": "subscription.created",
    "subscription.activated": "subscription.activated",
    "subscription.charged": "payment.success",
    "subscription.pending": "payment.failed",
    "subscription.halted": "subscription.past_due",
    "subscription.updated": "subscription.updated",
    "subscription.cancelled": "subscription.canceled",
    "subscription.completed": "subscription.expired",
    "payment.failed": "payment.failed",
    "refund.created": "refund.created",
    "refund.processed": "refund.created",
}


class RazorpayProvider(Provider):
    name = "razorpay"

    def __init__(self):
        s = get_settings()
        if not (s.razorpay_key_id and s.razorpay_key_secret):
            raise PaymentError("Razorpay is not configured (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET).")
        self.key_id, self.key_secret, self.webhook_secret = s.razorpay_key_id, s.razorpay_key_secret, s.razorpay_webhook_secret

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        auth = base64.b64encode(f"{self.key_id}:{self.key_secret}".encode()).decode()
        req = urllib.request.Request(RAZORPAY_API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error", {}).get("description")
            except Exception:
                msg = None
            raise PaymentError(f"Razorpay: {msg or e.reason}")
        except urllib.error.URLError as e:
            raise PaymentError(f"Could not reach Razorpay ({e.reason}).")

    def create_checkout(self, user, plan, checkout_id):
        rzp_plan = (plan.get("external_ids") or {}).get("razorpay")
        if not rzp_plan:
            raise PaymentError(f"The {plan['name']} plan has no Razorpay plan id. An admin must add it first.")
        sub = self._call("POST", "/subscriptions", {
            "plan_id": rzp_plan, "total_count": 10 if plan["billing_interval"] == "yearly" else 120,
            "customer_notify": 1, "notes": {"checkout_id": checkout_id, "user_id": str(user["id"])}})
        return {"external_id": sub["id"], "client": {
            "provider": "razorpay", "checkout_id": checkout_id, "key": self.key_id, "subscription_id": sub["id"],
            "name": "Urban Money Loan Copilot", "description": f"{plan['name']} plan",
            "prefill": {"name": user.get("name", ""), "email": user.get("email", "")}}}

    def verify_checkout(self, payload, external_id):
        pid, sid, sig = (payload.get("razorpay_payment_id") or "", payload.get("razorpay_subscription_id") or "",
                         payload.get("razorpay_signature") or "")
        if not (pid and sid and sig) or sid != external_id:
            raise SignatureError("Incomplete payment confirmation.")
        if not hmac.compare_digest(sig, _hmac(self.key_secret, f"{pid}|{sid}".encode())):
            raise SignatureError("The payment signature does not match.")
        # The signature proves the browser did not invent the ids; the API confirms the money actually moved.
        sub, pay = self._call("GET", f"/subscriptions/{sid}"), self._call("GET", f"/payments/{pid}")
        if sub.get("status") not in ("authenticated", "active") or pay.get("status") not in ("authorized", "captured"):
            raise PaymentError("The payment has not been confirmed by Razorpay yet.")
        return [Event(id=f"verify:{pid}", type="payment.success", external_subscription_id=sid, external_payment_id=pid,
                      amount_minor=pay.get("amount"), currency=pay.get("currency"), raw_type="checkout.verified")]

    def parse_webhook(self, headers, body):
        if not self.webhook_secret:
            raise SignatureError("RAZORPAY_WEBHOOK_SECRET is not set; webhooks cannot be verified.")
        if not hmac.compare_digest(headers.get("x-razorpay-signature") or "", _hmac(self.webhook_secret, body)):
            raise SignatureError("Invalid webhook signature.")
        e = json.loads(body)
        kind = _RZP_EVENTS.get(e.get("event", ""))
        event_id = headers.get("x-razorpay-event-id") or hashlib.sha256(body).hexdigest()
        if kind is None:
            return [Event(id=event_id, type="ignored", raw_type=e.get("event", ""))]
        p = e.get("payload", {})
        sub = (p.get("subscription") or {}).get("entity") or {}
        pay = (p.get("payment") or {}).get("entity") or {}
        ref = (p.get("refund") or {}).get("entity") or {}
        return [Event(id=event_id, type=kind, external_subscription_id=sub.get("id") or pay.get("subscription_id"),
                      external_payment_id=ref.get("payment_id") or pay.get("id"),
                      amount_minor=ref.get("amount") if ref else pay.get("amount"),
                      currency=ref.get("currency") or pay.get("currency"), external_plan_id=sub.get("plan_id"),
                      raw_type=e.get("event", ""))]

    def cancel(self, external_id, at_period_end=True):
        return self._call("POST", f"/subscriptions/{external_id}/cancel", {"cancel_at_cycle_end": 1 if at_period_end else 0})

    def change_plan(self, external_id, plan):
        rzp_plan = (plan.get("external_ids") or {}).get("razorpay")
        if not rzp_plan:
            raise PaymentError(f"The {plan['name']} plan has no Razorpay plan id.")
        return self._call("PATCH", f"/subscriptions/{external_id}", {"plan_id": rzp_plan, "schedule_change_at": "cycle_end"})

    def refund(self, external_payment_id, amount_minor=None):
        return self._call("POST", f"/payments/{external_payment_id}/refund",
                          {"amount": amount_minor} if amount_minor else {})


def get_provider(name: Optional[str] = None) -> Provider:
    s = get_settings()
    name = (name or s.payment_provider or "mock").lower()
    if name == "mock":
        if s.app_env == "production":
            raise PaymentError("The mock payment provider cannot be used in production. Set PAYMENT_PROVIDER=razorpay.")
        return MockProvider()
    if name == "razorpay":
        return RazorpayProvider()
    raise PaymentError(f"Unknown payment provider: {name}")
