"""Modal Cloud deployment blueprint for Microcorder.

Wraps the existing FastAPI app (app/main.py) and serves it on a Modal-hosted
container with a T4 GPU. Scales to zero between requests; first cold start
downloads the WhisperX + pyannote model weights (~1 GB).

Deploy:
    modal deploy modal_app.py

Required Modal secret (`microcorder-secrets`):
    DATABASE_URL                     - Neon Postgres connection string
    HF_TOKEN                         - Hugging Face read token (pyannote diarization)
    LEMON_SQUEEZY_WEBHOOK_SECRET     - HMAC signing secret
    LEMON_SQUEEZY_CHECKOUT_URL       - Pro tier checkout URL
    LEMON_SQUEEZY_STARTER_URL        - Starter tier checkout URL
    DEMO_USER_EMAIL                  - Default demo identity
    WHISPER_MODEL                    - faster-whisper model id (base / small / ...)
    WHISPER_DEVICE                   - 'cuda' on Modal, 'cpu' locally
    WHISPER_COMPUTE_TYPE             - 'float16' on GPU, 'int8' on CPU
    DIARIZATION_MODEL                - pyannote/speaker-diarization-3.1
"""
import modal


app = modal.App("microcorder-backend")

# Persist Hugging Face model cache across cold starts so we only pay the
# ~1 GB download once. Default HF cache path inside Modal containers is
# /root/.cache/huggingface — pyannote, transformers, faster-whisper all use it.
hf_cache = modal.Volume.from_name("microcorder-hf-cache", create_if_missing=True)


# Build image: ffmpeg via apt for pydub/whisperx; full Python deps for the
# WhisperX pipeline (whisperx pulls in torch, transformers, pyannote.audio).
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi",
        "faster-whisper",
        "whisperx>=3.8",
        "sqlalchemy>=2.0",
        "psycopg2-binary",
        "python-multipart",
        "uvicorn",
        "pydub",
        "python-dotenv",
        "imageio-ffmpeg",
    )
    # Snapshot the local repo into the image so /root contains app/, index.html,
    # scripts/, etc. Excludes secrets, caches, and dev artifacts.
    .add_local_dir(
        ".",
        "/root",
        ignore=[
            "**/.venv/**",
            "**/.git/**",
            "**/__pycache__/**",
            "**/*.db",
            "**/.env",
            "**/.env.*",
            "**/.pytest_cache/**",
            "**/.coverage",
            "**/htmlcov/**",
        ],
    )
)


@app.function(
    image=image,
    gpu="T4",
    secrets=[modal.Secret.from_name("microcorder-secrets")],
    volumes={"/root/.cache/huggingface": hf_cache},
    timeout=600,
)
@modal.asgi_app()
def fastapi_app():
    """Return the existing FastAPI instance — Modal serves it as a web API."""
    from app.main import app as fastapi_instance
    return fastapi_instance
