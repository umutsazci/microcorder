import os
import pytest

os.environ["MOCK_AI"] = "True"

from app.ai_pipeline import run_pipeline, summarize  # noqa: E402


REQUIRED_KEYS = {
    "transcript", "summary", "duration_ms", "model", "mocked",
    "backend", "segments", "num_speakers",
}


def test_pipeline_returns_required_schema():
    result = run_pipeline(b"\x00\x01\x02\x03")
    assert set(result.keys()) == REQUIRED_KEYS
    assert isinstance(result["transcript"], str)
    assert isinstance(result["summary"], str)
    assert isinstance(result["duration_ms"], int)
    assert isinstance(result["model"], str)
    assert isinstance(result["mocked"], bool)
    assert isinstance(result["backend"], str)
    assert result["backend"] == "whisperx"
    assert isinstance(result["segments"], list)
    assert isinstance(result["num_speakers"], int)


def test_pipeline_mocked_flag_true_when_env_set(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "True")
    result = run_pipeline(b"audio-blob")
    assert result["mocked"] is True
    assert result["model"].startswith("mock")
    assert "mock whisperx transcript" in result["transcript"]
    # Mock pipeline emits a single fake speaker segment
    assert result["num_speakers"] == 1
    assert len(result["segments"]) >= 1
    assert result["segments"][0]["speaker"].startswith("SPEAKER_")


def test_pipeline_deterministic_for_same_input():
    a = run_pipeline(b"same-audio")
    b = run_pipeline(b"same-audio")
    assert a["transcript"] == b["transcript"]


def test_pipeline_rejects_empty_audio():
    with pytest.raises(ValueError):
        run_pipeline(b"")


def test_pipeline_rejects_non_bytes():
    with pytest.raises(TypeError):
        run_pipeline("not-bytes")  # type: ignore[arg-type]


def test_summarize_first_sentence():
    assert summarize("hello world. and more text.") == "hello world."


def test_summarize_short_text():
    assert summarize("short text") == "short text"


def test_summarize_long_text_truncated():
    text = " ".join(f"w{i}" for i in range(30))
    out = summarize(text)
    assert out.endswith("...")
    assert len(out.split()) <= 13


def test_pipeline_summary_non_empty_for_non_empty_input():
    result = run_pipeline(b"some-bytes")
    assert result["summary"]
