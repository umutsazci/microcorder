import io
import os

os.environ["MOCK_AI"] = "True"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, make_engine, make_session_factory
import app.database as db_module
import app.main as main_module
from app.models import User


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
        yield c

    eng.dispose()


def test_root_ok(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_record_runs_pipeline_and_deducts_credits(client):
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
    # Per-minute billing: 1 min x 2 credits = 2; bootstrap 5 - 2 = 3.
    assert body["minutes"] == 1
    assert body["credits_charged"] == 2
    assert body["credits_remaining"] == 3
    # Diarization output threaded through the response
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
    from app.database import SessionLocal
    import os
    demo_email = os.environ.get("DEMO_USER_EMAIL", "demo@example.com")

    # Trigger demo user creation
    client.post(
        "/api/record",
        files={"file": ("a.wav", io.BytesIO(b"x"), "audio/wav")},
    )

    with SessionLocal() as s:
        user = s.query(User).filter_by(email=demo_email).one()
        user.credits = 1
        s.commit()

    r = client.post(
        "/api/record",
        files={"file": ("a.wav", io.BytesIO(b"y"), "audio/wav")},
    )
    assert r.status_code == 402
    assert "2" in r.json()["detail"]


def test_status_returns_completed_task(client):
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
