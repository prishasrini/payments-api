import os
import socket
import threading
import time

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/payments_test")
os.environ["PROCESSING_DELAY_MS"] = "40"  # widen race windows so the concurrency tests are meaningful
os.environ["DEMO_MODE"] = "true"

import uvicorn  # noqa: E402

from app.db import seed_demo  # noqa: E402
from app.main import app  # noqa: E402

KEY = {"X-API-Key": "sk_test_demo"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def api(base_url):
    seed_demo(reset=True)
    with httpx.Client(base_url=base_url, headers=KEY, timeout=30) as client:
        yield client


@pytest.fixture()
def base(base_url):
    seed_demo(reset=True)
    return base_url
