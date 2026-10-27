"""Shared fixtures. Qt runs offscreen; the engine fixture serves the REAL core API on a free local port
with the synthetic fake exchange from the core test suite (SYNTHETIC data only)."""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TOKEN = "desktop-test-token-" + "x" * 30


def repo_root() -> Path:
    """Repository root holding tests/fakes.py; MY_AI_TRADING_REPO_ROOT overrides it (e.g. for a source snapshot)."""
    env = os.environ.get("MY_AI_TRADING_REPO_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def wait_until(cond: Callable[[], bool], timeout_s: float = 15.0) -> bool:
    """Spin the Qt event loop until `cond()` is true or the timeout expires (bounded wait)."""
    from PySide6.QtCore import QCoreApplication, QEventLoop
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        if cond():
            return True
        time.sleep(0.01)
    QCoreApplication.processEvents()
    return cond()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def engine():
    """Start the real FastAPI app with uvicorn in a background thread. Yields (port, token, store)."""
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    fakes_dir = repo_root() / "tests"
    if not (fakes_dir / "fakes.py").exists():
        pytest.skip(f"core test fakes not found under {fakes_dir}; set MY_AI_TRADING_REPO_ROOT")
    sys.path.insert(0, str(fakes_dir))
    try:
        from fakes import make_exchange  # type: ignore[import-not-found]
    finally:
        sys.path.remove(str(fakes_dir))
    from my_ai_trading.api import create_app
    from my_ai_trading.copilot import Copilot
    from my_ai_trading.store import Store

    _, svc = make_exchange()
    store = Store()
    port = free_port()
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN, port=port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "engine did not start"
    yield port, TOKEN, store
    server.should_exit = True
    thread.join(timeout=10)
