# Card authorization service

[![CI](https://github.com/drew-garcia/card-authorization-service/actions/workflows/ci.yml/badge.svg)](https://github.com/drew-garcia/card-authorization-service/actions/workflows/ci.yml)

A FastAPI and Postgres service that approves or declines card swipes in real time.

## Running locally

Needs [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env          # DATABASE_URL for the local database
docker compose up -d          # Postgres 17 on localhost:5433
uv sync
uv run alembic upgrade head   # create the tables
uv run fastapi dev            # API on http://localhost:8000, docs at /docs
```

Data lives in the `pgdata` Docker volume, so it survives `docker compose restart`
and `docker compose down`. `docker compose down -v` deletes it.

## Hold expiry worker

Approved swipes put a pending hold on the card. If the network never captures or
reverses it, the hold expiry worker expires it and gives the money back:

```bash
uv run authz-hold-expiry      # or: uv run python -m authz.workers.hold_expiry
```

Every `HOLD_EXPIRY_INTERVAL_SECONDS` (default 60) it expires pending holds older
than `HOLD_EXPIRY_SECONDS` (default 7 days), `HOLD_EXPIRY_BATCH_SIZE` (default 100)
per transaction. It claims holds with `FOR UPDATE SKIP LOCKED`, so any number of
copies can run at once without releasing a hold twice. Stop it with Ctrl-C or SIGTERM.

## Tests

```bash
uv run pytest                 # integration tests need `docker compose up -d`
uv run ruff check . && uv run pyright
```

Integration tests use a separate `authz_test` database. They rebuild its schema from
the Alembic migrations on every run and empty the tables after each test.
