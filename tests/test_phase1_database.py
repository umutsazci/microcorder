import pytest
from sqlalchemy.exc import IntegrityError

from app import crud
from app.database import session_scope
from app.models import (
    User, Subscription, Task, WebhookEvent,
    SubscriptionStatus, TaskStatus,
)


def test_create_user_starts_with_zero_credits(session):
    user = crud.create_user(session, "alice@example.com", "hash")
    session.commit()

    assert user.id is not None
    assert user.credits == 0


def test_user_email_unique(session):
    crud.create_user(session, "dup@example.com", "h1")
    session.commit()
    with pytest.raises(IntegrityError):
        crud.create_user(session, "dup@example.com", "h2")
    session.rollback()


def test_subscription_relationship(session):
    user = crud.create_user(session, "bob@example.com", "h")
    sub = Subscription(
        user_id=user.id,
        plan="pro",
        status=SubscriptionStatus.ACTIVE,
        provider_subscription_id="sub_123",
    )
    session.add(sub)
    session.commit()

    assert user.subscription.plan == "pro"
    assert user.subscription.status == SubscriptionStatus.ACTIVE
    assert sub.user.email == "bob@example.com"


def test_foreign_key_violation_on_task(session):
    bad = Task(user_id=99999, status=TaskStatus.PENDING)
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_cascade_delete_user_removes_children(session):
    user = crud.create_user(session, "carol@example.com", "h", credits=10)
    session.add(Subscription(user_id=user.id, plan="free",
                             status=SubscriptionStatus.TRIALING))
    crud.create_task(session, user.id)
    session.commit()
    uid = user.id

    crud.delete_user(session, uid)
    session.commit()

    assert session.query(User).filter_by(id=uid).one_or_none() is None
    assert session.query(Subscription).filter_by(user_id=uid).count() == 0
    assert session.query(Task).filter_by(user_id=uid).count() == 0


def test_user_credits_default_zero_and_assignable(session):
    user = crud.create_user(session, "dan@example.com", "h")
    assert user.credits == 0
    user.credits = 7
    session.commit()
    refetched = session.get(User, user.id)
    assert refetched.credits == 7


def test_session_rollback_on_error(session_factory):
    with pytest.raises(RuntimeError):
        with session_scope(session_factory) as s:
            crud.create_user(s, "rollback@example.com", "h")
            raise RuntimeError("boom")

    with session_scope(session_factory) as s:
        assert crud.get_user_by_email(s, "rollback@example.com") is None


def test_session_scope_commits_on_success(session_factory):
    with session_scope(session_factory) as s:
        crud.create_user(s, "ok@example.com", "h")

    with session_scope(session_factory) as s:
        u = crud.get_user_by_email(s, "ok@example.com")
        assert u is not None and u.email == "ok@example.com"


def test_unique_subscription_per_user(session):
    user = crud.create_user(session, "eve@example.com", "h")
    session.add(Subscription(user_id=user.id, plan="a",
                             status=SubscriptionStatus.ACTIVE))
    session.commit()
    session.add(Subscription(user_id=user.id, plan="b",
                             status=SubscriptionStatus.ACTIVE))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_task_crud_round_trip(session):
    user = crud.create_user(session, "frank@example.com", "h")
    task = crud.create_task(session, user.id)
    session.commit()

    fetched = crud.get_task(session, task.id)
    assert fetched is not None
    assert fetched.status == TaskStatus.PENDING
    assert fetched.user.email == "frank@example.com"


def test_webhook_event_unique_event_id(session):
    session.add(WebhookEvent(provider="lemonsqueezy", event_id="evt_1"))
    session.commit()
    session.add(WebhookEvent(provider="lemonsqueezy", event_id="evt_1"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
