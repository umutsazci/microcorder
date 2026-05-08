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


def test_root_serves_index_html(client):
    """Frontend now lives at repo root (so GitHub Pages can serve it)."""
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "<html" in body.lower()
    assert "MediaRecorder" in body
    assert "/api/record" in body
    assert "credits_remaining" in body


def test_index_has_no_language_dropdown(client):
    body = client.get("/").text
    assert 'id="lang"' not in body
    assert "<select" not in body
    assert "fd.append('language'" not in body


def test_old_static_path_404(client):
    """Old `/static/...` mount has been removed."""
    r = client.get("/static/index.html")
    assert r.status_code == 404
