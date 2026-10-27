"""FLOW-7 (subscription change during the handshake) and PERF-8 (reconnect delay never exceeds its cap)."""
import asyncio
import random

from test_streams import FakeSocket, msg

from my_ai_trading import streams as S


def test_subscription_change_during_handshake_reconnects_with_the_new_streams():
    urls, stop, ks = [], None, None

    async def connect(url):
        urls.append(url)
        if len(urls) == 1:
            # The change lands while the first handshake is still in progress.
            ks.set_subscriptions({("BTCUSDT", "spot", "1h"), ("ETHUSDT", "spot", "1h")})
            await asyncio.sleep(0.01)
            return FakeSocket([], asyncio.Event())  # would stay open forever on the stale stream list
        return FakeSocket([msg()], stop)

    async def main():
        nonlocal stop, ks
        stop = asyncio.Event()
        ks = S.KlineStreams(on_closed=lambda *a: None, connect=connect)
        ks.set_subscriptions({("BTCUSDT", "spot", "1h")})
        await asyncio.wait_for(ks.run(stop), 3)  # the old code never noticed the change: timed out here
    asyncio.run(main())
    assert urls[0].endswith("streams=btcusdt@kline_1h")
    assert urls[1].endswith("streams=btcusdt@kline_1h/ethusdt@kline_1h")


def test_reconnect_delay_is_jittered_exponential_and_never_exceeds_the_cap():
    ks = S.KlineStreams(on_closed=lambda *a: None, rng=random.Random(5))
    worst = 0.0
    for failures in range(1, 30):
        for _ in range(200):
            d = ks.reconnect_delay_s(failures)
            assert 0 < d <= S.MAX_BACKOFF_S
            assert d >= min(S.MAX_BACKOFF_S, 0.5 * 2 ** min(failures, 7))
            worst = max(worst, d)
    assert worst == S.MAX_BACKOFF_S  # the cap is reached, not exceeded (was up to 1.5 x 120 s = 180 s)
    assert ks.reconnect_delay_s(1) < 3 and ks.reconnect_delay_s(6) >= 32  # 2^n x [0.5, 1.5)
