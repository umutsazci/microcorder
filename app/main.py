import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Request
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


CREDITS_PER_MINUTE = 2  # Premium WhisperX diarization rate


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


def _ensure_demo_user(db: Session) -> User:
    user = crud.get_user_by_email(db, os.environ.get("DEMO_USER_EMAIL", "demo@example.com"))
    if user is None:
        # Bootstrap demo user with a small credit grant so /api/record works
        # before any webhook arrives. Real users start at 0 and top up via
        # Lemon Squeezy.
        user = crud.create_user(db, os.environ.get("DEMO_USER_EMAIL", "demo@example.com"), "demo", credits=5)
        db.commit()
    return user


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
def me(db: Session = Depends(get_db)):
    """Current user's credit balance + checkout URL for top-ups."""
    user = _ensure_demo_user(db)
    db.refresh(user)
    return {
        "email": user.email,
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
    file: UploadFile = File(...),
    email: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Premium WhisperX pipeline billed per audio-minute.

    Strict order:
      1. Save the upload to a temp file.
      2. Measure duration -> minutes (ceil, min 1).
      3. Pre-check user credits >= minutes * CREDITS_PER_MINUTE -> else 402.
      4. Run WhisperX (transcribe + Wav2Vec2 align + pyannote diarize).
      5. Atomically deduct minutes * CREDITS_PER_MINUTE.
      6. Delete temp file.
      7. Return transcript + speaker segments + balance.
    """
    blob = await file.read()
    if not blob:
        raise HTTPException(status_code=400, detail="empty audio blob")

    # Resolve user (provided email or demo fallback for the unauth'd UI)
    if email:
        user = crud.get_user_by_email(db, email)
        if user is None:
            raise HTTPException(status_code=404, detail=f"unknown user {email}")
    else:
        user = _ensure_demo_user(db)

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
def status(task_id: int, db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if task is None:
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
