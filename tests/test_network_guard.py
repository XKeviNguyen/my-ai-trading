"""Positive control for the network guard in conftest.py: it must actually block."""
import socket
import urllib.request

import pytest

from conftest import NetworkBlocked


def test_non_loopback_connections_and_lookups_are_blocked():
    with socket.socket() as s, pytest.raises(NetworkBlocked):
        s.connect(("1.1.1.1", 443))
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("testnet.binance.vision", 443)
    with pytest.raises(Exception) as exc:
        urllib.request.urlopen("https://testnet.binance.vision/api/v3/time", timeout=1)
    assert "use a fake" in str(exc.value)


def test_loopback_still_works():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.create_connection(server.getsockname(), timeout=2):
            pass


def test_udp_and_legacy_lookups_are_blocked_too():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(NetworkBlocked):
        s.sendto(b"x", ("8.8.8.8", 53))
    for lookup in (socket.gethostbyname, socket.gethostbyname_ex):
        with pytest.raises(NetworkBlocked):
            lookup("testnet.binance.vision")
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("testserver", 80)  # a real DNS name, not the TestClient's in-process host
