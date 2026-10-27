"""Binance public kline streams used as a wake-up signal for the monitor (R8 M7).

A closed-kline message ("x": true) makes the monitor evaluate that subscription now instead of waiting for its
REST schedule. The stream never supplies data to analysis: the monitor still fetches validated closed bars over
REST, so a lost, late or duplicated stream message cannot change a result, only when it is computed.

Endpoints (Binance docs, checked 2026-10-27):
- Spot: wss://data-stream.binance.vision (market data only), combined streams /stream?streams=a/b.
- USD-M futures: wss://fstream.binance.com/market (kline streams moved to /market; legacy URLs retired 2026-04-23).
- Payload: {"stream": "<symbol>@kline_<interval>", "data": {"e": "kline", "s": "BTCUSDT", "k": {"t": open_ms,
  "i": "1h", "x": closed}}}. Connections are cut after 24 h and the server pings every 20 s (pong is automatic).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

HOSTS = {"spot": "wss://data-stream.binance.vision/stream", "usdm_perpetual": "wss://fstream.binance.com/market/stream"}
MAX_STREAMS_PER_CONNECTION = 200
ROTATE_AFTER_S = 23 * 3600  # reconnect on our schedule before Binance's 24 h cut-off
MAX_BACKOFF_S = 120.0
HEALTHY_AFTER_S = 60.0  # a connection must stay up this long before its failure count resets
SEEN_LIMIT = 4096

OnClosed = Callable[[str, str, str, int], None]  # (symbol, market, timeframe, bar_open_ms)
Connector = Callable[[str], Awaitable[Any]]


def stream_names(subs: set[tuple[str, str]]) -> list[str]:
    """(symbol, timeframe) pairs -> sorted Binance stream names, e.g. 'btcusdt@kline_1h'."""
    return sorted(f"{s.lower()}@kline_{tf}" for s, tf in subs)


def parse_closed_kline(raw: str | bytes) -> tuple[str, str, int] | None:
    """(SYMBOL, interval, open_ms) for a closed kline message; None for anything else (never raises)."""
    try:
        msg = json.loads(raw)
        data = msg.get("data", msg) if isinstance(msg, dict) else None
        k = data.get("k") if isinstance(data, dict) and data.get("e") == "kline" else None
        if not isinstance(k, dict) or k.get("x") is not True:
            return None
        symbol, interval, open_ms = data.get("s"), k.get("i"), k.get("t")
        if isinstance(symbol, str) and isinstance(interval, str) and isinstance(open_ms, int):
            return symbol.upper(), interval, open_ms
    except (ValueError, TypeError, AttributeError):
        pass
    return None


@dataclass
class StreamStatus:
    market: str
    streams: int = 0
    connected: bool = False
    connects: int = 0
    failures: int = 0
    last_message_ms: int | None = None
    last_closed_bar: str | None = None
    last_error: str | None = None
    duplicates_ignored: int = 0
    dropped_streams: int = 0  # subscriptions beyond one connection's limit (reported, not silently ignored)


@dataclass
class KlineStreams:
    """One reconnecting connection per market (and per 200 streams). Subscriptions can change at any time."""
    on_closed: OnClosed
    connect: Connector | None = None
    rng: random.Random = field(default_factory=random.Random)
    clock: Callable[[], float] = time.time
    subs: dict[str, set[tuple[str, str]]] = field(default_factory=dict)  # market -> {(symbol, tf)}
    status: dict[str, StreamStatus] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)  # per market; a change reconnects only that market
    _seen: dict[tuple[str, str, str, int], None] = field(default_factory=dict)

    def set_subscriptions(self, wanted: set[tuple[str, str, str]]) -> None:
        """wanted: {(symbol, market, timeframe)}. Unsupported markets are ignored."""
        new: dict[str, set[tuple[str, str]]] = {m: set() for m in HOSTS}
        for symbol, market, tf in wanted:
            if market in new:
                new[market].add((symbol, tf))
        for market in HOSTS:
            if new[market] != self.subs.get(market):
                self.versions[market] = self.versions.get(market, 0) + 1
        self.subs = new

    def _deliver(self, market: str, parsed: tuple[str, str, int]) -> None:
        key = (market, *parsed)
        st = self.status[market]
        if key in self._seen:  # Binance may resend a closed kline after a reconnect
            st.duplicates_ignored += 1
            return
        self._seen[key] = None
        if len(self._seen) > SEEN_LIMIT:  # bounded memory: drop the oldest half
            for old in list(self._seen)[: SEEN_LIMIT // 2]:
                del self._seen[old]
        st.last_closed_bar = f"{parsed[0]} {parsed[1]} {parsed[2]}"
        try:
            self.on_closed(parsed[0], market, parsed[1], parsed[2])
        except Exception:  # a bad callback must not kill the stream
            log.exception("kline callback failed")

    async def _connect(self, url: str) -> Any:
        if self.connect is not None:
            return await self.connect(url)
        import websockets  # optional dependency: pip install '.[accounts]'
        return await websockets.connect(url, open_timeout=10, ping_interval=None, max_size=2**20, max_queue=64)

    def reconnect_delay_s(self, failures: int) -> float:
        """Exponential backoff with jitter; jitter is applied BEFORE the cap so the delay never exceeds
        MAX_BACKOFF_S (PERF-8)."""
        return min(MAX_BACKOFF_S, 2 ** min(failures, 7) * (0.5 + self.rng.random()))

    async def _run_chunk(self, market: str, names: list[str], version: int, stop: asyncio.Event) -> None:
        """``version`` is the subscription version ``names`` were built from (captured together, FLOW-7): a
        change during the handshake ends this connection at once instead of being missed."""
        st = self.status[market]
        url = f"{HOSTS[market]}?streams={'/'.join(names)}"
        ws = await self._connect(url)
        st.connected, st.connects, st.last_error = True, st.connects + 1, None
        opened = self.clock()
        try:
            while not stop.is_set() and version == self.versions.get(market, 0) and self.clock() - opened < ROTATE_AFTER_S:
                if st.failures and self.clock() - opened >= HEALTHY_AFTER_S:
                    st.failures = 0
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except TimeoutError:
                    continue
                st.last_message_ms = int(self.clock() * 1000)
                parsed = parse_closed_kline(raw)
                if parsed is not None:
                    self._deliver(market, parsed)
        finally:
            st.connected = False
            await ws.close()

    async def _run_market(self, market: str, stop: asyncio.Event) -> None:
        st = self.status.setdefault(market, StreamStatus(market))
        while not stop.is_set():
            version = self.versions.get(market, 0)  # read with the names, before any await
            wanted = stream_names(self.subs.get(market, set()))
            names = wanted[:MAX_STREAMS_PER_CONNECTION]
            st.streams, st.dropped_streams = len(names), len(wanted) - len(names)
            if st.dropped_streams:
                log.warning("%d %s kline streams over the per-connection limit use the REST schedule only",
                            st.dropped_streams, market)
            if not names:
                await asyncio.sleep(1)
                continue
            try:
                await self._run_chunk(market, names, version, stop)
            except Exception as exc:  # network errors, handshake refusals, server shutdowns
                st.failures += 1
                st.last_error = type(exc).__name__
                delay = self.reconnect_delay_s(st.failures)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(*(self._run_market(m, stop) for m in HOSTS))

    def snapshot(self) -> list[dict[str, Any]]:
        return [vars(s).copy() for s in self.status.values()]
