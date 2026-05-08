import io
import os

os.environ["MOCK_AI"] = "True"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, make_engine, make_session_factory
import app.database as db_module
import app.main as main_module


TEST_USER = "test@example.com"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    eng = make_engine(f"sqlite:///{tmp_path/'test.db'}")
    Base.metadata.create_all(bind=eng)
    factory = make_session_factory(eng)

    monkeypatch.setattr(db_module, "engine", eng)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    # Stub audio-duration measurement: pydub can't parse our fake bytes.
    monkeypatch.setattr(main_module, "measure_audio_minutes", lambda path: 1)

    with TestClient(main_module.app) as c:
        # Header-based identity is required by the auth-less identity scheme.
        c.headers["X-User-Email"] = TEST_USER
        yield c

    eng.dispose()


def _grant(factory, email, credits):
    """Helper: ensure user exists with the given balance."""
    from app import crud
    with factory() as s:
        u = crud.get_user_by_email(s, email)
        if u is None:
            u = crud.create_user(s, email, "test", credits=credits)
        else:
            u.credits = credits
        s.commit()


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_root_serves_spa(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "MediaRecorder" in r.text


def test_record_runs_pipeline_and_deducts_credits(client):
    # New users start at 0 — explicitly grant credits before recording.
    _grant(main_module.SessionLocal, TEST_USER, 5)

    fake_audio = b"RIFF....fakewav"
    r = client.post(
        "/api/record",
        files={"file": ("a.wav", io.BytesIO(fake_audio), "audio/wav")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert isinstance(body["transcript"], str) and body["transcript"]
    assert isinstance(body["summary"], str)
    # Per-minute billing: 1 min x 2 credits = 2; granted 5 - 2 = 3.
    assert body["minutes"] == 1
    assert body["credits_charged"] == 2
    assert body["credits_remaining"] == 3
    assert isinstance(body["segments"], list)
    assert body["num_speakers"] >= 1


def test_record_rejects_empty_blob(client):
    r = client.post(
        "/api/record",
        files={"file": ("a.wav", io.BytesIO(b""), "audio/wav")},
    )
    assert r.status_code == 400


def test_record_402_when_below_per_minute_threshold(client):
    """1 credit isn't enough for 1-minute clip billed at 2 credits/min."""
    _grant(main_module.SessionLocal, TEST_USER, 1)

    r = client.post(
        "/api/record",
        files={"file": ("a.wav", io.BytesIO(b"y"), "audio/wav")},
    )
    assert r.status_code == 402
    assert "2" in r.json()["detail"]


def test_record_rejects_missing_auth_header(tmp_path, monkeypatch):
    """No X-User-Email -> 401 (security: no shared default account)."""
    eng = make_engine(f"sqlite:///{tmp_path/'test.db'}")
    Base.metadata.create_all(bind=eng)
    factory = make_session_factory(eng)
    monkeypatch.setattr(db_module, "engine", eng)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "measure_audio_minutes", lambda path: 1)

    with TestClient(main_module.app) as c:
        # NB: no X-User-Email header set
        r = c.post(
            "/api/record",
            files={"file": ("a.wav", io.BytesIO(b"abc"), "audio/wav")},
        )
        assert r.status_code == 401
    eng.dispose()


def test_status_returns_completed_task(client):
    _grant(main_module.SessionLocal, TEST_USER, 5)
    r = client.post(
        "/api/record",
        files={"file": ("a.wav", io.BytesIO(b"abc"), "audio/wav")},
    )
    task_id = r.json()["task_id"]

    s = client.get(f"/api/status/{task_id}")
    assert s.status_code == 200
    body = s.json()
    assert body["task_id"] == task_id
    assert body["status"] == "completed"
    assert body["transcript"]


def test_status_404_for_unknown_task(client):
    r = client.get("/api/status/999999")
    assert r.status_code == 404
