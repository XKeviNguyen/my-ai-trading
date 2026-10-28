"""DATA-5 (gap/partial-response backfill) and PERF-5 (cache sized for the largest watchlist). SYNTHETIC data."""
from my_ai_trading.domain import DAY_MS, InstrumentIdentity, MarketType, Timeframe
from my_ai_trading.engine import analyze
from my_ai_trading.fixtures import candles_from_closes, uptrend_closes
from my_ai_trading.market import HISTORY_RECHECK_MS, MAX_CACHE_KEYS, MarketDataService, ServerClock
from my_ai_trading.store import UserSettings

PERP = InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, "SANDUSDT", "USDT")


class Provider:
    """Fixed history; ``plan`` holds per-call row caps (None = healthy) to simulate partial responses."""

    def __init__(self, candles, now_ms, plan=()):
        self.candles, self.now_ms, self.plan, self.limits = candles, now_ms, list(plan), []

    def server_time_ms(self, market=None):
        return self.now_ms

    def instrument(self, symbol, market):
        return InstrumentIdentity("binance", MarketType(market), symbol, "USDT")

    def klines(self, instrument, timeframe, *, limit=500, end_time_ms=None):
        self.limits.append(limit)
        visible = [c for c in self.candles if c.open_time_ms <= self.now_ms][-limit:]
        cap = self.plan.pop(0) if self.plan else None
        return visible if cap is None else visible[-cap:]


def service(n=700, plan=(), drop=()):
    cs = [c for i, c in enumerate(candles_from_closes(uptrend_closes(n))) if i not in set(drop)]
    prov = Provider(cs, cs[-60].open_time_ms + DAY_MS // 2, plan)  # bar -60 forming; later bars are 'future'
    return cs, prov, MarketDataService(prov, clock=ServerClock(prov, local=lambda: prov.now_ms / 1000))


def next_bar(prov, bars=1):
    prov.now_ms += bars * DAY_MS
    return prov.now_ms


def test_partial_full_response_is_refetched_instead_of_trusted_as_history_start():
    cs, prov, svc = service(n=700, plan=[50])  # first response: only the newest 50 rows
    s = svc.series(PERP, "1d", bars=400)
    assert len(s.candles) == 49
    assert analyze(s, as_of_ms=prov.now_ms)["trend"]["state"] == "UNKNOWN"
    svc.series(PERP, "1d", bars=400)
    assert len(prov.limits) == 1  # not re-asked on every call (a real new listing stays cheap)
    prov.now_ms += HISTORY_RECHECK_MS  # same bar, but the short answer is re-verified after a while
    s = svc.series(PERP, "1d", bars=400)
    assert prov.limits == [401, 401] and len(s.candles) == 400 and not s.gaps
    assert analyze(s, as_of_ms=prov.now_ms)["trend"]["state"] != "UNKNOWN"  # SMA200 available again


def test_partial_response_heals_at_the_next_bar_close_too():
    cs, prov, svc = service(n=700, plan=[50])
    svc.series(PERP, "1d", bars=400)
    next_bar(prov)
    s = svc.series(PERP, "1d", bars=400)
    assert prov.limits == [401, 401] and len(s.candles) == 400 and not s.gaps


def test_incremental_fetch_that_starts_after_a_gap_triggers_one_full_refetch():
    cs, prov, svc = service(n=700)
    assert len(svc.series(PERP, "1d", bars=400).candles) == 400
    next_bar(prov, 6)
    prov.plan = [2]  # the incremental answer misses older rows: fresh data would start after a hole
    s = svc.series(PERP, "1d", bars=400)
    assert prov.limits[1] < 401 and prov.limits[2] == 401 and len(prov.limits) == 3
    assert not s.gaps and s.candles[-1] == [c for c in cs if c.open_time_ms <= prov.now_ms][-2]
    next_bar(prov)
    svc.series(PERP, "1d", bars=400)
    assert len(prov.limits) == 4 and prov.limits[3] < 401  # healthy again: back to small incremental fetches


def test_a_real_exchange_gap_is_not_refetched_on_every_bar():
    cs, prov, svc = service(n=700, drop=range(600, 603))  # the exchange itself has no rows there
    s = svc.series(PERP, "1d", bars=400)
    assert len(s.gaps) == 1
    for _ in range(3):
        next_bar(prov)
        s = svc.series(PERP, "1d", bars=400)
        assert len(s.gaps) == 1
    assert prov.limits[0] == 401 and all(x < 401 for x in prov.limits[1:]) and len(prov.limits) == 4


def test_cache_holds_the_largest_allowed_watchlist_on_every_timeframe():
    fields = UserSettings.model_fields
    max_symbols = next(m.max_length for m in fields["watchlist"].metadata if hasattr(m, "max_length"))
    assert MAX_CACHE_KEYS >= max_symbols * len(Timeframe.all_codes())
    assert MAX_CACHE_KEYS <= 2 * max_symbols * len(Timeframe.all_codes())  # still bounded


def test_four_hundred_subscriptions_are_served_from_cache_on_the_second_pass():
    cs = candles_from_closes([1.0 + 0.001 * i for i in range(60)])
    prov = Provider(cs, cs[-1].open_time_ms + DAY_MS // 2)
    svc = MarketDataService(prov, clock=ServerClock(prov, local=lambda: prov.now_ms / 1000))
    # 50 symbols x 8 timeframes (the settings maximum). The fake history is daily, so 8 distinct instruments
    # per symbol stand in for the 8 timeframe keys: same number of cache keys.
    insts = [InstrumentIdentity("binance", MarketType.USDM_PERPETUAL, f"S{i:02d}USDT{j}", "USDT")
             for i in range(50) for j in range(8)]
    for _ in range(2):
        for inst in insts:
            svc.series(inst, "1d", bars=50)
    assert len(prov.limits) == 400  # the second pass refetched nothing (the old 256-key cache refetched all)


def test_a_hole_left_by_a_partial_retry_is_rechecked_later_instead_of_kept_forever():
    from my_ai_trading.market import GAP_RECHECK_BARS
    cs, prov, svc = service(n=700)
    svc.series(PERP, "1d", bars=400)
    next_bar(prov, 6)
    klines, glitches = prov.klines, [3]

    def glitchy(instrument, timeframe, **kw):  # full-length answers with rows missing in the middle
        rows = klines(instrument, timeframe, **kw)
        if glitches[0]:
            glitches[0] -= 1
            return rows[:-5] + rows[-2:]
        return rows
    prov.klines = glitchy
    assert len(svc.series(PERP, "1d", bars=400).gaps) == 1  # the incremental answer and its full retry
    next_bar(prov)
    assert len(svc.series(PERP, "1d", bars=400).gaps) == 1  # and the retry at the next bar: kept as a real gap
    next_bar(prov, GAP_RECHECK_BARS)
    assert not svc.series(PERP, "1d", bars=400).gaps  # re-audit DATA-5: the hole was never re-checked
