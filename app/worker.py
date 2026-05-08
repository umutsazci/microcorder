import os
from celery import Celery

EAGER = os.environ.get("CELERY_EAGER", "False").lower() in {"1", "true", "yes"}
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

if EAGER:
    BROKER_URL = "memory://"
    RESULT_BACKEND = "cache+memory://"
else:
    BROKER_URL = REDIS_URL
    RESULT_BACKEND = REDIS_URL

celery_app = Celery("ai_saas", broker=BROKER_URL, backend=RESULT_BACKEND)
celery_app.conf.update(
    task_always_eager=EAGER,
    task_eager_propagates=EAGER,
    task_serializer="json",
)


@celery_app.task(name="process_audio_task")
def process_audio_task(task_id: int, audio_b64: str) -> dict:
    """Run the AI pipeline for a Task row and persist results."""
    import base64
    from app.database import session_scope
    from app.ai_pipeline import run_pipeline
    from app.models import Task, TaskStatus
    from datetime import datetime, timezone

    audio = base64.b64decode(audio_b64.encode("ascii"))

    with session_scope() as s:
        task = s.get(Task, task_id)
        if task is None:
            return {"ok": False, "error": "task not found"}
        task.status = TaskStatus.PROCESSING

    try:
        result = run_pipeline(audio)
    except Exception as exc:  # pragma: no cover - exercised via mocks
        with session_scope() as s:
            task = s.get(Task, task_id)
            task.status = TaskStatus.FAILED
            task.error = str(exc)
        return {"ok": False, "error": str(exc)}

    with session_scope() as s:
        task = s.get(Task, task_id)
        task.transcript = result["transcript"]
        task.summary = result["summary"]
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.now(timezone.utc)

    return {"ok": True, "task_id": task_id}
