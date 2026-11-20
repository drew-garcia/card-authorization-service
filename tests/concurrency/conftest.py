"""Fixtures for concurrency tests, which hit a real multi-process server over HTTP."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import Engine

WORKERS = 4
STARTUP_TIMEOUT_SECONDS = 20


@pytest.fixture(autouse=True)
def _clean_tables_before_each_test(clean_tables: None) -> None:
    """autouse=True applies this to every test in this folder, so each one
    starts with empty tables."""


def _free_port() -> int:
    """Ask the OS for an unused port by binding to port 0, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(engine: Engine, test_database_url: str) -> Iterator[str]:
    """Run the app under `uvicorn --workers 4` and yield its base URL.

    Each worker is a separate process with its own connection pool, like a real
    deployment, so requests really do run in parallel against Postgres.
    Depending on `engine` makes sure authz_test is migrated before it starts.
    DATABASE_URL in the environment takes priority over .env, so the workers
    use the test database.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    # sys.executable is the venv's Python, so this is the same uvicorn as
    # `uv run uvicorn`. Output goes to a pipe so it can be shown on failure.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "authz.main:app",
            "--workers",
            str(WORKERS),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env={**os.environ, "DATABASE_URL": test_database_url},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_until_ready(proc, base_url)
        yield base_url
    finally:
        # SIGTERM: uvicorn shuts its workers down gracefully.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _wait_until_ready(proc: subprocess.Popen[str], base_url: str) -> None:
    """Poll until the server answers, failing fast if the process dies."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"uvicorn exited with code {proc.returncode}:\n{output}")
        try:
            if httpx.get(f"{base_url}/openapi.json", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass  # not listening yet
        time.sleep(0.1)
    proc.kill()
    output = proc.stdout.read() if proc.stdout else ""
    pytest.fail(f"uvicorn not ready after {STARTUP_TIMEOUT_SECONDS}s:\n{output}")
