import os
import json
import hmac
import hashlib

os.environ["MOCK_AI"] = "True"
os.environ.setdefault("LEMON_SQUEEZY_WEBHOOK_SECRET", "test-secret-please-change")

import pytest
from fastapi.testclient import TestClient

from app import crud
from app.billing import (
    verify_signature,
    grant_credits,
    deduct_credit_atomic,
    handle_lemonsqueezy_event,
    InsufficientCredits,
)
from app.models import User, WebhookEvent
from app.database import Base, make_engine, make_session_factory
import app.database as db_module
import app.main as main_module


SECRET = os.environ["LEMON_SQUEEZY_WEBHOOK_SECRET"]


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------- direct ----
def test_verify_signature_valid_and_invalid():
    body = b'{"hello":"world"}'
    good = _sign(body)
    assert verify_signature(body, good, SECRET) is True
    assert verify_signature(body, "0" * 64, SECRET) is False
    assert verify_signature(body, None, SECRET) is False
    assert verify_signature(body, good, "") is False


def test_verify_signature_handles_sha256_prefix():
    body = b"payload"
    sig = "sha256=" + _sign(body)
    assert verify_signature(body, sig, SECRET) is True


def test_deduct_credit_atomic_success(session):
    user = crud.create_user(session, "a@b.com", "h")
    grant_credits(session, user.id, 3)
    new_balance = deduct_credit_atomic(session, user.id)
    assert new_balance == 2


def test_deduct_credit_atomic_raises_when_zero(session):
    user = crud.create_user(session, "z@b.com", "h")
    with pytest.raises(InsufficientCredits):
        deduct_credit_atomic(session, user.id)
    refetched = session.get(User, user.id)
    assert refetched.credits == 0


def test_deduct_credit_atomic_does_not_go_negative(session):
    user = crud.create_user(session, "p@b.com", "h", credits=2)
    with pytest.raises(InsufficientCredits):
        deduct_credit_atomic(session, user.id, amount=5)
    refetched = session.get(User, user.id)
    assert refetched.credits == 2  # unchanged


def test_order_created_grants_credits_to_existing_user(session):
    user = crud.create_user(session, "buyer@example.com", "h")
    session.commit()

    event = {
        "meta": {"event_name": "order_created", "event_id": "evt_1"},
        "data": {"type": "orders", "id": "order_42",
                 "attributes": {"user_email": "buyer@example.com"}},
    }
    out = handle_lemonsqueezy_event(session, event)
    assert out["handled"] is True
    assert out["credits_added"] == 100
    assert out["balance"] == 100

    refetched = session.get(User, user.id)
    assert refetched.credits == 100


def test_order_created_auto_provisions_unknown_user(session):
    """Guest-checkout: an order_created for an unknown email creates the user
    and grants credits."""
    event = {
        "meta": {"event_name": "order_created", "event_id": "evt_2"},
        "data": {"type": "orders", "id": "order_99",
                 "attributes": {"user_email": "nobody@example.com"}},
    }
    out = handle_lemonsqueezy_event(session, event)
    assert out["handled"] is True
    assert out["user_created"] is True
    assert out["credits_added"] == 100
    user = crud.get_user_by_email(session, "nobody@example.com")
    assert user is not None and user.credits == 100
    assert session.query(WebhookEvent).filter_by(event_id="evt_2").count() == 1


def test_replayed_event_is_idempotent(session):
    user = crud.create_user(session, "replay@example.com", "h")
    session.commit()

    event = {
        "meta": {"event_name": "order_created", "event_id": "evt_replay"},
        "data": {"type": "orders", "id": "order_1",
                 "attributes": {"user_email": "replay@example.com"}},
    }
    handle_lemonsqueezy_event(session, event)
    out2 = handle_lemonsqueezy_event(session, event)
    assert out2["handled"] is False
    assert out2["reason"] == "duplicate"

    refetched = session.get(User, user.id)
    assert refetched.credits == 100  # only credited once


def test_subscription_event_creates_subscription(session):
    crud.create_user(session, "sub@example.com", "h")
    session.commit()
    event = {
        "meta": {"event_name": "subscription_created", "event_id": "evt_sub_1"},
        "data": {"type": "subscriptions", "id": "sub_xyz",
                 "attributes": {"user_email": "sub@example.com",
                                "status": "active"}},
    }
    out = handle_lemonsqueezy_event(session, event)
    assert out["handled"] is True
    assert out["status"] == "active"


# --------------------------------------------------------- HTTP webhook ----
@pytest.fixture()
def client(tmp_path, monkeypatch):
    eng = make_engine(f"sqlite:///{tmp_path/'test.db'}")
    Base.metadata.create_all(bind=eng)
    factory = make_session_factory(eng)

    monkeypatch.setattr(db_module, "engine", eng)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "SessionLocal", factory)

    # Seed a buyer
    with factory() as s:
        crud.create_user(s, "client-buyer@example.com", "h")
        s.commit()

    with TestClient(main_module.app) as c:
        yield c

    eng.dispose()


def _post_webhook(client, payload: dict, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {}
    if signature is not None:
        headers["X-Signature"] = signature
    return client.post(
        "/api/webhooks/lemonsqueezy",
        content=body,
        headers={"content-type": "application/json", **headers},
    )


def test_webhook_rejects_missing_signature(client):
    r = _post_webhook(client, {"hello": "world"})
    assert r.status_code == 401


def test_webhook_rejects_bad_signature(client):
    r = _post_webhook(client, {"hello": "world"}, signature="0" * 64)
    assert r.status_code == 401


def test_webhook_accepts_valid_signature_and_grants_credits(client):
    payload = {
        "meta": {"event_name": "order_created", "event_id": "evt_http_1"},
        "data": {"type": "orders", "id": "ord_http_1",
                 "attributes": {"user_email": "client-buyer@example.com"}},
    }
    body = json.dumps(payload).encode()
    sig = _sign(body)
    r = client.post(
        "/api/webhooks/lemonsqueezy",
        content=body,
        headers={"content-type": "application/json", "X-Signature": sig},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["handled"] is True
    assert out["credits_added"] == 100

    # Replay with same signature/body -> 200 idempotent (no double credit)
    r2 = client.post(
        "/api/webhooks/lemonsqueezy",
        content=body,
        headers={"content-type": "application/json", "X-Signature": sig},
    )
    assert r2.status_code == 200
    assert r2.json()["reason"] == "duplicate"
