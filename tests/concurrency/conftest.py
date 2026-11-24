"""Fixtures for concurrency tests, which hit a real multi-process server over HTTP."""

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

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
def live_server(
    engine: Engine, test_database_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """Run the app under `uvicorn --workers 4` and yield its base URL.

    Each worker is a separate process with its own connection pool, like a real
    deployment, so requests really do run in parallel against Postgres.
    Depending on `engine` makes sure authz_test is migrated before it starts.
    DATABASE_URL in the environment takes priority over .env, so the workers
    use the test database.

    Server output goes to a file, not a pipe: uvicorn logs a line per request,
    and once an unread pipe's 64 KB buffer fills, every worker blocks on that
    write with its event loop frozen. start_new_session puts the server and
    its workers in their own process group, so they can all be killed at once.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("live_server") / "uvicorn.log"
    log_file = log_path.open("w")
    # sys.executable is the venv's Python, so this is the same uvicorn as
    # `uv run uvicorn`. Output goes to log_path so it can be shown on failure.
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
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        _wait_until_ready(proc, base_url, log_path)
        yield base_url
    finally:
        # SIGTERM: uvicorn shuts its workers down gracefully.
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Killing only the parent would orphan the workers, still holding
            # their database connections; kill the whole process group.
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        log_file.close()


def _wait_until_ready(
    proc: subprocess.Popen[bytes], base_url: str, log_path: Path
) -> None:
    """Poll until the server answers, failing fast if the process dies."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = log_path.read_text()
            pytest.fail(f"uvicorn exited with code {proc.returncode}:\n{output}")
        try:
            if httpx.get(f"{base_url}/openapi.json", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass  # not listening yet
        time.sleep(0.1)
    proc.kill()
    output = log_path.read_text()
    pytest.fail(f"uvicorn not ready after {STARTUP_TIMEOUT_SECONDS}s:\n{output}")
