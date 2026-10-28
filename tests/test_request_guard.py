"""Six-breaker remediation: SEC-1 (byte-counted body cap), SEC-3 (token and DB files), SEC-5, ENV-3, ENV-6."""
import asyncio
import os
import socket
import stat
import threading
import time

import pytest

from fakes import make_exchange
from my_ai_trading.api import MAX_JSON_BYTES, RequestGuard, create_app, load_or_create_token, token_path
from my_ai_trading.copilot import Copilot
from my_ai_trading.store import Store, default_db_path

TOKEN = "t" * 43
HOST = "127.0.0.1:8765"


def _call(_guard, headers, chunks, method="POST", path="/api/v1/settings"):
    """Drive the ASGI guard directly: `chunks` are the body messages the client sends."""
    sent, reached = [], []
    queue = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]

    async def app(scope, receive, send):
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        reached.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": method, "path": path,
             "headers": [(b"host", HOST.encode()), (b"authorization", f"Bearer {TOKEN}".encode()), *headers]}
    asyncio.run(RequestGuard(app, allowed_hosts={HOST}, token=TOKEN)(scope, receive, send))
    return sent[0]["status"], reached


def test_a_body_longer_than_its_content_length_is_refused_before_the_app_sees_it():
    status, reached = _call(RequestGuard, [(b"content-length", b"2")], [b"{}", b"x" * 1024 * 1024, b"y" * 1024 * 1024])
    assert status == 413 and reached == []


def test_a_body_within_its_content_length_reaches_the_app_unchanged():
    status, reached = _call(RequestGuard, [(b"content-length", b"7")], [b'{"a"', b":1}"])
    assert status == 200 and reached == [b'{"a":1}']


def test_chunked_bodies_are_refused():
    status, reached = _call(RequestGuard, [(b"content-length", b"2"), (b"transfer-encoding", b"chunked")], [b"{}"])
    assert status == 411 and reached == []


def test_a_token_without_the_bearer_prefix_is_refused():
    async def app(scope, receive, send):
        raise AssertionError("must not be reached")
    sent = []

    async def send(msg):
        sent.append(msg)
    scope = {"type": "http", "method": "GET", "path": "/api/v1/settings",
             "headers": [(b"host", HOST.encode()), (b"authorization", TOKEN.encode())]}
    asyncio.run(RequestGuard(app, allowed_hosts={HOST}, token=TOKEN)(scope, None, send))
    assert sent[0]["status"] == 401


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_uvicorn_refuses_content_length_plus_chunked_streaming():
    uvicorn = pytest.importorskip("uvicorn")
    port = _free_port()
    assert port != 8765
    _, svc = make_exchange()
    store = Store()
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN, port=port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall((f"PUT /api/v1/settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {TOKEN}\r\n"
                       "Content-Type: application/json\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n").encode())
            try:
                chunk = b"x" * 65536
                for _ in range(48):  # 3 MiB, as in the audit reproduction
                    s.sendall(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                s.sendall(b"0\r\n\r\n")
            except OSError:
                pass  # the server may close the connection early: that is a refusal too
            reply = s.recv(4096)
        status = int(reply.split(b" ", 2)[1]) if reply else 0
        assert status in (0, 400, 411, 413), reply[:200]  # never 200
    finally:
        server.should_exit = True
        t.join(timeout=10)
        store.close()


def test_a_lone_surrogate_is_a_validation_error_not_a_server_error():
    from fastapi.testclient import TestClient
    _, svc = make_exchange()
    store = Store()
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
        bad = c.post("/api/v1/strategies/compile", content=b'{"prompts": ["close above sma 20 \\ud800"]}', headers=h)
        assert bad.status_code == 422
        emoji = c.post("/api/v1/strategies/compile", content=b'{"prompts": ["close above sma 20 \\ud83d\\ude00"]}', headers=h)
        assert emoji.status_code == 200  # a valid surrogate pair is ordinary text
    store.close()


def test_the_token_file_is_created_private_and_tightened_if_others_can_read_it(tmp_path):
    p = tmp_path / "cfg" / "api_token"
    tok = load_or_create_token(p)
    assert len(tok) >= 32 and stat.S_IMODE(p.stat().st_mode) == 0o600
    p.chmod(0o644)
    assert load_or_create_token(p) == tok and stat.S_IMODE(p.stat().st_mode) == 0o600


def test_a_symlinked_token_or_database_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("s" * 43)
    link = tmp_path / "api_token"
    link.symlink_to(target)
    with pytest.raises(OSError):
        load_or_create_token(link)
    db = tmp_path / "data" / "app.db"
    db.parent.mkdir()
    db.symlink_to(tmp_path / "missing.db")  # dangling: SQLite would create the target
    with pytest.raises(PermissionError):
        Store(db)
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("value", ["", "relative/dir"])
def test_empty_or_relative_xdg_values_fall_back_to_the_home_defaults(monkeypatch, value):
    monkeypatch.setenv("XDG_CONFIG_HOME", value)
    monkeypatch.setenv("XDG_DATA_HOME", value)
    assert token_path().is_absolute() and str(token_path()).startswith(os.path.expanduser("~/.config"))
    assert str(default_db_path()).startswith(os.path.expanduser("~/.local/share"))


def test_json_cap_still_applies_to_the_declared_length():
    status, _ = _call(RequestGuard, [(b"content-length", str(MAX_JSON_BYTES + 1).encode())], [b"{}"])
    assert status == 413


@pytest.mark.parametrize("ctype, body", [
    (b"Application/JSON", b'{"prompts": ["x \\ud800"]}'),
    (b"application/merge-patch+json", b'{"prompts": ["x \\ud800"]}'),
    (b"application/json; charset=utf-8", b'{"prompts": ["x \\ud800"]}'),
    (b"application/json", b'{"prompts": ["x \xed\xa0\x80"]}'),  # raw surrogate bytes are not UTF-8
])
def test_every_json_spelling_and_raw_surrogate_bytes_are_refused(ctype, body):
    status, reached = _call(RequestGuard, [(b"content-length", str(len(body)).encode()), (b"content-type", ctype)],
                            [body], path="/api/v1/strategies/compile")
    assert status == 422 and reached == []  # re-audit: these reached the route and gave HTTP 500


def test_a_large_upload_is_spooled_and_reaches_the_app_unchanged():
    from my_ai_trading.api import MAX_UPLOAD_BYTES, SPOOL_BYTES, UPLOAD_PATHS
    data = bytes(range(256)) * ((SPOOL_BYTES * 3) // 256)  # spills to a temporary file
    assert SPOOL_BYTES < len(data) <= MAX_UPLOAD_BYTES
    chunks = [data[i:i + 100_000] for i in range(0, len(data), 100_000)]
    status, reached = _call(RequestGuard, [(b"content-length", str(len(data)).encode()),
                                           (b"content-type", b"multipart/form-data; boundary=x")],
                            chunks, path=next(iter(UPLOAD_PATHS)))
    assert status == 200 and reached == [data]


def test_duplicate_content_type_headers_are_refused():
    # Verifier D2: the guard read the last Content-Type, the app the first, so the Unicode check was bypassed.
    body = b'{"prompts": ["x \\ud800"]}'
    status, reached = _call(RequestGuard, [(b"content-length", str(len(body)).encode()),
                                           (b"content-type", b"text/plain"), (b"content-type", b"application/json")],
                            [body], path="/api/v1/strategies/compile")
    assert status == 400 and reached == []
