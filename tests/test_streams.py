"""R8 M7: kline streams wake the monitor; they never feed data into analysis. No network: a scripted socket."""
import asyncio
import json
import random

import pytest

from my_ai_trading import streams as S

DOC_KLINE = {  # shape of the Binance kline stream payload (docs)
    "e": "kline", "E": 1672515782136, "s": "BNBBTC",
    "k": {"t": 1672515780000, "T": 1672515839999, "s": "BNBBTC", "i": "1m", "f": 100, "L": 200, "o": "0.0010",
          "c": "0.0020", "h": "0.0025", "l": "0.0015", "v": "1000", "n": 100, "x": False, "q": "1.0000",
          "V": "500", "Q": "0.500", "B": "123456"},
}


def msg(symbol="BTCUSDT", interval="1h", open_ms=1_000, closed=True):
    data = json.loads(json.dumps(DOC_KLINE))
    data["s"], data["k"]["i"], data["k"]["t"], data["k"]["x"] = symbol, interval, open_ms, closed
    return json.dumps({"stream": f"{symbol.lower()}@kline_{interval}", "data": data})


def test_parse_only_closed_klines_and_never_raise():
    assert S.parse_closed_kline(msg()) == ("BTCUSDT", "1h", 1_000)
    assert S.parse_closed_kline(msg(closed=False)) is None
    assert S.parse_closed_kline(json.dumps(DOC_KLINE)) is None  # raw (non-combined) and still forming
    for junk in ("", "not json", "[]", "null", '{"data": {"e": "kline", "k": "x"}}', '{"e":"trade"}', b"\xff\xfe"):
        assert S.parse_closed_kline(junk) is None


def test_hosts_are_the_documented_market_data_endpoints_and_names_are_lowercase():
    assert S.HOSTS == {"spot": "wss://data-stream.binance.vision/stream",
                       "usdm_perpetual": "wss://fstream.binance.com/market/stream"}
    assert S.stream_names({("BTCUSDT", "1h"), ("SANDUSDT", "1M")}) == ["btcusdt@kline_1h", "sandusdt@kline_1M"]


class FakeSocket:
    def __init__(self, messages, stop):
        self.messages, self.stop, self.closed = list(messages), stop, False

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        self.stop.set()  # script finished: end the test run
        await asyncio.sleep(0.01)
        return "{}"  # not a kline: ignored

    async def close(self):
        self.closed = True


def run(streams, stop, timeout=5):
    async def main():
        await asyncio.wait_for(streams.run(stop), timeout)
    asyncio.run(main())


def test_closed_bars_are_delivered_once_even_when_resent():
    got, urls, stop = [], [], None

    async def connect(url):
        urls.append(url)
        return FakeSocket([msg(open_ms=1), msg(open_ms=1), msg(closed=False), "garbage", msg(open_ms=2)], stop)

    async def main():
        nonlocal stop
        stop = asyncio.Event()
        ks = S.KlineStreams(on_closed=lambda *a: got.append(a), connect=connect)
        ks.set_subscriptions({("BTCUSDT", "usdm_perpetual", "1h"), ("ETHUSDT", "binance_options", "1h")})
        await asyncio.wait_for(ks.run(stop), 5)
        return ks
    ks = asyncio.run(main())
    assert got == [("BTCUSDT", "usdm_perpetual", "1h", 1), ("BTCUSDT", "usdm_perpetual", "1h", 2)]
    assert urls == ["wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1h"]  # unsupported market ignored
    st = ks.status["usdm_perpetual"]
    assert st.duplicates_ignored == 1 and st.connects == 1 and st.connected is False


def test_failures_back_off_with_jitter_and_then_reconnect(monkeypatch):
    monkeypatch.setattr(S, "MAX_BACKOFF_S", 0.01)
    attempts, stop = [], None

    async def connect(url):
        attempts.append(url)
        if len(attempts) < 3:
            raise ConnectionRefusedError("down")
        return FakeSocket([msg()], stop)

    async def main():
        nonlocal stop
        stop = asyncio.Event()
        ks = S.KlineStreams(on_closed=lambda *a: None, connect=connect, rng=random.Random(1))
        ks.set_subscriptions({("BTCUSDT", "spot", "4h")})
        await asyncio.wait_for(ks.run(stop), 5)
        return ks
    ks = asyncio.run(main())
    assert len(attempts) == 3 and ks.status["spot"].connects == 1
    assert ks.status["spot"].failures == 2  # resets only after a connection stays up HEALTHY_AFTER_S


def test_subscription_changes_reconnect_with_the_new_streams():
    urls, stop, ks = [], None, None

    async def connect(url):
        urls.append(url)
        if len(urls) == 1:
            async def later():
                await asyncio.sleep(0.05)
                ks.set_subscriptions({("BTCUSDT", "spot", "1h"), ("ETHUSDT", "spot", "1h")})
            asyncio.ensure_future(later())
            return FakeSocket([], asyncio.Event())  # never finishes by itself
        return FakeSocket([msg()], stop)

    async def main():
        nonlocal stop, ks
        stop = asyncio.Event()
        ks = S.KlineStreams(on_closed=lambda *a: None, connect=connect)
        ks.set_subscriptions({("BTCUSDT", "spot", "1h")})
        await asyncio.wait_for(ks.run(stop), 15)
    asyncio.run(main())
    assert urls[0].endswith("streams=btcusdt@kline_1h")
    assert urls[1].endswith("streams=btcusdt@kline_1h/ethusdt@kline_1h")


def test_monitor_wakes_immediately_on_a_closed_bar_instead_of_waiting_for_its_schedule():
    from fakes import make_exchange
    from my_ai_trading.monitor import Monitor
    from my_ai_trading.store import Store, UserSettings

    _, svc = make_exchange()
    store = Store()
    store.save_settings(UserSettings(watchlist=[{"symbol": "SANDUSDT", "market": "usdm_perpetual"}], timeframes=["1h"]))
    mon = Monitor(svc, store)
    seen = []
    mon.on_subscriptions = seen.append
    ticks = []
    original = mon.tick

    async def counting_tick():
        ticks.append(1)
        return await original()
    mon.tick = counting_tick

    async def main():
        stop = asyncio.Event()
        runner = asyncio.ensure_future(mon.run_forever(stop=stop, idle_s=60))
        await asyncio.sleep(0.3)  # first tick done; the next one is ~an hour away
        assert len(ticks) == 1
        key = next(iter(mon.state))
        mon.wake("SANDUSDT", "usdm_perpetual", "1h", 0)
        assert mon.state[key].next_due_ms == 0
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(runner, 5)
    asyncio.run(main())
    assert len(ticks) == 2  # woke up well before the 60 s idle timeout
    assert seen[0] == {("SANDUSDT", "usdm_perpetual", "1h")}


def test_a_spot_change_does_not_reconnect_futures_and_overflow_is_reported():
    ks = S.KlineStreams(on_closed=lambda *a: None)
    ks.set_subscriptions({("BTCUSDT", "spot", "1h"), ("BTCUSDT", "usdm_perpetual", "1h")})
    before = dict(ks.versions)
    ks.set_subscriptions({("ETHUSDT", "spot", "1h"), ("BTCUSDT", "usdm_perpetual", "1h")})
    assert ks.versions["usdm_perpetual"] == before["usdm_perpetual"] and ks.versions["spot"] == before["spot"] + 1
    many = {(f"S{i:04d}USDT", "spot", "1h") for i in range(S.MAX_STREAMS_PER_CONNECTION + 5)}

    async def main():
        stop = asyncio.Event()
        urls = []

        async def connect(url):
            urls.append(url)
            stop.set()
            raise ConnectionRefusedError("test")
        k = S.KlineStreams(on_closed=lambda *a: None, connect=connect)
        k.set_subscriptions(many)
        await asyncio.wait_for(k.run(stop), 5)
        return k
    k = asyncio.run(main())
    assert k.status["spot"].streams == S.MAX_STREAMS_PER_CONNECTION and k.status["spot"].dropped_streams == 5


def test_a_wake_never_cuts_short_a_rate_limit_backoff():
    from fakes import make_exchange
    from my_ai_trading.monitor import Monitor, SubState
    from my_ai_trading.store import Store
    mon = Monitor(make_exchange()[1], Store())
    mon.state[("binance:usdm_perpetual:SANDUSDT", "1h")] = SubState(next_due_ms=10**15, failures=1)
    mon.wake("SANDUSDT", "usdm_perpetual", "1h", 0)
    assert mon.state[("binance:usdm_perpetual:SANDUSDT", "1h")].next_due_ms == 10**15
