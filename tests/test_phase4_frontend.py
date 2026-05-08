import os
import pytest
from fastapi.testclient import TestClient

os.environ["MOCK_AI"] = "True"

from app.database import Base, make_engine, make_session_factory
import app.database as db_module
import app.main as main_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    eng = make_engine(f"sqlite:///{tmp_path/'test.db'}")
    Base.metadata.create_all(bind=eng)
    factory = make_session_factory(eng)
    monkeypatch.setattr(db_module, "engine", eng)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    with TestClient(main_module.app) as c:
        yield c
    eng.dispose()


def test_static_index_served(client):
    r = client.get("/static/index.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "<html" in body.lower()
    assert "MediaRecorder" in body
    assert "/api/record" in body
    assert "credits_remaining" in body  # frontend renders credits from response


def test_static_index_has_no_language_dropdown(client):
    """Phase 4 rollback — UI is English-only, no language selector."""
    body = client.get("/static/index.html").text
    assert 'id="lang"' not in body
    assert "<select" not in body
    assert "fd.append('language'" not in body


def test_static_root_html_index_served(client):
    r = client.get("/static/")
    assert r.status_code == 200
    assert "MediaRecorder" in r.text


def test_static_missing_file_404(client):
    r = client.get("/static/does-not-exist.js")
    assert r.status_code == 404
