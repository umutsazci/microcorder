import pytest

from app.database import Base, make_engine, make_session_factory


@pytest.fixture()
def engine():
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture()
def session(session_factory):
    s = session_factory()
    try:
        yield s
    finally:
        s.close()
