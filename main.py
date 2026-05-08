"""Microcorder core backend — auth + DB + gateway to the Modal AI engine.

Run locally:
    uvicorn main:app --reload

Endpoints:
    POST /register   -> create user (5 credits) -> {username, credits}
    POST /login      -> form-encoded username/password -> {access_token, token_type}
    GET  /me         -> JWT-protected -> {username, credits}
    POST /record     -> JWT-protected gateway: forwards audio to the Modal
                        AI engine, deducts 1 credit on success, returns
                        {text, credits}.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import httpx
from fastapi import (
    Depends, FastAPI, File, HTTPException, UploadFile, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from auth import (
    create_access_token, get_current_user, hash_password, verify_password,
)
from database import Base, engine, get_db
from models import User


# ---- config ----------------------------------------------------------------
MODAL_TRANSCRIBE_URL = os.getenv(
    "MODAL_TRANSCRIBE_URL",
    "https://umutsazc9--microcorder-ai-engine-fastapi-engine.modal.run/transcribe",
)
DEFAULT_CREDITS = 5
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


# ---- app -------------------------------------------------------------------
app = FastAPI(title="Microcorder Core Backend", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://umutsazci.github.io",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)


# ---- schemas ---------------------------------------------------------------
class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeOut(BaseModel):
    username: str
    credits: int


class RecordOut(BaseModel):
    text: str
    credits: int


# ---- endpoints -------------------------------------------------------------
@app.get("/health")
def health():
    return {"service": "microcorder-core", "ok": True}


@app.post("/register", response_model=MeOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    existing = db.query(User).filter_by(username=body.username).one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")
    user = User(
        username=body.username,
        hashed_password=hash_password(body.password),
        credits=DEFAULT_CREDITS,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return MeOut(username=user.username, credits=user.credits)


@app.post("/login", response_model=TokenOut)
def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter_by(username=form.username).one_or_none()
    if user is None or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenOut(access_token=create_access_token(user.username))


@app.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)):
    return MeOut(username=user.username, credits=user.credits)


@app.post("/record", response_model=RecordOut)
async def record(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Gateway: pre-check credits -> forward to Modal -> atomic deduct -> return."""
    # Pre-check (cheap reject before reading the body).
    if user.credits <= 0:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED, "No credits remaining",
        )

    blob = await file.read()
    if not blob:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty audio file")
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Audio too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )

    # Forward to the Modal AI engine.
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                MODAL_TRANSCRIBE_URL,
                files={
                    "file": (
                        file.filename or "audio.webm",
                        blob,
                        file.content_type or "audio/webm",
                    ),
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"AI engine unreachable: {exc}",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"AI engine error {resp.status_code}: {resp.text[:200]}",
        )
    text = (resp.json() or {}).get("text", "")

    # Atomic credit deduction — UPDATE ... WHERE credits >= 1 is race-safe.
    result = db.execute(
        update(User)
        .where(User.id == user.id, User.credits >= 1)
        .values(credits=User.credits - 1)
    )
    if result.rowcount == 0:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED, "Credits depleted",
        )
    db.commit()
    db.refresh(user)

    return RecordOut(text=text, credits=user.credits)
