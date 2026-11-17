# Card authorization service

A portfolio project: a FastAPI service that approves or declines card swipes in real time and stays correct under concurrency, retries and failures. The owner is learning Python and backend engineering by building it, so briefly explain any non-obvious Python or design choice you make.

## Stack

- Python, managed with **uv** only (never pip, poetry or requirements.txt)
- FastAPI and Pydantic v2
- SQLModel on Postgres 17 (SQLAlchemy underneath), migrations with Alembic
- pytest, ruff, pyright
- Coming later: Redis (usage limits), OpenTelemetry, Prometheus and Grafana, k6, Terraform

## Commands

- Sync dependencies: `uv sync`
- Add a dependency: `uv add <pkg>` (dev only: `uv add --dev <pkg>`)
- Run the API: `uv run fastapi dev src/authz/main.py`
- Tests: `uv run pytest`
- Lint and format: `uv run ruff check .` and `uv run ruff format .`
- Type check: `uv run pyright`
- Database (once set up): `docker compose up -d`, then `uv run alembic upgrade head`
- New migration: `uv run alembic revision --autogenerate -m "<what changed>"`

## Layout

Some of these don't exist yet. Create a folder only when a task needs it.

- `src/authz/schemas.py`: request and response models
- `src/authz/models.py`: database tables
- `src/authz/api/`: routers. Thin: validate input, call a service, return the result. No SQL here.
- `src/authz/services/`: all business logic; each operation runs in one transaction
- `src/authz/workers/`: background workers (delivery, hold expiry)
- `tests/`: `unit/`, `integration/`, `concurrency/`
- `design-notes/`: the owner's notes, including `agent-log.md`

## Hard rules

1. Money is always an integer number of cents (`int` in Python, `BIGINT` in Postgres). Never use float for amounts. Amount fields end in `_cents`.
2. Every change to a card's balance, a hold or the ledger happens inside one database transaction, together with its outbox event (once the outbox exists).
3. Never read a balance, check it in Python, then write it back. Balance changes use a row lock or a conditional `UPDATE ... WHERE available_cents >= :amount`.
   - **Exception:** the current read-check-write in the authorization path is deliberate, so the race test fails on purpose. Don't fix it unless asked.
4. Network IDs (`network_request_id`, `network_capture_id`, `network_reversal_id`, `advice_id`) are idempotency keys. A repeat with the same body returns the original response; a repeat with a different body returns 409.
5. Every ledger transaction's entries sum to zero.
6. Never edit a migration that has already been applied. Add a new one.
7. No secrets in code. Config comes from environment variables through the settings module; `.env` is gitignored.

## How we work together

- **The owner writes the correctness core.** Don't write or rewrite code that locks rows, changes balances, handles idempotency, or posts to the ledger unless explicitly asked. You may review it, point out bugs, and propose tests that could break it.
- **Everything else goes through tests.** For scaffolding, migrations, Docker Compose, the simulator, dashboards and Terraform: outline your plan before large changes, keep diffs small, and don't call a task done until tests, ruff and pyright pass (and the invariant checker, once it exists).
- Never weaken, skip or delete a failing test to make it pass. Don't remove `xfail` markers unless asked.
- If something is ambiguous, ask instead of guessing.
- End each task with a 2-3 line summary: what you changed, and anything you weren't sure about. The owner copies it into `design-notes/agent-log.md`.

## Code style

- Type hints on every function; pyright must pass.
- Services take a database session as an argument; routes never contain SQL.
- Tests that touch the database run against real Postgres. Don't mock the database.
- Every new behavior gets a test, including its failure case. Anything concurrency-sensitive gets a concurrency test.