"""Market-data service: server clock, shared closed-bar cache, multi-timeframe snapshots.

Closed bars are immutable, so the cache only refetches once the next bar should have closed.
Public market data is shared across users; nothing user-specific is stored here.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Protocol

from .domain import Candle, CandleSeries, InstrumentIdentity, MarketType, Timeframe
from .engine import analyze, utc_iso
from .patterns import recent_patterns
from .providers.binance import MarketDataError, validate_symbol

CLOSE_SAFETY_MS = 1_500        # bar counts as closed only this long after its end (clock skew)
CLOCK_RESYNC_S = 600
CLOCK_RETRY_S = 60
DEFAULT_BARS = 400
MAX_CACHE_KEYS = 256
DEFAULT_TIMEFRAMES = ("1h", "4h", "1d", "1w")


class Provider(Protocol):
    def server_time_ms(self, market: MarketType = ...) -> int: ...
    def klines(self, instrument: InstrumentIdentity, timeframe: Timeframe, *, limit: int = ...,
               end_time_ms: int | None = ...) -> list[Candle]: ...
    def instrument(self, symbol: str, market: MarketType) -> InstrumentIdentity: ...


class ServerClock:
    """Local clock corrected by the provider's server-time offset."""

    def __init__(self, provider: Provider, *, local: Callable[[], float] = time.time):
        self._provider, self._local = provider, local
        self._offset_ms: int | None = None
        self._synced_at = 0.0
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        local_ms = int(self._local() * 1000)
        with self._lock:
            due = self._offset_ms is None or self._local() - self._synced_at > CLOCK_RESYNC_S
        if due:
            try:
                before = int(self._local() * 1000)
                server = self._provider.server_time_ms()
                after = int(self._local() * 1000)
                with self._lock:
                    self._offset_ms = server - (before + after) // 2
                    self._synced_at = self._local()
            except MarketDataError:
                if self._offset_ms is None:
                    raise
                with self._lock:  # keep the old offset; retry in ~60 s instead of on every call
                    self._synced_at = self._local() - CLOCK_RESYNC_S + CLOCK_RETRY_S
        return local_ms + (self._offset_ms or 0)


class MarketDataService:
    def __init__(self, provider: Provider, *, clock: ServerClock | None = None):
        self.provider = provider
        self.clock = clock or ServerClock(provider)
        self._cache: OrderedDict[tuple[str, str], list[Candle]] = OrderedDict()
        self._instruments: dict[tuple[str, str], InstrumentIdentity] = {}
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._history_start: dict[tuple[str, str], bool] = {}
        self._guard = threading.Lock()
        self.fetch_count = 0

    def resolve(self, symbol: str, market: MarketType | str = MarketType.USDM_PERPETUAL) -> InstrumentIdentity:
        market = MarketType(market)
        key = (validate_symbol(symbol), str(market))
        with self._guard:
            hit = self._instruments.get(key)
        if hit:
            return hit
        inst = self.provider.instrument(symbol, market)
        with self._guard:
            self._instruments[key] = inst
        return inst

    def _lock_for(self, key: tuple[str, str]) -> threading.Lock:
        with self._guard:
            return self._key_locks.setdefault(key, threading.Lock())

    def series(self, instrument: InstrumentIdentity, timeframe: Timeframe | str, *,
               bars: int = DEFAULT_BARS, now_ms: int | None = None) -> CandleSeries:
        """Closed bars as of ``now_ms`` (default: server now). Raises MarketDataError on bad provider data."""
        tf = timeframe if isinstance(timeframe, Timeframe) else Timeframe(timeframe)
        if not 1 <= bars <= 1499:  # provider max is 1500 rows, one of which may be forming
            raise ValueError("bars must be in 1..1499")
        key = (instrument.key, tf.code)
        now = self.clock.now_ms() if now_ms is None else now_ms
        with self._lock_for(key):  # single-flight per key: concurrent callers share one fetch
            cached = self._cache.get(key, [])
            have_enough = len(cached) >= bars or self._history_start.get(key, False)
            next_close_due = tf.next_bar_close_ms(cached[-1].close_time_ms) + 1 if cached else -1
            if not cached or not have_enough or now > next_close_due + CLOSE_SAFETY_MS:
                # +1: the newest row returned is usually the still-forming bar, which is dropped.
                full = min(bars + 1, 1500)
                needed = (now - cached[-1].close_time_ms) // tf.duration_ms + 3 if cached else full
                if needed > full:  # idle longer than one fetch window: refetch rather than leave a hole
                    cached, have_enough = [], False
                limit = full if not have_enough else min(full, max(3, needed))
                fresh = self.provider.klines(instrument, tf, limit=limit)
                self.fetch_count += 1
                if limit == full and len(fresh) < full:
                    self._history_start[key] = True  # provider has no older bars (new listing)
                merged = _merge(cached, [c for c in fresh if c.is_closed(now, safety_ms=CLOSE_SAFETY_MS)])
                try:  # validate BEFORE caching so one malformed row cannot poison the key
                    CandleSeries(instrument, tf, tuple(merged))
                except ValueError as exc:
                    with self._guard:
                        self._cache.pop(key, None)
                    raise MarketDataError(f"Provider returned inconsistent {tf} candles") from exc
                cached = merged
                with self._guard:
                    self._cache[key] = cached[-1500:]
                    self._cache.move_to_end(key)
                    while len(self._cache) > MAX_CACHE_KEYS:
                        self._cache.popitem(last=False)
        return CandleSeries.from_candles(instrument, tf, cached[-bars:], now_ms=now, safety_ms=CLOSE_SAFETY_MS)


    def snapshot(self, instrument: InstrumentIdentity, timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES,
                 *, bars: int = DEFAULT_BARS) -> dict[str, Any]:
        """Multi-timeframe facts at one instant. Per-timeframe failures are reported, not hidden."""
        now = self.clock.now_ms()
        reports: dict[str, Any] = {}
        series: dict[str, CandleSeries] = {}
        for code in timeframes:
            try:
                s = self.series(instrument, code, bars=bars, now_ms=now)
                series[code] = s
                reports[code] = analyze(s, as_of_ms=now)
                reports[code]["patterns"] = [] if reports[code]["status"] == "STALE_DATA" else recent_patterns(s)
            except MarketDataError as exc:
                reports[code] = {"timeframe": code, "status": "PROVIDER_ERROR", "error": str(exc)}
        return {"instrument": instrument.as_dict(), "as_of_ms": now, "as_of_utc": utc_iso(now),
                "source": "binance public REST", "timeframes": reports,
                "alignment": alignment(reports), "_series": series}


def _merge(old: list[Candle], new: list[Candle]) -> list[Candle]:
    """Merge by open time; a later fetch of the same closed bar replaces the earlier copy."""
    by_open = {c.open_time_ms: c for c in old}
    for c in new:
        by_open[c.open_time_ms] = c
    return [by_open[k] for k in sorted(by_open)]


def alignment(reports: dict[str, Any]) -> dict[str, Any]:
    """Summarize per-timeframe trend states; disagreement is reported, not voted away."""
    states = {tf: r.get("trend", {}).get("state", "UNKNOWN") for tf, r in reports.items()
              if r.get("status") in ("OK", "INSUFFICIENT_DATA")}
    known = {s for s in states.values() if s in ("UP", "DOWN")}
    if not known:
        verdict = "UNKNOWN"
    elif len(known) == 1 and all(s in known for s in states.values()):
        verdict = f"ALIGNED_{known.pop()}"
    else:
        verdict = "CONFLICTING" if len(known) > 1 else "PARTIAL"
    return {"trend_by_timeframe": states, "verdict": verdict,
            "note": "Timeframes are context for each other; a lower-timeframe move does not change a "
                    "higher-timeframe conclusion until that timeframe's own bar closes."}
