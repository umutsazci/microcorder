import math
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal, init_db
from app.models import Task, TaskStatus, User
from app import crud
from app.ai_pipeline import run_pipeline
from app.billing import (
    verify_signature, handle_lemonsqueezy_event,
    deduct_credit_atomic, InsufficientCredits,
)


app = FastAPI(title="AI SaaS")

# CORS — frontend on GitHub Pages calls the Modal backend cross-origin.
# Allow the Pages origin plus localhost for dev. No cookies → credentials off.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://umutsazci.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Repo root — index.html lives here so GitHub Pages can serve it directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_FILE = REPO_ROOT / "index.html"


@app.on_event("startup")
def _startup():
    init_db()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


CREDITS_PER_MINUTE = 2          # Premium WhisperX diarization rate
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB hard cap on /api/record uploads


def _resolve_user_from_request(db: Session, request: Request) -> User:
    """Identify the caller from the X-User-Id header (UUID4 string).

    No real auth — the header is a per-browser UUID stored in localStorage
    under `microcorder_user_id`. Auto-provisions a new user with **0 credits**
    on first sight; visitors must purchase credits to use /api/record.
    The UUID is stored in the legacy `User.email` column (which is just the
    unique identifier for now).
    """
    raw = (
        request.headers.get("X-User-Id")
        or request.headers.get("x-user-id")
        or ""
    ).strip()
    if not raw:
        raise HTTPException(status_code=401, detail="missing X-User-Id header")
    try:
        uid = str(uuid.UUID(raw))  # validate + canonicalise (lowercase)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid X-User-Id (must be UUID)")
    user = crud.get_user_by_email(db, uid)
    if user is None:
        user = crud.create_user(db, uid, "anonymous", credits=0)
        db.commit()
    return user


def measure_audio_minutes(path: str) -> int:
    """Audio duration in whole minutes, rounded up. Minimum 1.

    Uses pydub (which delegates to ffmpeg). Configures pydub to use the
    bundled imageio-ffmpeg binary so we don't need ffmpeg on PATH.
    """
    from pydub import AudioSegment
    import imageio_ffmpeg
    AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()
    seg = AudioSegment.from_file(path)
    seconds = len(seg) / 1000.0
    return max(1, math.ceil(seconds / 60.0))


@app.get("/api/health")
def health():
    return {"service": "ai-saas", "ok": True}


@app.get("/", include_in_schema=False)
def root():
    """Serve the SPA at root — same path GitHub Pages serves it from."""
    if INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE), media_type="text/html")
    raise HTTPException(status_code=404, detail="index.html missing")


@app.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    """Current caller's credit balance + checkout URL for top-ups.

    Identity from `X-User-Email` header — auto-provisions 0-credit users.
    Email is intentionally NOT returned (privacy: clients already know it).
    """
    user = _resolve_user_from_request(db, request)
    return {
        "credits": user.credits,
        # `checkout_url` is the Pro tier (kept for backward compat with older
        # clients that only know one URL).
        "checkout_url": os.environ.get("LEMON_SQUEEZY_CHECKOUT_URL", ""),
        "pricing": {
            "starter": {
                "name": "Starter",
                "price_usd": 4.99,
                "minutes": 30,        # 60 credits / 2 credits per minute
                "credits": 60,
                "checkout_url": os.environ.get("LEMON_SQUEEZY_STARTER_URL", ""),
            },
            "pro": {
                "name": "Pro",
                "price_usd": 9.99,
                "minutes": 50,        # 100 credits / 2 credits per minute
                "credits": 100,
                "checkout_url": os.environ.get("LEMON_SQUEEZY_CHECKOUT_URL", ""),
            },
        },
    }


# ----------------------------------------------------------- AI endpoint ----
@app.post("/api/record")
async def record(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Premium WhisperX pipeline billed per audio-minute.

    Identity is derived from the `X-User-Email` header — anonymous browser ID
    stored client-side. Server refuses arbitrary emails as form params (the
    previous behaviour let any caller spend any user's credits).

    Strict order:
      1. Save the upload to a temp file.
      2. Measure duration -> minutes (ceil, min 1).
      3. Pre-check user credits >= minutes * CREDITS_PER_MINUTE -> else 402.
      4. Run WhisperX (transcribe + Wav2Vec2 align + pyannote diarize).
      5. Atomically deduct minutes * CREDITS_PER_MINUTE.
      6. Delete temp file.
      7. Return transcript + speaker segments + balance.
    """
    # Cheap pre-check: refuse oversize uploads before reading them all into
    # memory. Real check below covers tampered Content-Length.
    content_len = int(request.headers.get("content-length") or 0)
    if content_len and content_len > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="audio file too large (max 25 MB)")

    blob = await file.read()
    if not blob:
        raise HTTPException(status_code=400, detail="empty audio blob")
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="audio file too large (max 25 MB)")

    user = _resolve_user_from_request(db, request)

    # Step 1: persist to a temp file. Suffix preserves the container hint
    # (.webm/.wav/...) so ffmpeg/whisperx auto-detect format.
    suffix = ""
    if file.filename and "." in file.filename:
        suffix = "." + file.filename.rsplit(".", 1)[-1].lower()[:8]
    fd, tmp_path = tempfile.mkstemp(suffix=suffix or ".audio")
    os.close(fd)

    try:
        with open(tmp_path, "wb") as f:
            f.write(blob)

        # Step 2: measure & compute price
        try:
            minutes = measure_audio_minutes(tmp_path)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"could not parse audio: {exc}",
            )
        required = minutes * CREDITS_PER_MINUTE

        # Step 3: pre-check
        db.refresh(user)
        if user.credits < required:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"insufficient credits: need {required} "
                    f"({minutes} min x {CREDITS_PER_MINUTE} credits/min), "
                    f"have {user.credits}"
                ),
            )

        # Step 4: WhisperX (transcribe -> align -> diarize -> assign)
        task = crud.create_task(db, user.id)
        task.status = TaskStatus.PROCESSING
        db.commit()

        try:
            result = run_pipeline(audio_path=tmp_path)
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            db.commit()
            raise HTTPException(status_code=500,
                                detail=f"pipeline failed: {exc}")

        # Step 5: atomic billing — strict; if we lost a concurrent race the
        # caller didn't actually pay, so we refuse the result.
        try:
            new_balance = deduct_credit_atomic(db, user.id, amount=required)
            db.commit()
        except InsufficientCredits:
            db.rollback()
            task.status = TaskStatus.FAILED
            task.error = "insufficient credits at deduction time"
            db.commit()
            raise HTTPException(
                status_code=402,
                detail="credits depleted during processing",
            )

        # Persist completed task
        task.transcript = result["transcript"]
        task.summary = result["summary"]
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now(timezone.utc)
        db.commit()

        return {
            "task_id": task.id,
            "status": task.status.value,
            "transcript": result["transcript"],
            "summary": result["summary"],
            "segments": result.get("segments", []),
            "num_speakers": result.get("num_speakers", 0),
            "minutes": minutes,
            "credits_charged": required,
            "credits_remaining": new_balance,
        }

    finally:
        # Step 6: cleanup temp file unconditionally
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.get("/api/status/{task_id}")
def status(task_id: int, request: Request, db: Session = Depends(get_db)):
    # Auth + ownership: only the user who created the task can read it.
    user = _resolve_user_from_request(db, request)
    task = db.get(Task, task_id)
    if task is None or task.user_id != user.id:
        # Same 404 for non-existent and non-owned tasks — don't leak existence.
        raise HTTPException(status_code=404, detail="task not found")
    return {
        "task_id": task.id,
        "status": task.status.value,
        "transcript": task.transcript,
        "summary": task.summary,
        "error": task.error,
    }


# ------------------------------------------------------------- webhooks -----
@app.post("/api/webhooks/lemonsqueezy")
async def lemonsqueezy_webhook(request: Request, db: Session = Depends(get_db)):
    """Receive a Lemon Squeezy webhook with HMAC verification + idempotency."""
    secret = os.environ.get("LEMON_SQUEEZY_WEBHOOK_SECRET", "")
    body = await request.body()
    signature = request.headers.get("X-Signature") or request.headers.get(
        "x-signature"
    )

    if not verify_signature(body, signature, secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    result = handle_lemonsqueezy_event(db, payload)
    db.commit()
    return result
