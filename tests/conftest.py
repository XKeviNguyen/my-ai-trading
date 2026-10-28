"""Test-wide network isolation: tests may reach loopback only.

An audit harness once sent a request with a dummy key to a real exchange testnet. This guard makes any
non-loopback connection or DNS lookup fail at once, in every thread, so a missing fake cannot reach the internet.
"""
import ipaddress
import socket

_real_connect, _real_connect_ex, _real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
_real_sendto, _real_gethostbyname, _real_gethostbyname_ex = socket.socket.sendto, socket.gethostbyname, socket.gethostbyname_ex
_LOCAL_NAMES = {"localhost"}


class NetworkBlocked(OSError):
    pass


def _is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode()
    if host is None or host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False  # any other name would need DNS


def _check(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_local(address[0]):
        raise NetworkBlocked(f"tests may not connect to {address[0]!r}; use a fake")


def _connect(self, address):
    _check(self, address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check(self, address)
    return _real_connect_ex(self, address)


def _getaddrinfo(host, *args, **kw):
    if not _is_local(host):
        raise NetworkBlocked(f"tests may not resolve {host!r}; use a fake")
    return _real_getaddrinfo(host, *args, **kw)


def _sendto(self, data, *args):
    _check(self, args[-1])  # UDP: the address is the last argument
    return _real_sendto(self, data, *args)


def _gethostbyname(host):
    if not _is_local(host):
        raise NetworkBlocked(f"tests may not resolve {host!r}; use a fake")
    return _real_gethostbyname(host)


def _gethostbyname_ex(host):
    if not _is_local(host):
        raise NetworkBlocked(f"tests may not resolve {host!r}; use a fake")
    return _real_gethostbyname_ex(host)


def pytest_configure(config):
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = _connect, _connect_ex, _getaddrinfo
    socket.socket.sendto, socket.gethostbyname, socket.gethostbyname_ex = _sendto, _gethostbyname, _gethostbyname_ex
