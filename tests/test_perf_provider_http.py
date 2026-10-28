"""ENV-2: garbage HTTP (captive portal, proxy junk, oversized headers) is a provider error (API 502),
never an unhandled exception (API 500). Loopback socket only; no external network."""
import socket
import threading
from http.client import IncompleteRead

import pytest

from my_ai_trading.providers import binance as B


def serve_once(payload: bytes) -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))  # ephemeral loopback port
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(payload)
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return port


@pytest.mark.parametrize("payload", [
    b"<html>Sign in to the hotel Wi-Fi</html>\r\n\r\n",                          # BadStatusLine
    b"HTTP/1.1 200 OK\r\nX-Junk: " + b"a" * 70_000 + b"\r\n\r\n",               # LineTooLong header
    b"HTTP/1.1 200 OK\r\n" + b"".join(b"X-%d: 1\r\n" % i for i in range(150)) + b"\r\n",  # too many headers
], ids=["bad-status-line", "line-too-long", "too-many-headers"])
def test_malformed_http_maps_to_market_data_error(payload):
    port = serve_once(payload)
    with pytest.raises(B.MarketDataError):
        B.urllib_fetch(f"http://127.0.0.1:{port}/fapi/v1/time", 5.0, 1_000_000)


def test_http_exception_raised_while_reading_is_mapped(monkeypatch):
    class Resp:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            raise IncompleteRead(b"partial", 10)

        read1 = read

    class Opener:
        def open(self, req, timeout):
            return Resp()
    monkeypatch.setattr(B, "_OPENER", Opener())
    with pytest.raises(B.MarketDataError, match="Malformed HTTP response"):
        B.urllib_fetch("https://fapi.binance.com/fapi/v1/time", 5.0, 1_000_000)


def test_a_server_that_drips_bytes_cannot_hold_a_fetch_past_its_deadline():
    import socket
    import threading
    import time
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    stop = threading.Event()

    def drip():  # re-audit ENV-13: one byte every 0.2 s kept a 1 s fetch alive for as long as it dripped
        conn, _ = server.accept()
        conn.recv(4096)
        for b in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 200:
            if stop.is_set():
                break
            try:
                conn.sendall(bytes([b]))
            except OSError:
                break
            time.sleep(0.2)
        conn.close()
    t = threading.Thread(target=drip, daemon=True)
    t.start()
    started = time.monotonic()
    with pytest.raises(B.MarketDataError, match="deadline"):
        B.urllib_fetch(f"http://127.0.0.1:{server.getsockname()[1]}/x", 1.0, 1_000_000)
    assert time.monotonic() - started < 3
    stop.set()
    server.close()


def test_a_connection_opened_after_the_deadline_is_cut_at_once(monkeypatch):
    # Verifier C: a socket that connected after the watchdog fired (slow DNS) was never cut.
    import socket
    import threading
    import time
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def drip():
        conn, _ = server.accept()
        conn.recv(4096)
        try:
            for b in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 200:
                conn.sendall(bytes([b]))
                time.sleep(0.2)
        except OSError:
            pass
        conn.close()
    threading.Thread(target=drip, daemon=True).start()
    real = socket.create_connection

    def slow_dns(*a, **kw):
        time.sleep(1.2)  # longer than the 1 s deadline
        return real(*a, **kw)
    monkeypatch.setattr(socket, "create_connection", slow_dns)
    started = time.monotonic()
    with pytest.raises(B.MarketDataError, match="deadline"):
        B.urllib_fetch(f"http://127.0.0.1:{server.getsockname()[1]}/x", 1.0, 1_000_000)
    assert time.monotonic() - started < 3
    server.close()
