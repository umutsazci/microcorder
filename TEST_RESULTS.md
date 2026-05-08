# Test Results — AI SaaS Architecture

**Status:** ✅ 36 / 36 tests passing
**Total code coverage:** **84%** (323 statements, 53 missed)
**Runner:** `pytest` against in-memory SQLite, FastAPI `TestClient`, mocked Celery & Stripe.
**Environment:** Local `.venv` (Python 3.13.7, sqlalchemy 2.0, fastapi, pytest 9, pytest-cov 7).

## Per-phase summary

| Phase | Module(s)                      | Tests | Status |
| ----: | :----------------------------- | ----: | :----: |
|  1    | Database architecture          |    10 |  ✅    |
|  2    | AI pipeline (Wav2Vec2 + MOCK)  |     9 |  ✅    |
|  3    | FastAPI endpoints + Celery     |     6 |  ✅    |
|  4    | Frontend static MVP            |     3 |  ✅    |
|  5    | Stripe billing & credits       |     8 |  ✅    |
| **Total** |                            | **36** | **✅** |

## Coverage by module

| Module               | Stmts | Miss | Cover |
| :------------------- | ----: | ---: | ----: |
| `app/__init__.py`    |     0 |    0 |  100% |
| `app/crud.py`        |    27 |    0 |  100% |
| `app/database.py`    |    33 |    0 |  100% |
| `app/models.py`      |    54 |    0 |  100% |
| `app/main.py`        |    58 |    3 |   95% |
| `app/billing.py`     |    58 |    4 |   93% |
| `app/ai_pipeline.py` |    66 |   26 |   61% |
| `app/worker.py`      |    27 |   20 |   26% |
| **Total**            |   323 |   53 |  **84%** |

### Why some lines are intentionally uncovered

- **`app/ai_pipeline.py`** — real Wav2Vec2 inference branch (`_load_real_model`, `_real_transcribe`) is bypassed by the `MOCK_AI=True` toggle, exactly as required by Phase 2. Running it would require multi-GB model downloads.
- **`app/worker.py`** — Celery task body runs inside a worker process. Tests mock `process_audio_task.delay()` at the queueing boundary (Phase 3 requirement: "verify task queuing without requiring a live Redis server").
- **`app/main.py`** — startup hook lines covered only when the app boots inside `TestClient`; remaining few lines are guard branches.

## Phase 1 — Database (10 tests, 100% coverage on models/crud/database)

- `test_create_user_creates_credit_balance`
- `test_user_email_unique` — IntegrityError on duplicate email
- `test_subscription_relationship` — bidirectional User↔Subscription
- `test_foreign_key_violation_on_task` — FK enforcement (PRAGMA on)
- `test_cascade_delete_user_removes_children` — cascades to subs/credits/tasks
- `test_add_credits_updates_balance`
- `test_session_rollback_on_error` — `session_scope` rollback path
- `test_session_scope_commits_on_success`
- `test_unique_subscription_per_user`
- `test_task_crud_round_trip`

## Phase 2 — AI Pipeline (9 tests)

- Schema lock-in: result has exactly `{transcript, summary, duration_ms, model, mocked}`
- `MOCK_AI` env flag honored
- Determinism for identical input
- Empty-bytes / non-bytes input rejection
- `summarize()` first-sentence, short-text, and long-text truncation paths

## Phase 3 — API & Queues (6 tests)

- `GET /` healthcheck
- `POST /api/record` queues task — `process_audio_task.delay` mocked, args verified
- 400 on empty blob, no queue call
- `GET /api/status/{id}` returns task state
- 404 on unknown task id
- 402 `insufficient credits` when balance hits zero

## Phase 4 — Frontend (3 tests)

- `/static/index.html` served (200, `text/html`, contains `MediaRecorder` + endpoint URLs)
- `/static/` directory index serves `index.html`
- 404 for missing static asset

## Phase 5 — Billing & Credits (8 tests)

- `deduct_credit` happy path
- `InsufficientCredits` raised on zero balance
- `InsufficientCredits` raised when amount > balance, **balance unchanged**
- `checkout.session.completed` webhook grants credits at `CREDITS_PER_USD` rate
- `customer.subscription.updated` creates/updates Subscription row
- Unknown event types ignored cleanly
- Webhook with missing email ignored
- Full mocked flow: purchase → consume to zero → next deduct raises

## How to reproduce

```bash
cd ai-saas-architecture
python -m venv .venv
. .venv/Scripts/activate          # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
MOCK_AI=True python -m pytest --cov=app --cov-report=term-missing
```

## Git history

Each phase committed and pushed to `origin/main` only after its tests passed:

- `feat/test: completed Phase 1 - Database Architecture`
- `feat/test: completed Phase 2 - AI Pipeline (Wav2Vec2 + MOCK_AI)`
- `feat/test: completed Phase 3 - Backend API & Queues`
- `feat/test: completed Phase 4 - Frontend MVP`
- `feat/test: completed Phase 5 - Billing & Credits (Stripe)`
