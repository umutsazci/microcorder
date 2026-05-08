"""SQLAlchemy bootstrap for the core backend.

Reads `DATABASE_URL` from .env (Neon Postgres in prod, SQLite locally if unset).
Exposes `Base`, `engine`, `SessionLocal`, and a FastAPI-friendly `get_db()`.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./microcorder.db")

# pool_pre_ping=True keeps Neon's auto-suspended compute happy by validating
# pooled connections before each checkout.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
