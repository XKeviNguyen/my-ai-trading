"""Regression tests for the independent Breaker review findings (one test per reproduced defect)."""
import asyncio
import random
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from fakes import PERP, make_exchange
from my_ai_trading.api import create_app
from my_ai_trading.compiler import compile_prompts
from my_ai_trading.copilot import Copilot
from my_ai_trading.domain import HOUR_MS
from my_ai_trading.monitor import Monitor
from my_ai_trading.providers.binance import MarketDataError, _NoRedirect
from my_ai_trading.store import Store, UserSettings, WatchItem

TOKEN = "k" * 43
H = {"Authorization": f"Bearer {TOKEN}"}


def test_f1_compiler_has_no_catastrophic_backtracking():
    adversarial = ["x" + " " * 1998 + "x", "4h " + "\t" * 1990 + "candle", "close " * 330]
    start = time.perf_counter()
    for prompt in adversarial:
        compile_prompts([prompt[:2000]] * 10)
    assert time.perf_counter() - start < 2.0  # was ~12 s per single 2000-char prompt


@pytest.fixture
def client():
    ex, svc = make_exchange()
    store = Store()
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c, ex, svc, store
    store.close()


def test_f2_auth_and_size_checked_before_body_is_parsed(client):
    c, *_ = client
    r = c.post("/api/v1/strategies/compile", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 401  # was 422: body parsed before auth
    big = b'{"prompts": ["' + b"a" * (70 * 1024) + b'"]}'
    assert c.post("/api/v1/strategies/compile", content=big, headers={"Content-Type": "application/json"}
                  ).status_code == 401
    assert c.post("/api/v1/strategies/compile", content=big,
                  headers={**H, "Content-Type": "application/json"}).status_code == 413
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    assert c.get("/api/v1/health", headers={"Host": "testserver"}).status_code == 421


def make_monitor(settings, svc=None, ex=None):
    if svc is None:
        ex, svc = make_exchange()
    store = Store()
    store.save_settings(settings)
    return ex, svc, store, Monitor(svc, store, rng=random.Random(0))


def test_f3_unresolvable_symbol_does_not_stop_other_symbols():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT"), WatchItem(symbol="GONEUSDT")], timeframes=["1h"])
    ex, svc, store, mon = make_monitor(s)
    asyncio.run(mon.tick())
    assert mon.state[(PERP.key, "1h")].last_bar_open_ms is not None
    assert any(r.get("status") == "UNRESOLVED" for r in mon.status())
    calls = ex.calls
    asyncio.run(mon.tick())  # unresolved symbol is not retried every tick
    assert ex.calls == calls


def test_f4_secondary_timeframe_failure_retries_the_bar_instead_of_skipping_it():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"])
    ex, svc, store, mon = make_monitor(s)
    rule = compile_prompts(["1h close above SMA20 and daily close above SMA50"]).rule
    sid, v = store.create_strategy(rule, ["x"])
    store.approve(sid, v, rule.rule_hash())
    real = ex.klines

    def flaky(inst, tf, **kw):
        if tf.code == "1d":
            raise MarketDataError("1d down")
        return real(inst, tf, **kw)
    ex.klines = flaky
    asyncio.run(mon.tick())  # must not raise out of tick()
    st = mon.state[(PERP.key, "1h")]
    assert st.last_bar_open_ms is None and "1d down" in st.last_error
    ex.klines = real
    st.next_due_ms = 0
    asyncio.run(mon.tick())
    assert any(a["event_type"] == "STRATEGY_MATCH" for a in store.alerts())


def test_f5_malformed_provider_row_cannot_poison_the_cache():
    ex, svc = make_exchange()
    good = list(ex.histories["1h"])
    i = sum(c.open_time_ms <= ex.now for c in good) - 5  # a row the provider actually serves
    bad = replace(good[i], open_time_ms=good[i].open_time_ms + 30 * 60_000,
                  close_time_ms=good[i].close_time_ms + 30 * 60_000)
    ex.histories["1h"] = good[:i] + [bad] + good[i + 1:]
    with pytest.raises(MarketDataError):
        svc.series(PERP, "1h")
    ex.histories["1h"] = good
    recovered = svc.series(PERP, "1h")  # recovers once the provider is sane
    assert recovered.gaps == () and len(recovered.candles) >= 399  # newest bar may sit in the skew margin


def test_f6_stale_subscription_backs_off_instead_of_polling_every_second():
    s = UserSettings(watchlist=[WatchItem(symbol="SANDUSDT")], timeframes=["1h"])
    ex, svc, store, mon = make_monitor(s)
    ex.histories["1h"] = [c for c in ex.histories["1h"] if c.open_time_ms < ex.now - 6 * HOUR_MS]
    asyncio.run(mon.tick())
    calls = ex.calls
    for _ in range(10):
        ex.now += 1_000
        asyncio.run(mon.tick())
    assert ex.calls == calls and mon.state[(PERP.key, "1h")].next_due_ms > ex.now


def test_f7_incremental_alert_polling_never_skips():
    store = Store()
    for i in range(10):
        store.record_alert(f"e{i}", user="local", instrument_key="k", timeframe="1h", source="s",
                           event_type="X", bar_open_time_ms=i, priority=1, payload={})
    assert [a["seq"] for a in store.alerts(after_seq=0, limit=3)] == [10, 9, 8]  # initial view: newest
    seen, cursor = [], 2  # poller last saw seq 2; eight new alerts arrived, more than one page
    while rows := store.alerts(after_seq=cursor, limit=3):
        seen.extend(r["seq"] for r in rows)
        cursor = rows[-1]["seq"]
    assert seen == list(range(3, 11))  # was [10, 9, 8] then nothing: seqs 3..7 lost


def test_f8_idle_cache_does_not_invent_gaps():
    ex, svc = make_exchange()
    svc.series(PERP, "1h", bars=400)
    ex.now += 450 * HOUR_MS
    for h in ("1h",):
        ex.histories[h] = ex.histories[h] + [
            replace(ex.histories[h][-1], open_time_ms=ex.histories[h][-1].open_time_ms + k * HOUR_MS,
                    close_time_ms=ex.histories[h][-1].close_time_ms + k * HOUR_MS) for k in range(1, 460)]
    s = svc.series(PERP, "1h", bars=600)
    assert s.gaps == () and s.missing_bars() == 0


def test_f9_snapshot_series_are_cut_at_the_snapshot_instant():
    ex, svc = make_exchange()
    snap = svc.snapshot(PERP, ("1h", "4h"))
    for s in snap["_series"].values():
        assert s.candles[-1].close_time_ms < snap["as_of_ms"]


def test_redirects_are_never_followed():
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None
