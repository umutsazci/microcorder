"""Standalone Modal serverless transcription microservice.

Pure AI engine — no database, no auth, no billing. The main backend (running
elsewhere) authenticates the user, deducts credits, and forwards the audio
blob to this engine's `POST /transcribe`. The engine returns plain text.

Deploy:
    modal deploy modal_engine.py
"""
import modal


app = modal.App("microcorder-ai-engine")


# Tiny image — only what faster-whisper needs.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi",
        "faster-whisper",
        "python-multipart",
        "uvicorn",
    )
)

# Persist the ~150 MB faster-whisper model between cold starts.
model_cache = modal.Volume.from_name(
    "microcorder-engine-cache", create_if_missing=True,
)


@app.function(
    image=image,
    gpu="T4",
    timeout=600,
    volumes={"/root/.cache/huggingface": model_cache},
)
@modal.asgi_app()
def fastapi_engine():
    """Build + return the FastAPI app. Runs once per container; the loaded
    Whisper model is reused for every request the container serves."""
    import os
    import tempfile
    from fastapi import FastAPI, UploadFile, File, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from faster_whisper import WhisperModel

    # Load once on container start. T4 supports float16; CPU fallback uses int8.
    model = WhisperModel(
        os.environ.get("WHISPER_MODEL", "base"),
        device=os.environ.get("WHISPER_DEVICE", "cuda"),
        compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "float16"),
    )

    web = FastAPI(title="Microcorder AI Engine", version="1.0")

    # Open CORS — this is an internal microservice. Lock down to your core
    # backend's origin once that lands.
    web.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @web.get("/")
    def health():
        return {"service": "microcorder-ai-engine", "ok": True}

    @web.post("/transcribe")
    async def transcribe(file: UploadFile = File(...)):
        blob = await file.read()
        if not blob:
            raise HTTPException(status_code=400, detail="empty audio blob")

        suffix = ""
        if file.filename and "." in file.filename:
            suffix = "." + file.filename.rsplit(".", 1)[-1].lower()[:8]
        fd, path = tempfile.mkstemp(suffix=suffix or ".audio")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(blob)
            segments, _info = model.transcribe(
                path,
                beam_size=int(os.environ.get("WHISPER_BEAM_SIZE", "1")),
                vad_filter=True,
                condition_on_previous_text=False,
                temperature=0.0,
                without_timestamps=True,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"transcription failed: {exc}")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        return {"text": text}

    return web
