import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

from my_ai_trading.domain import DAY_MS, InstrumentIdentity, MarketType, Timeframe
from my_ai_trading.fixtures import candles_from_closes
from my_ai_trading.market import CLOSE_SAFETY_MS, MarketDataService, ServerClock
from my_ai_trading.providers.binance import (BinancePublic, HttpResult, MarketDataError,
                                             RateLimitedError)

PERP = InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, "SANDUSDT", "USDT")


def kline_row(c):
    return [c.open_time_ms, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume),
            c.close_time_ms, str(c.quote_volume), 10, "0", "0", "0"]


class FakeHttp:
    def __init__(self, responder):
        self.responder, self.urls = responder, []

    def __call__(self, url, deadline, max_bytes):
        self.urls.append(url)
        return self.responder(url)


def ok(payload, headers=None):
    return HttpResult(200, json.dumps(payload).encode(), headers or {})


def test_fixed_hosts_per_market_and_allowlisted_query():
    http = FakeHttp(lambda url: ok([]))
    p = BinancePublic(http)
    p.klines(PERP, Timeframe("1h"), limit=5)
    p.klines(InstrumentIdentity("binance", MarketType.SPOT, "SANDUSDT"), Timeframe("1h"), limit=5)
    perp_url, spot_url = (urlparse(u) for u in http.urls)
    assert (perp_url.netloc, perp_url.path) == ("fapi.binance.com", "/fapi/v1/klines")
    assert (spot_url.netloc, spot_url.path) == ("api.binance.com", "/api/v3/klines")
    assert parse_qs(perp_url.query) == {"symbol": ["SANDUSDT"], "interval": ["1h"], "limit": ["5"]}
    for bad in ("sandusdt", "SAND/USDT", "http://x", "A" * 21):
        with pytest.raises(ValueError):
            p.klines(InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, bad), Timeframe("1h"))
    with pytest.raises(ValueError):
        p.klines(PERP, Timeframe("1h"), limit=0)


def test_rate_limit_honours_retry_after_and_blocks_further_calls():
    now = [1000.0]
    http = FakeHttp(lambda url: HttpResult(429, b"", {"retry-after": "30"}))
    p = BinancePublic(http, clock=lambda: now[0])
    with pytest.raises(RateLimitedError) as exc:
        p.klines(PERP, Timeframe("1h"))
    assert exc.value.retry_after_s == 30
    with pytest.raises(RateLimitedError):
        p.klines(PERP, Timeframe("1h"))
    assert len(http.urls) == 1  # second call never reached the network
    now[0] += 31
    http.responder = lambda url: ok([])
    assert p.klines(PERP, Timeframe("1h")) == []


def test_ip_ban_418_and_http_errors_and_bad_payloads():
    p = BinancePublic(FakeHttp(lambda url: HttpResult(418, b"", {})))
    with pytest.raises(RateLimitedError) as exc:
        p.server_time_ms()
    assert exc.value.retry_after_s == 60
    for res in (HttpResult(500, b"", {}), HttpResult(200, b"not json", {}), ok({"x": 1}),
                ok([[1, "1"]]), ok([kline_row(candles_from_closes([1.0])[0])[:6] + [1.5, "1"]])):
        with pytest.raises(MarketDataError):
            BinancePublic(FakeHttp(lambda url, r=res: r)).klines(PERP, Timeframe("1d"))
    with pytest.raises(MarketDataError):
        BinancePublic(FakeHttp(lambda url: ok({"serverTime": "soon"}))).server_time_ms()


def test_used_weight_is_tracked():
    p = BinancePublic(FakeHttp(lambda url: ok({"serverTime": 5}, {"x-mbx-used-weight-1m": "42"})))
    assert p.server_time_ms() == 5 and p.last_used_weight == 42


def test_instrument_resolution_checks_perpetual_and_trading_status():
    info = {"symbols": [
        {"symbol": "SANDUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
        {"symbol": "BTCUSDT_261225", "contractType": "CURRENT_QUARTER", "status": "TRADING"},
        {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "status": "SETTLING"},
    ]}
    p = BinancePublic(FakeHttp(lambda url: ok(info)))
    inst = p.instrument("SANDUSDT", MarketType.USDM_PERPETUAL)
    assert inst.key == "binance:usdm_perpetual:SANDUSDT" and inst.quote_asset == "USDT"
    for sym in ("BTCUSDT_261225", "OLDUSDT", "NOPEUSDT"):
        with pytest.raises(ValueError):
            p.instrument(sym, MarketType.USDM_PERPETUAL)


class FakeProvider:
    """Serves a fixed candle history, hiding bars that have not opened yet at 'server now'."""

    def __init__(self, candles, now_ms):
        self.candles, self.now_ms, self.kline_calls = candles, now_ms, 0
        self.lock = threading.Lock()

    def server_time_ms(self, market=None):
        return self.now_ms

    def instrument(self, symbol, market):
        return PERP

    def klines(self, instrument, timeframe, *, limit=500, end_time_ms=None):
        with self.lock:
            self.kline_calls += 1
        time.sleep(0.01)
        visible = [c for c in self.candles if c.open_time_ms <= self.now_ms]
        return visible[-limit:]


def make_service(n=60):
    cs = candles_from_closes([1.0 + i * 0.01 for i in range(n)])
    # "now" is mid-way through the last bar, so it is still forming.
    prov = FakeProvider(cs, cs[-1].open_time_ms + DAY_MS // 2)
    clock = ServerClock(prov, local=lambda: prov.now_ms / 1000)
    return cs, prov, MarketDataService(prov, clock=clock)


def test_series_excludes_forming_bar_and_caches_until_next_close():
    cs, prov, svc = make_service()
    s = svc.series(PERP, "1d", bars=50)
    assert s.candles[-1] == cs[-2]  # forming bar excluded
    svc.series(PERP, "1d", bars=50)
    assert prov.kline_calls == 1  # served from cache, no new bar could have closed
    prov.now_ms = cs[-1].close_time_ms + 1 + CLOSE_SAFETY_MS + 1
    s2 = svc.series(PERP, "1d", bars=50)
    assert prov.kline_calls == 2 and s2.candles[-1] == cs[-1]


def test_concurrent_requests_share_one_fetch():
    _, prov, svc = make_service()
    threads = [threading.Thread(target=svc.series, args=(PERP, "1d"), kwargs={"bars": 50}) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert prov.kline_calls == 1


def test_new_listing_with_short_history_does_not_refetch_every_call():
    _, prov, svc = make_service(n=30)
    assert len(svc.series(PERP, "1d", bars=400).candles) == 29
    svc.series(PERP, "1d", bars=400)
    assert prov.kline_calls == 1


def test_snapshot_reports_provider_errors_per_timeframe():
    _, prov, svc = make_service()

    def boom(*a, **k):
        raise MarketDataError("down")
    real = prov.klines
    prov.klines = lambda inst, tf, **k: boom() if tf.code == "4h" else real(inst, tf, **k)
    snap = svc.snapshot(PERP, ("1d", "4h"))
    assert snap["timeframes"]["4h"]["status"] == "PROVIDER_ERROR"
    assert snap["timeframes"]["1d"]["status"] in ("OK", "INSUFFICIENT_DATA")
