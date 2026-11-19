# Card authorization service

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

## Tests

```bash
uv run pytest                 # integration tests need `docker compose up -d`
uv run ruff check . && uv run pyright
```

Integration tests use a separate `authz_test` database. They rebuild its schema from
the Alembic migrations on every run and empty the tables after each test.
