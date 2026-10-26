"""Public Binance market data (USD-M perpetual futures and spot). No credentials, no trading endpoints.

Hosts and paths are fixed constants; only allowlisted symbol/interval/limit values reach the query
string, so there is no SSRF surface.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..domain import Candle, InstrumentIdentity, MarketType, Timeframe

SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")
MAX_LIMIT = 1500
_ENDPOINTS = {
    MarketType.USDM_PERPETUAL: {"base": "https://fapi.binance.com", "klines": "/fapi/v1/klines",
                                "time": "/fapi/v1/time", "info": "/fapi/v1/exchangeInfo"},
    MarketType.SPOT: {"base": "https://api.binance.com", "klines": "/api/v3/klines",
                      "time": "/api/v3/time", "info": "/api/v3/exchangeInfo"},
}
USER_AGENT = "MyAITrading/0.2 (read-only research)"


class _NoRedirect(HTTPRedirectHandler):
    """Never follow redirects: a 3xx could move a request off the fixed public host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect)


class MarketDataError(Exception):
    """Expected provider failure; message is safe to show (no secrets, no raw payloads)."""


class RateLimitedError(MarketDataError):
    def __init__(self, retry_after_s: float):
        super().__init__(f"Provider rate limit; retry after {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s


def validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must be 3-20 uppercase letters/digits (e.g. SANDUSDT)")
    return symbol


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]


Fetcher = Callable[[str, float, int], HttpResult]


def urllib_fetch(url: str, deadline_s: float, max_bytes: int) -> HttpResult:
    """GET with a *total* deadline and a hard size cap."""
    start = time.monotonic()
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=min(deadline_s, 5.0)) as resp:
            chunks, size = [], 0
            while True:
                if time.monotonic() - start > deadline_s:
                    raise MarketDataError("Provider response exceeded deadline")
                chunk = resp.read(65_536)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise MarketDataError("Provider response too large")
                chunks.append(chunk)
            return HttpResult(resp.status, b"".join(chunks), {k.lower(): v for k, v in resp.headers.items()})
    except HTTPError as exc:
        return HttpResult(exc.code, b"", {k.lower(): v for k, v in (exc.headers or {}).items()})
    except (URLError, TimeoutError, OSError) as exc:
        raise MarketDataError("Network error contacting market data provider") from exc


class BinancePublic:
    """Synchronous public adapter, safe to share across threads."""

    def __init__(self, fetch: Fetcher = urllib_fetch, *, deadline_s: float = 10.0,
                 clock: Callable[[], float] = time.time):
        self._fetch = fetch
        self._deadline_s = deadline_s
        self._clock = clock
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self.last_used_weight: int | None = None

    def _get(self, market: MarketType, path_key: str, params: dict[str, Any] | None = None,
             *, max_bytes: int = 2_000_000) -> Any:
        with self._lock:
            wait = self._blocked_until - self._clock()
        if wait > 0:
            raise RateLimitedError(wait)  # never hammer a provider that asked us to back off
        ep = _ENDPOINTS[market]
        url = ep["base"] + ep[path_key] + (f"?{urlencode(params)}" if params else "")
        res = self._fetch(url, self._deadline_s, max_bytes)
        weight = res.headers.get("x-mbx-used-weight-1m") or res.headers.get("x-mbx-used-weight")
        if weight and weight.isdigit():
            self.last_used_weight = int(weight)
        if res.status in (418, 429):
            retry = res.headers.get("retry-after", "")
            retry_s = float(retry) if retry.replace(".", "", 1).isdigit() else 60.0
            retry_s = min(max(retry_s, 1.0), 3600.0)
            with self._lock:
                self._blocked_until = max(self._blocked_until, self._clock() + retry_s)
            raise RateLimitedError(retry_s)
        if res.status != 200:
            raise MarketDataError(f"Market data provider returned HTTP {res.status}")
        try:
            return json.loads(res.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise MarketDataError("Provider returned malformed JSON") from exc

    def server_time_ms(self, market: MarketType = MarketType.USDM_PERPETUAL) -> int:
        data = self._get(market, "time")
        st = data.get("serverTime") if isinstance(data, dict) else None
        if isinstance(st, bool) or not isinstance(st, int) or st <= 0:
            raise MarketDataError("Provider returned invalid server time")
        return st

    def klines(self, instrument: InstrumentIdentity, timeframe: Timeframe, *, limit: int = 500,
               end_time_ms: int | None = None) -> list[Candle]:
        """Raw candles (may include the forming bar); callers filter by close via CandleSeries."""
        validate_symbol(instrument.symbol)
        if instrument.venue != "binance":
            raise ValueError("BinancePublic serves only venue 'binance'")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be an integer in 1..{MAX_LIMIT}")
        params: dict[str, Any] = {"symbol": instrument.symbol, "interval": timeframe.code, "limit": limit}
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        data = self._get(instrument.market_type, "klines", params)
        if not isinstance(data, list) or len(data) > limit:
            raise MarketDataError("Unexpected kline response shape")
        try:
            return [Candle.from_binance(row) for row in data]
        except ValueError as exc:
            raise MarketDataError("Provider returned an invalid candle") from exc

    def instrument(self, symbol: str, market: MarketType) -> InstrumentIdentity:
        """Resolve and verify a symbol is listed and trading on this market type."""
        validate_symbol(symbol)
        params = {"symbol": symbol} if market is MarketType.SPOT else None
        data = self._get(market, "info", params, max_bytes=12_000_000)
        symbols = data.get("symbols") if isinstance(data, dict) else None
        if not isinstance(symbols, list):
            raise MarketDataError("Unexpected exchangeInfo shape")
        for s in symbols:
            if isinstance(s, dict) and s.get("symbol") == symbol:
                if market is MarketType.USDM_PERPETUAL and s.get("contractType") != "PERPETUAL":
                    raise ValueError(f"{symbol} is not a perpetual contract on Binance USD-M")
                if s.get("status") != "TRADING":
                    raise ValueError(f"{symbol} is not currently trading (status {s.get('status')!r})")
                quote = s.get("quoteAsset")
                return InstrumentIdentity("binance", market, symbol, quote if isinstance(quote, str) else None)
        raise ValueError(f"{symbol} not found on Binance {market}")
