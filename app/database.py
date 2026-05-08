from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DEFAULT_DB_URL = "sqlite:///./app.db"

Base = declarative_base()


def make_engine(url: str = DEFAULT_DB_URL, **kwargs):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


engine = make_engine()
SessionLocal = make_session_factory(engine)


def init_db(eng=None):
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=eng or engine)


@contextmanager
def session_scope(factory=None):
    factory = factory or SessionLocal
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
