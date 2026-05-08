import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, Enum, Boolean, Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    CANCELED = "canceled"
    PAST_DUE = "past_due"
    TRIALING = "trialing"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    credits = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    subscription = relationship(
        "Subscription", back_populates="user", uselist=False,
        cascade="all, delete-orphan",
    )
    tasks = relationship(
        "Task", back_populates="user", cascade="all, delete-orphan",
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     unique=True, nullable=False)
    provider_subscription_id = Column(String(255), unique=True, nullable=True)
    plan = Column(String(64), nullable=False, default="free")
    status = Column(Enum(SubscriptionStatus), nullable=False,
                    default=SubscriptionStatus.TRIALING)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    user = relationship("User", back_populates="subscription")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    status = Column(Enum(TaskStatus), nullable=False, default=TaskStatus.PENDING)
    transcript = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="tasks")


class WebhookEvent(Base):
    """Idempotency log for inbound provider webhooks."""

    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True)
    provider = Column(String(64), nullable=False)
    event_id = Column(String(255), nullable=False, unique=True, index=True)
    event_name = Column(String(128), nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
