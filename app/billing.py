"""Lemon Squeezy webhook handling + atomic credit accounting.

Security model:
  * Webhook integrity: HMAC-SHA256 over raw body, compared with X-Signature header
    using a constant-time comparison (hmac.compare_digest).
  * Idempotency: every processed event_id is recorded in webhook_events; replays
    are silently acknowledged with 200.
  * Atomicity: credit deduction is a single UPDATE ... WHERE credits > 0 SQL
    statement, so concurrent requests cannot oversell credits.
"""
import hmac
import hashlib
import os

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import (
    User, Subscription, SubscriptionStatus, WebhookEvent,
)
from app import crud


PROVIDER = "lemonsqueezy"

# Pricing tiers — match by product_name first (case-insensitive substring),
# fall back to total_usd in cents. Both maps are independently editable.
TIER_BY_PRODUCT_NAME: dict[str, int] = {
    "60 transcription credits": 60,
    "100 transcription credits": 100,
}
TIER_BY_PRICE_USD_CENTS: dict[int, int] = {
    499: 60,    # $4.99 -> 60 credits
    999: 100,   # $9.99 -> 100 credits
}
# Used only when the order looks like a paid order but neither product nor
# price match a configured tier. Keeps backward compat for older orders.
DEFAULT_TIER_CREDITS = int(os.environ.get("DEFAULT_TIER_CREDITS", "100"))


class InsufficientCredits(Exception):
    pass


def _credits_for_order(attrs: dict) -> tuple[int, str]:
    """Resolve (credits, tier_label) from a Lemon Squeezy order's attributes.

    Priority: product_name match -> total_usd match -> default fallback.
    Returns (0, "unrecognized") if the order has no usable signal at all.
    """
    item = (attrs.get("first_order_item") or {}) if isinstance(attrs, dict) else {}
    product_name = (item.get("product_name") or "").strip().lower()
    if product_name:
        for key, credits in TIER_BY_PRODUCT_NAME.items():
            if key in product_name:
                return credits, item.get("product_name") or key

    total_usd = attrs.get("total_usd")
    if isinstance(total_usd, (int, float)):
        cents = int(total_usd)
        if cents in TIER_BY_PRICE_USD_CENTS:
            return TIER_BY_PRICE_USD_CENTS[cents], f"${cents/100:.2f}"

    if attrs.get("status") == "paid" or attrs.get("user_email"):
        return DEFAULT_TIER_CREDITS, "default"

    return 0, "unrecognized"


# ------------------------------------------------------------------ HMAC ----
def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification of the raw webhook body."""
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    # Some providers send "sha256=<hex>" — strip the prefix if present.
    sig = signature[7:] if signature.lower().startswith("sha256=") else signature
    try:
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


# ------------------------------------------------------------- credit ops ---
def grant_credits(session: Session, user_id: int, amount: int) -> int:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"user {user_id} not found")
    user.credits = (user.credits or 0) + amount
    session.flush()
    return user.credits


def deduct_credit_atomic(session: Session, user_id: int, amount: int = 1) -> int:
    """Deduct `amount` credits in a single UPDATE — race-safe.

    The WHERE clause `credits >= amount` guarantees we never go negative even
    under concurrent requests; rowcount==0 means the deduction was rejected.
    Returns the *new* balance on success; raises InsufficientCredits otherwise.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")

    stmt = (
        update(User)
        .where(User.id == user_id, User.credits >= amount)
        .values(credits=User.credits - amount)
    )
    result = session.execute(stmt)
    if result.rowcount == 0:
        raise InsufficientCredits(
            f"user {user_id} has insufficient credits for deduction of {amount}"
        )
    session.flush()
    user = session.get(User, user_id)
    if user is not None:
        session.refresh(user)
    return int(user.credits) if user else 0


# ------------------------------------------------------- webhook handling ---
def _extract_event_id(payload: dict) -> str | None:
    meta = payload.get("meta") or {}
    # Lemon Squeezy puts a fresh event_id under meta on every delivery
    eid = meta.get("event_id")
    if eid:
        return str(eid)
    # Fallback to the resource id (e.g. order id) — still unique per resource.
    data = payload.get("data") or {}
    if data.get("id"):
        return f"{data.get('type', 'data')}-{data['id']}"
    return None


def _record_event(
    session: Session, event_id: str, event_name: str | None
) -> bool:
    """Insert event_id into webhook_events. Returns True if newly recorded,
    False if it was already present (replay)."""
    existing = (
        session.query(WebhookEvent)
        .filter_by(event_id=event_id)
        .one_or_none()
    )
    if existing is not None:
        return False
    session.add(WebhookEvent(
        provider=PROVIDER, event_id=event_id, event_name=event_name,
    ))
    session.flush()
    return True


def handle_lemonsqueezy_event(session: Session, payload: dict) -> dict:
    """Dispatch a verified Lemon Squeezy event. Idempotent — replays no-op."""
    meta = payload.get("meta") or {}
    event_name = meta.get("event_name")
    event_id = _extract_event_id(payload)

    if not event_id:
        return {"handled": False, "reason": "missing event_id"}

    if not _record_event(session, event_id, event_name):
        return {"handled": False, "reason": "duplicate", "event_id": event_id}

    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}

    if event_name == "order_created":
        email = attrs.get("user_email") or attrs.get("email")
        if not email:
            return {"handled": True, "reason": "no email", "event_id": event_id}

        # Resolve which tier was purchased BEFORE granting any credits.
        credits_to_add, tier = _credits_for_order(attrs)
        if credits_to_add <= 0:
            return {
                "handled": True, "reason": "unrecognized tier",
                "event_id": event_id, "email": email,
            }

        user = crud.get_user_by_email(session, email)
        created = False
        if user is None:
            # Guest-checkout: provision the account on first paid order.
            user = crud.create_user(session, email, "lemonsqueezy-managed")
            created = True
        new_balance = grant_credits(session, user.id, credits_to_add)
        return {
            "handled": True, "event_id": event_id,
            "user_id": user.id, "email": email,
            "tier": tier, "credits_added": credits_to_add,
            "balance": new_balance, "user_created": created,
        }

    if event_name in ("subscription_created", "subscription_updated"):
        email = attrs.get("user_email") or attrs.get("email")
        status = (attrs.get("status") or "active").lower()
        sub_id = str(data.get("id") or "")
        if not email:
            return {"handled": True, "reason": "no email", "event_id": event_id}
        user = crud.get_user_by_email(session, email)
        if user is None:
            return {"handled": True, "reason": "unknown user", "event_id": event_id}
        sub = (
            session.query(Subscription)
            .filter_by(user_id=user.id)
            .one_or_none()
        )
        if sub is None:
            sub = Subscription(user_id=user.id, plan="pro")
            session.add(sub)
        sub.provider_subscription_id = sub_id or sub.provider_subscription_id
        try:
            sub.status = SubscriptionStatus(status)
        except ValueError:
            sub.status = SubscriptionStatus.ACTIVE
        session.flush()
        return {
            "handled": True, "event_id": event_id,
            "user_id": user.id, "status": sub.status.value,
        }

    return {"handled": True, "reason": f"ignored event {event_name}",
            "event_id": event_id}
