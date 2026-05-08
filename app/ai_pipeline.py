"""WhisperX pipeline: Whisper transcription + Wav2Vec2 alignment + pyannote diarization.

Premium feature — billed at 2 credits/call. Output includes per-segment speaker
labels (SPEAKER_00, SPEAKER_01, ...).

Set MOCK_AI=True to bypass model loads entirely (tests + UI dev). Real inference
requires HF_TOKEN in .env after accepting model terms at:
  - https://huggingface.co/pyannote/speaker-diarization-3.1
  - https://huggingface.co/pyannote/segmentation-3.0
"""
import os
import hashlib
from typing import Any, TypedDict


class Segment(TypedDict, total=False):
    start: float
    end: float
    text: str
    speaker: str


class PipelineResult(TypedDict):
    transcript: str
    summary: str
    duration_ms: int
    model: str
    mocked: bool
    backend: str
    segments: list[dict]
    num_speakers: int


# Environment-driven config — sane defaults for CPU + int8 quantization.
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_BATCH_SIZE = int(os.environ.get("WHISPER_BATCH_SIZE", "8"))
# Pin diarization to v3.1 (the model the user accepted). whisperx 3.8.5
# defaults to `speaker-diarization-community-1` which requires separate gating.
DIARIZATION_MODEL = os.environ.get(
    "DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1",
)


def _is_mock() -> bool:
    return os.environ.get("MOCK_AI", "False").lower() in {"1", "true", "yes"}


def _hf_token() -> str:
    tok = (os.environ.get("HF_TOKEN") or "").strip()
    if not tok:
        raise RuntimeError(
            "HF_TOKEN is missing. Speaker diarization requires a Hugging Face "
            "token after accepting the gated model terms at "
            "https://huggingface.co/pyannote/speaker-diarization-3.1 and "
            "https://huggingface.co/pyannote/segmentation-3.0. "
            "Add HF_TOKEN=<token> to .env."
        )
    return tok


# Lazy module-level cache — we never reload these between requests.
_cache: dict[str, Any] = {}


def _load_transcribe_model():
    if "transcribe" in _cache:
        return _cache["transcribe"]
    import whisperx
    _cache["transcribe"] = whisperx.load_model(
        WHISPER_MODEL,
        device=WHISPER_DEVICE,
        compute_type=WHISPER_COMPUTE_TYPE,
    )
    return _cache["transcribe"]


def _load_align_model(language: str):
    key = f"align:{language}"
    if key in _cache:
        return _cache[key]
    import whisperx
    model, meta = whisperx.load_align_model(
        language_code=language, device=WHISPER_DEVICE,
    )
    _cache[key] = (model, meta)
    return _cache[key]


def _load_diarization_pipeline():
    if "diarize" in _cache:
        return _cache["diarize"]
    from whisperx.diarize import DiarizationPipeline
    # whisperx 3.8.5: `use_auth_token` -> `token`; default `model_name` is
    # `speaker-diarization-community-1` which requires its own gating, so we
    # pin to `speaker-diarization-3.1` (env-overridable).
    _cache["diarize"] = DiarizationPipeline(
        model_name=DIARIZATION_MODEL,
        token=_hf_token(),
        device=WHISPER_DEVICE,
    )
    return _cache["diarize"]


def _decode_to_pcm16k(audio: bytes):
    """Decode any browser-supported audio container to 16kHz mono float32 PCM."""
    import subprocess
    import numpy as np
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error",
         "-i", "pipe:0",
         "-f", "s16le", "-ac", "1", "-ar", "16000",
         "pipe:1"],
        input=audio, capture_output=True, check=True,
    )
    samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return samples


def _mock_run(audio: bytes) -> dict:
    digest = hashlib.sha256(audio).hexdigest()[:8]
    text = f"mock whisperx transcript for blob {digest} ({len(audio)} bytes)"
    return {
        "transcript": text,
        "model": "mock-whisperx",
        "segments": [{
            "start": 0.0, "end": 1.5, "text": text, "speaker": "SPEAKER_00",
        }],
        "num_speakers": 1,
    }


def _real_run(audio: bytes | None = None, audio_path: str | None = None) -> dict:
    """The four-step WhisperX pipeline: transcribe -> align -> diarize -> assign.

    Pass either raw bytes (decoded via ffmpeg subprocess) or a file path
    (loaded via whisperx.load_audio — preferred when /api/record has already
    written the upload to a temp file).
    """
    import whisperx

    if audio_path is not None:
        samples = whisperx.load_audio(audio_path)
    else:
        samples = _decode_to_pcm16k(audio or b"")
    if samples.size == 0:
        return {"transcript": "", "model": WHISPER_MODEL,
                "segments": [], "num_speakers": 0}

    # Step 1 — Whisper transcription (faster-whisper backend, batched)
    transcribe_model = _load_transcribe_model()
    result = transcribe_model.transcribe(samples, batch_size=WHISPER_BATCH_SIZE)
    language = result.get("language") or "en"

    # Step 2 — Wav2Vec2 forced alignment (gives word-level timestamps)
    align_model, align_meta = _load_align_model(language)
    aligned = whisperx.align(
        result["segments"], align_model, align_meta,
        samples, WHISPER_DEVICE, return_char_alignments=False,
    )

    # Step 3 — pyannote speaker diarization (requires HF_TOKEN)
    diar_pipe = _load_diarization_pipeline()
    diar_segments = diar_pipe(samples)

    # Step 4 — merge: each aligned segment gets a speaker label
    final = whisperx.assign_word_speakers(diar_segments, aligned)

    segs: list[dict] = []
    speakers: set[str] = set()
    for s in final.get("segments", []):
        speaker = s.get("speaker") or "SPEAKER_?"
        speakers.add(speaker)
        segs.append({
            "start": float(s.get("start") or 0.0),
            "end": float(s.get("end") or 0.0),
            "text": (s.get("text") or "").strip(),
            "speaker": speaker,
        })

    transcript = " ".join(seg["text"] for seg in segs).strip()
    return {
        "transcript": transcript,
        "model": WHISPER_MODEL,
        "segments": segs,
        "num_speakers": len(speakers),
    }


def summarize(transcript: str) -> str:
    if not transcript:
        return ""
    for delim in (". ", "? ", "! "):
        if delim in transcript:
            return transcript.split(delim, 1)[0].strip() + "."
    tokens = transcript.split()
    if len(tokens) <= 12:
        return transcript.strip()
    return " ".join(tokens[:12]).strip() + "..."


def run_pipeline(audio: bytes | None = None,
                 audio_path: str | None = None) -> PipelineResult:
    """Audio bytes or file path -> WhisperX (transcribe + align + diarize)."""
    import time

    if audio is None and audio_path is None:
        raise ValueError("provide audio bytes or audio_path")
    if audio is not None:
        if not isinstance(audio, (bytes, bytearray)):
            raise TypeError("audio must be bytes")
        if len(audio) == 0:
            raise ValueError("audio blob is empty")

    started = time.perf_counter()
    mocked = _is_mock()
    if mocked:
        if audio is None:
            with open(audio_path, "rb") as f:
                audio = f.read()
        out = _mock_run(bytes(audio))
    else:
        out = _real_run(audio=bytes(audio) if audio else None,
                        audio_path=audio_path)
    summary = summarize(out["transcript"])
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    return {
        "transcript": out["transcript"],
        "summary": summary,
        "duration_ms": elapsed_ms,
        "model": out["model"],
        "mocked": mocked,
        "backend": "whisperx",
        "segments": out["segments"],
        "num_speakers": out["num_speakers"],
    }
