"""Always-on Auto Analyst (PRD F06, Scenario D/L).

Each (instrument, timeframe) subscription is processed once per newly CLOSED bar of that timeframe.
Custom strategies run on their own ``trigger_timeframe`` and methodologies on their native timeframe,
so a weekly conclusion is never re-issued on every hourly close. Alert IDs are deterministic, and
the store keeps each ID once, so replays, restarts and duplicate polls cannot double-alert.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .domain import AnalysisMode, CandleSeries, InstrumentIdentity, Timeframe
from .engine import analyze
from .market import CLOSE_SAFETY_MS, MarketDataService
from .methodology import BY_ID, assess
from .patterns import recent_patterns
from .providers.binance import MarketDataError, RateLimitedError
from .rules import StrategyRule, evaluate
from .store import LOCAL_USER, Store
from .strategies import EVALUATORS

log = logging.getLogger(__name__)
PATTERN_CAVEAT = ("Geometric pattern detectors also fire on random data (about 6 per 100 bars in tests); "
                  "treat this as a prompt to look, not as evidence.")
PRIORITY = {"PATTERN_CONFIRMED": 1, "BREAKOUT": 2, "BREAKDOWN": 2, "FAILED_BREAKOUT": 2, "FAILED_BREAKDOWN": 2, "STRATEGY_MATCH": 3,
            "METHODOLOGY_STATE": 2, "STALE_DATA": 1}
MAX_BACKOFF_MS = 15 * 60_000
UNRESOLVED_RETRY_MS = 10 * 60_000
MAX_TICK_BACKOFF_S = 60.0  # a failing tick (e.g. first clock sync) retries with backoff up to this (PERF-6)


def event_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:24]


@dataclass
class SubState:
    last_bar_open_ms: int | None = None
    next_due_ms: int = 0
    failures: int = 0
    last_error: str | None = None


@dataclass
class Monitor:
    service: MarketDataService
    store: Store
    user: str = LOCAL_USER
    on_alert: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    rng: random.Random = field(default_factory=random.Random)
    state: dict[tuple[str, str], SubState] = field(default_factory=dict)
    instruments: dict[str, InstrumentIdentity] = field(default_factory=dict)

    unresolved: dict[str, tuple[int, str]] = field(default_factory=dict)  # watch key -> (retry_at_ms, error)
    on_subscriptions: Callable[[set[tuple[str, str, str]]], None] | None = None  # e.g. KlineStreams.set_subscriptions
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _stop: asyncio.Event | None = None  # set by run_forever so tick() can stop between subscriptions
    tick_failures: int = 0

    def wake(self, symbol: str, market: str, timeframe: str, bar_open_ms: int) -> None:
        """A stream saw this bar close: make the subscription due now. Data still comes from REST in tick()."""
        for (key, tf), st in self.state.items():
            if tf == timeframe and key.endswith(f":{market}:{symbol}") and st.failures == 0:
                st.next_due_ms = 0  # never cut short a backoff (e.g. after HTTP 429: an early retry risks an IP ban)
                self._wake.set()

    def subscriptions(self, now_ms: int | None = None, *, settings=None,
                      rules: list[tuple[str, int, StrategyRule]] | None = None) -> list[tuple[InstrumentIdentity, str]]:
        """Watchlist x needed timeframes. A symbol that fails to resolve is skipped (with backoff),
        never allowed to stop monitoring of the others. ``settings`` / ``rules`` reuse a tick's snapshot."""
        s = self.store.settings(self.user) if settings is None else settings
        tfs = set(s.timeframes)
        for _, _, rule in (self.store.active_rules(self.user) if rules is None else rules):
            tfs.update(rule.timeframes())
        for sel in s.selected_methodologies:
            p = BY_ID[sel.methodology_id]
            if p.documented_timeframes:
                tfs.add(p.documented_timeframes[0])
        ordered = sorted(tfs, key=lambda t: Timeframe(t).duration_ms)
        subs = []
        for w in s.watchlist:
            key = f"{w.symbol}:{w.market}"
            if key not in self.instruments:
                retry_at = self.unresolved.get(key, (0, ""))[0]
                if now_ms is not None and now_ms < retry_at:
                    continue
                try:
                    self.instruments[key] = self.service.resolve(w.symbol, w.market)
                    self.unresolved.pop(key, None)
                except (MarketDataError, ValueError) as exc:
                    self.unresolved[key] = ((now_ms or 0) + UNRESOLVED_RETRY_MS, str(exc))
                    log.warning("watchlist symbol %s unresolved: %s", key, exc)
                    continue
            subs.extend((self.instruments[key], tf) for tf in ordered)
        return subs

    def tick_backoff_s(self, idle_s: float) -> float:
        """Delay after ``tick_failures`` consecutive failed ticks: exponential from ``idle_s``, jitter applied
        BEFORE the cap so the delay never exceeds MAX_TICK_BACKOFF_S."""
        base = idle_s * 2 ** min(self.tick_failures, 16)
        return min(MAX_TICK_BACKOFF_S, base * (0.5 + self.rng.random()))

    async def run_forever(self, *, stop: asyncio.Event | None = None, idle_s: float = 1.0) -> None:
        stop = stop or asyncio.Event()
        self._stop = stop
        while not stop.is_set():
            try:
                await self.tick()
                self.tick_failures = 0
            except Exception:  # never let one bad cycle kill the monitor
                self.tick_failures += 1
                if self.tick_failures == 1:
                    log.exception("monitor tick failed")
                else:  # backed off, so this logs at most about once per MAX_TICK_BACKOFF_S
                    log.warning("monitor tick failed again (%d in a row)", self.tick_failures, exc_info=True)
            waits = {asyncio.ensure_future(stop.wait())}
            if self.tick_failures:  # back off: a stream wake-up must not turn this into a tight retry loop
                timeout = self.tick_backoff_s(idle_s)
            else:
                timeout = idle_s
                waits.add(asyncio.ensure_future(self._wake.wait()))
            await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for t in waits:
                t.cancel()
            self._wake.clear()

    def _backoff(self, st: SubState, now: int, error: str, retry_after_ms: int | None = None) -> None:
        st.failures += 1
        st.last_error = error
        if retry_after_ms is not None:
            st.next_due_ms = now + retry_after_ms
        else:
            base = min(MAX_BACKOFF_MS, 5_000 * 2 ** min(st.failures, 10))
            st.next_due_ms = now + int(base * (0.5 + self.rng.random()))  # bounded backoff with jitter

    async def tick(self, *, stop: asyncio.Event | None = None) -> list[dict[str, Any]]:
        """Process every subscription that is due. Returns newly recorded alerts. Settings and active
        rules are read once per tick (PERF-4); a stop request is honored between subscriptions (FLOW-11)."""
        stop = stop or self._stop
        settings = self.store.settings(self.user)
        if settings.alerts_paused:
            return []
        now = await asyncio.to_thread(self.service.clock.now_ms)
        rules = await asyncio.to_thread(self.store.active_rules, self.user)
        subs = await asyncio.to_thread(self.subscriptions, now, settings=settings, rules=rules)
        if self.on_subscriptions:
            self.on_subscriptions({(i.symbol, str(i.market_type), tf) for i, tf in subs})
        new: list[dict[str, Any]] = []
        for inst, tf in subs:
            if stop is not None and stop.is_set():
                break  # remaining subscriptions stay due and run on the next start
            st = self.state.setdefault((inst.key, tf), SubState())
            if now < st.next_due_ms:
                continue
            try:
                series = await asyncio.to_thread(self.service.series, inst, tf, now_ms=now)
            except RateLimitedError as exc:
                self._backoff(st, now, str(exc), int(exc.retry_after_s * 1000))
                continue
            except MarketDataError as exc:
                self._backoff(st, now, str(exc))
                continue
            alerts, complete, stale = await asyncio.to_thread(self._process, inst, tf, series, now, settings, rules)
            if stale:
                self._backoff(st, now, "STALE_DATA: no new closed bar when one was expected")
            elif not complete:
                self._backoff(st, now, st.last_error or "evaluation incomplete")
            else:
                st.failures, st.last_error = 0, None
                last_close = series.candles[-1].close_time_ms if series.candles else now
                next_close = Timeframe(tf).next_bar_close_ms(last_close) + 1
                st.next_due_ms = max(now + 1_000, next_close + CLOSE_SAFETY_MS + 500)
            for alert in alerts:  # deliver per subscription, so a later failure cannot swallow them
                if self.on_alert:
                    res = self.on_alert(alert)
                    if asyncio.iscoroutine(res):
                        await res
            new.extend(alerts)
        return new

    def _process(self, inst: InstrumentIdentity, tf: str, series: CandleSeries, now: int, settings,
                 rules: list[tuple[str, int, StrategyRule]] | None = None) -> tuple[list[dict[str, Any]], bool, bool]:
        """Evaluate one newly closed bar. Returns (alerts, complete, stale). The bar is marked as
        processed only when every engine/strategy/methodology check for it ran; otherwise it is retried
        (alert ids are idempotent, so a retry cannot duplicate anything already recorded)."""
        st = self.state[(inst.key, tf)]
        out: list[dict[str, Any]] = []
        quiet = _in_quiet_hours(now, settings.quiet_hours_utc)
        if series.is_stale(now):
            bar = series.candles[-1].open_time_ms if series.candles else 0
            out += self._alert(inst, tf, "system", "STALE_DATA", bar, quiet,
                               {"message": f"No new closed {tf} bar when one was expected; analysis paused for "
                                           f"this timeframe until data resumes.", "last_bar_open_ms": bar})
            return out, False, True
        last = series.candles[-1] if series.candles else None
        if last is None or last.open_time_ms == st.last_bar_open_ms:
            return out, True, False  # no new closed bar: nothing to re-evaluate
        report = analyze(series, as_of_ms=now)
        for ev in report.get("events", []):
            if ev["type"] in PRIORITY:
                out += self._alert(inst, tf, "engine", ev["type"], last.open_time_ms, quiet,
                                   {"event": ev, "trend": report.get("trend"), "rule_version": ev["rule_version"]})

        for d in recent_patterns(series, bars=1):  # only patterns resolved on THIS bar
            if d["state"] in ("BREAKOUT_CONFIRMED", "BREAKDOWN_CONFIRMED") and d["end_bar_open_ms"] == last.open_time_ms:
                out += self._alert(inst, tf, "pattern", "PATTERN_CONFIRMED", last.open_time_ms, quiet,
                                   {"pattern": d, "rule_version": d["rule_version"], "caveat": PATTERN_CAVEAT})

        errors: list[str] = []
        cache: dict[str, CandleSeries] = {tf: series}

        def get(code: str) -> CandleSeries:
            if code not in cache:
                cache[code] = self.service.series(inst, code, now_ms=now)
            return cache[code]

        for sid, version, rule in (self.store.active_rules(self.user) if rules is None else rules):
            if rule.trigger_timeframe != tf:
                continue
            try:
                res = evaluate(rule, get, as_of_ms=now)
            except (MarketDataError, ValueError) as exc:
                errors.append(f"strategy {sid}: {exc}")
                continue
            if res["result"] != "MATCH":
                continue
            key = f"{self.user}|{inst.key}|{sid}"
            prev_bar = self.store.cooldown_last(key)  # read only for a match (PERF-4)
            cool = prev_bar is None or Timeframe(tf).bars_between(prev_bar, last.open_time_ms) >= rule.cooldown_bars
            if cool:
                created = self._alert(inst, tf, f"strategy:{sid}:v{version}", "STRATEGY_MATCH", last.open_time_ms,
                                      quiet, {"strategy_id": sid, "version": version, "name": rule.name,
                                              "evaluation": res, "label": "Custom strategy"})
                if created:
                    self.store.mark_cooldown(key, last.open_time_ms)
                out += created

        for sel in settings.selected_methodologies:
            p = BY_ID[sel.methodology_id]
            a = assess(p, inst, settings.analysis_timeframe, AnalysisMode(sel.mode))
            if not a.evaluate or a.evaluation_timeframe != tf:
                continue  # evaluated only on its own native close cadence
            try:
                ev = EVALUATORS[p.evaluator](series)
            except ValueError as exc:
                errors.append(f"methodology {p.methodology_id}: {exc}")
                continue
            skey = f"mstate|{self.user}|{inst.key}|{p.methodology_id}"
            prev = self.store.get_state(skey)
            if ev.get("state") and ev["state"] != prev:
                self.store.set_state(skey, ev["state"])
                if prev is not None:  # first observation sets the baseline silently
                    out += self._alert(inst, tf, f"methodology:{p.methodology_id}", "METHODOLOGY_STATE",
                                       last.open_time_ms, quiet,
                                       {"methodology_id": p.methodology_id, "from": prev, "to": ev["state"],
                                        "label": a.label, "evaluation": ev})
        if errors:
            st.last_error = "; ".join(errors)[:500]
            return out, False, False
        st.last_bar_open_ms = last.open_time_ms
        return out, True, False

    def _alert(self, inst, tf, source, kind, bar_open_ms, quiet, payload) -> list[dict[str, Any]]:
        eid = event_id(self.user, inst.key, tf, source, kind, bar_open_ms,
                       payload.get("rule_version", payload.get("version", "")), payload.get("to", ""))
        alert = {"event_id": eid, "instrument_key": inst.key, "symbol": inst.symbol, "timeframe": tf,
                 "source": source, "event_type": kind, "bar_open_time_ms": bar_open_ms,
                 "priority": PRIORITY.get(kind, 1), "quiet": quiet, "payload": payload}
        stored = self.store.record_alert(eid, user=self.user, instrument_key=inst.key, timeframe=tf, source=source,
                                         event_type=kind, bar_open_time_ms=bar_open_ms, priority=alert["priority"],
                                         payload={**payload, "quiet": quiet})
        return [alert] if stored else []

    def status(self) -> list[dict[str, Any]]:
        rows = [{"subscription": f"{k[0]}|{k[1]}", "last_bar_open_ms": v.last_bar_open_ms,
                 "next_due_ms": v.next_due_ms, "failures": v.failures, "last_error": v.last_error}
                for k, v in list(self.state.items())]  # snapshot: tick() may add keys concurrently
        rows += [{"subscription": key, "status": "UNRESOLVED", "retry_at_ms": at, "last_error": err}
                 for key, (at, err) in list(self.unresolved.items())]
        return rows


def _in_quiet_hours(now_ms: int, window: tuple[int, int] | None) -> bool:
    if not window:
        return False
    hour = (now_ms // 3_600_000) % 24
    start, end = window
    return start <= hour < end if start <= end else hour >= start or hour < end


def dumps(alert: dict[str, Any]) -> str:
    return json.dumps(alert, default=str)
