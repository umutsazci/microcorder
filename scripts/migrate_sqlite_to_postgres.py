"""One-shot migration: copy users + webhook_events + subscriptions from the
local SQLite app.db into the configured Neon Postgres database.

Idempotent — safe to re-run. Existing rows are updated, not duplicated.
Tasks are intentionally skipped (operational data, not worth carrying over).

Usage:
    python scripts/migrate_sqlite_to_postgres.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

if not os.environ.get("DATABASE_URL"):
    sys.exit("DATABASE_URL must be set (target Postgres) — abort.")

from app.database import make_engine, make_session_factory, Base
from app.models import User, WebhookEvent, Subscription


def main() -> int:
    src_db = ROOT / "app.db"
    if not src_db.exists():
        print(f"no SQLite source at {src_db} — nothing to migrate")
        return 0

    src = make_session_factory(make_engine(f"sqlite:///{src_db}"))
    dst_engine = make_engine()                 # uses DATABASE_URL
    Base.metadata.create_all(bind=dst_engine)  # ensure target schema
    dst = make_session_factory(dst_engine)

    s_users = users_imported = 0
    s_events = events_imported = 0
    s_subs = subs_imported = 0

    with src() as src_s, dst() as dst_s:
        # ---- users (upsert by email)
        for u in src_s.query(User).all():
            s_users += 1
            existing = (
                dst_s.query(User).filter_by(email=u.email).one_or_none()
            )
            if existing:
                existing.credits = u.credits
                existing.is_active = u.is_active
                existing.hashed_password = u.hashed_password
            else:
                dst_s.add(User(
                    email=u.email,
                    hashed_password=u.hashed_password,
                    is_active=u.is_active,
                    credits=u.credits,
                    created_at=u.created_at,
                ))
                users_imported += 1
        dst_s.commit()

        # ---- webhook_events (insert if event_id new)
        for e in src_s.query(WebhookEvent).all():
            s_events += 1
            exists = (
                dst_s.query(WebhookEvent)
                .filter_by(event_id=e.event_id).one_or_none()
            )
            if exists:
                continue
            dst_s.add(WebhookEvent(
                provider=e.provider,
                event_id=e.event_id,
                event_name=e.event_name,
                received_at=e.received_at,
            ))
            events_imported += 1
        dst_s.commit()

        # ---- subscriptions (link by email)
        for sub in src_s.query(Subscription).all():
            s_subs += 1
            src_user = src_s.get(User, sub.user_id)
            if src_user is None:
                continue
            dst_user = (
                dst_s.query(User).filter_by(email=src_user.email).one_or_none()
            )
            if dst_user is None:
                continue
            existing_sub = (
                dst_s.query(Subscription)
                .filter_by(user_id=dst_user.id).one_or_none()
            )
            if existing_sub:
                existing_sub.plan = sub.plan
                existing_sub.status = sub.status
                existing_sub.provider_subscription_id = sub.provider_subscription_id
            else:
                dst_s.add(Subscription(
                    user_id=dst_user.id,
                    provider_subscription_id=sub.provider_subscription_id,
                    plan=sub.plan,
                    status=sub.status,
                    created_at=sub.created_at,
                ))
                subs_imported += 1
        dst_s.commit()

    print(f"users:         scanned {s_users:>3}  inserted {users_imported}")
    print(f"webhook events: scanned {s_events:>3}  inserted {events_imported}")
    print(f"subscriptions: scanned {s_subs:>3}  inserted {subs_imported}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
