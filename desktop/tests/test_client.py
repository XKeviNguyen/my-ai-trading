"""ApiClient tests against a local stdlib HTTP server (no Qt, no engine)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from my_ai_trading_desktop import client as C

TOKEN = "s3cret-token-" + "a" * 32


class Recorder(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        status, headers, payload = self.routes.get(self.path.split("?")[0], (200, {}, {"ok": True}))
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = _handle


@pytest.fixture()
def server():
    Recorder.routes, Recorder.seen = {}, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def other_server():
    """A second server standing in for 'somewhere else' that must never receive the token."""
    seen = []

    class Other(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Other)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, seen
    srv.shutdown()
    srv.server_close()


def make(server, token=TOKEN):
    return C.ApiClient(lambda: token, port=server.server_address[1], timeout_s=5)


def test_base_url_is_fixed_loopback_and_default_port():
    c = C.ApiClient(lambda: TOKEN)
    assert c.base_url == "http://127.0.0.1:8765"
    assert c.build_url("/api/v1/health") == "http://127.0.0.1:8765/api/v1/health"


@pytest.mark.parametrize("path", ["//evil.example/api/v1/x", "/api/v1/../x", "http://evil/api/v1", "/api/v1/a?b=c",
                                  "/api/v1/@evil", "/other"])
def test_build_url_rejects_paths_that_leave_the_api(path):
    with pytest.raises(C.ValidationError):
        C.ApiClient(lambda: TOKEN).build_url(path)


@pytest.mark.parametrize("port", [0, 70000, "8765", True, -1])
def test_port_must_be_a_valid_integer(port):
    with pytest.raises(ValueError):
        C.ApiClient(lambda: TOKEN, port=port)


def test_token_sent_as_bearer_only_to_base_url(server):
    make(server).get_settings()
    req = Recorder.seen[-1]
    assert req["path"] == "/api/v1/settings"
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["headers"]["Host"] == f"127.0.0.1:{server.server_address[1]}"


def test_health_is_sent_without_token(server):
    make(server).health()
    assert "Authorization" not in Recorder.seen[-1]["headers"]


def test_missing_or_malformed_token_never_hits_the_network(server):
    with pytest.raises(C.UnauthorizedError):
        make(server, token=None).get_settings()
    with pytest.raises(C.UnauthorizedError):
        make(server, token="short").get_settings()
    with pytest.raises(C.UnauthorizedError):
        make(server, token="a" * 40 + "\r\nX-Evil: 1").get_settings()
    assert Recorder.seen == []


@pytest.mark.parametrize("status,cls", [
    (400, C.ValidationError), (401, C.UnauthorizedError), (403, C.ForbiddenError), (404, C.NotFoundError),
    (409, C.ConflictError), (421, C.EngineUnavailableError), (422, C.ValidationError), (429, C.RateLimitedError),
    (500, C.ApiError), (502, C.UpstreamError), (503, C.UpstreamUnavailableError),
])
def test_status_mapping(server, status, cls):
    Recorder.routes["/api/v1/settings"] = (status, {"Retry-After": "2"}, {"detail": "boom"})
    with pytest.raises(cls) as ei:
        make(server).get_settings()
    assert type(ei.value) is cls
    assert ei.value.status == status
    if status in (429, 503):
        assert ei.value.retry_after_s == 2
        assert "Retry after 2 s" in str(ei.value)
    else:
        assert ei.value.retry_after_s is None


def test_validation_list_detail_is_readable(server):
    Recorder.routes["/api/v1/analysis"] = (422, {}, {"detail": [
        {"loc": ["query", "symbol"], "msg": "String should match pattern"}]})
    with pytest.raises(C.ValidationError) as ei:
        make(server).analysis("SANDUSDT", "spot")
    assert "query.symbol: String should match pattern" in str(ei.value)


def test_token_echoed_by_server_is_redacted_from_messages(server):
    Recorder.routes["/api/v1/settings"] = (502, {}, {"detail": f"upstream said Bearer {TOKEN} oops"})
    with pytest.raises(C.UpstreamError) as ei:
        make(server).get_settings()
    assert TOKEN not in str(ei.value)
    assert TOKEN not in ei.value.detail
    assert TOKEN not in repr(ei.value)
    assert "[redacted]" in ei.value.detail


def test_messages_for_every_error_kind_never_contain_token(server):
    for status in (400, 401, 404, 409, 421, 422, 429, 500, 502, 503):
        Recorder.routes["/api/v1/usage"] = (status, {}, {"detail": TOKEN * 2})
        with pytest.raises(C.ApiError) as ei:
            make(server).usage()
        assert TOKEN not in str(ei.value) and TOKEN not in ei.value.detail


def test_redirect_is_refused_and_token_not_forwarded(server, other_server):
    other, other_seen = other_server
    target = f"http://127.0.0.1:{other.server_address[1]}/steal"
    for code in (301, 302, 303, 307, 308):
        Recorder.routes["/api/v1/settings"] = (code, {"Location": target}, {})
        with pytest.raises(C.RedirectRefusedError) as ei:
            make(server).get_settings()
        assert TOKEN not in str(ei.value)
    assert other_seen == []


def test_proxy_environment_is_ignored(server, monkeypatch, other_server):
    other, other_seen = other_server
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{other.server_address[1]}")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{other.server_address[1]}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    make(server).get_settings()
    assert other_seen == []
    assert Recorder.seen[-1]["path"] == "/api/v1/settings"


def test_connection_refused_maps_to_engine_unavailable():
    port = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    p = port.server_address[1]
    port.server_close()
    with pytest.raises(C.EngineUnavailableError) as ei:
        C.ApiClient(lambda: TOKEN, port=p, timeout_s=2).get_settings()
    assert "my-ai-trading serve" in str(ei.value)
    assert TOKEN not in str(ei.value)


def test_timeout_maps_to_timeout_error():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)  # accepts the TCP connection but never answers
    try:
        with pytest.raises(C.ApiTimeoutError):
            C.ApiClient(lambda: TOKEN, port=s.getsockname()[1], timeout_s=0.5).get_settings()
    finally:
        s.close()


def test_non_json_success_is_an_error(server):
    Recorder.routes["/api/v1/usage"] = (200, {}, b"<html>not json</html>")
    with pytest.raises(C.ApiError) as ei:
        make(server).usage()
    assert "not a JSON object" in str(ei.value)


def test_client_side_validation_blocks_bad_input(server):
    c = make(server)
    with pytest.raises(C.ValidationError):
        c.analysis("sand usdt", "usdm_perpetual")
    with pytest.raises(C.ValidationError):
        c.analysis("SANDUSDT", "margin")
    with pytest.raises(C.ValidationError):
        c.approve("abc", 1, "not-a-hash")
    with pytest.raises(C.ValidationError):
        c.approve("../x", 1, "0123456789abcdef")
    with pytest.raises(C.ValidationError):
        c.deactivate("a/b")
    with pytest.raises(C.ValidationError):
        c.report_markdown("../../etc")
    with pytest.raises(C.ValidationError):
        c.vision(b"x", "a.gif", "image/gif")
    assert Recorder.seen == []


def test_query_and_json_bodies(server):
    c = make(server)
    c.analysis("SANDUSDT", "spot", "1M")
    assert Recorder.seen[-1]["path"] == "/api/v1/analysis?symbol=SANDUSDT&market=spot&analysis_timeframe=1M"
    c.alerts(7)
    assert Recorder.seen[-1]["path"] == "/api/v1/alerts?after_seq=7&limit=100"
    c.approve("abc123", 2, "0123456789abcdef")
    req = Recorder.seen[-1]
    assert req["path"] == "/api/v1/strategies/abc123/versions/2/approve"
    assert json.loads(req["body"]) == {"rule_hash": "0123456789abcdef"}
    c.save_report("SANDUSDT", "usdm_perpetual", "4h")
    assert json.loads(Recorder.seen[-1]["body"]) == {"symbol": "SANDUSDT", "market": "usdm_perpetual",
                                                     "analysis_timeframe": "4h"}


def test_markdown_report_is_returned_as_text(server):
    Recorder.routes["/api/v1/reports/0123456789ab"] = (200, {"Content-Type": "text/markdown"}, b"# Title\n\nbody")
    assert make(server).report_markdown("0123456789ab") == "# Title\n\nbody"
    assert Recorder.seen[-1]["path"] == "/api/v1/reports/0123456789ab?format=markdown"


def test_multipart_upload_shape(server):
    make(server).vision(b"\x89PNG....", "chart<1>.png", "image/png", "what?", symbol="SANDUSDT", market="spot")
    req = Recorder.seen[-1]
    assert req["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="symbol"\r\n\r\nSANDUSDT' in req["body"]
    assert b'filename="chart_1_.png"' in req["body"]
    assert b"\x89PNG...." in req["body"]


def test_token_file_respects_xdg_and_validates(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = C.token_path()
    assert p == tmp_path / "my-ai-trading" / "api_token"
    assert C.read_token_file() is None
    p.parent.mkdir(parents=True)
    p.write_text("short")
    assert C.read_token_file() is None
    p.write_text(TOKEN + "\n")
    assert C.read_token_file() == TOKEN


def test_parse_retry_after():
    assert C.parse_retry_after("1.2") == 2
    assert C.parse_retry_after("0") == 0
    assert C.parse_retry_after("-1") is None
    assert C.parse_retry_after("soon") is None
    assert C.parse_retry_after(None) is None
