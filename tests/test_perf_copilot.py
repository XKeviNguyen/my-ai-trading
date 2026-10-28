"""PERF-2: the budget lock is never held across a provider call; reservations keep concurrent calls
inside the budget; an in-flight limit fails fast with a typed error. Fake provider only (no network)."""
import threading
import time

import pytest

from my_ai_trading.copilot import Copilot, CopilotBusyError, LLMResult
from my_ai_trading.store import Store, UserSettings

SNAP = {"as_of_utc": "x", "timeframes": {}}


class SlowLLM:
    """Blocks in complete() until ``release`` is set (or ``delay_s`` passes); records overlap."""
    name, model = "fake", "test-model"

    def __init__(self, *, delay_s=None):
        self.delay_s, self.release = delay_s, threading.Event()
        self.entered = threading.Semaphore(0)
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, *, system, content, effort, max_tokens):
        with self._lock:
            self.calls += 1
        self.entered.release()
        if self.delay_s is not None:
            time.sleep(self.delay_s)
        else:
            assert self.release.wait(10), "test never released the provider"
        return LLMResult("ok", self.model, 1000, 200, "end_turn", 1)


def store_with(**settings):
    s = Store()
    s.save_settings(UserSettings(**settings))
    return s


def run_threads(fn, n):
    out, threads = [None] * n, []
    for i in range(n):
        def target(i=i):
            try:
                out[i] = fn(i)
            except Exception as exc:  # collected and asserted by the caller
                out[i] = exc
        threads.append(threading.Thread(target=target))
    for t in threads:
        t.start()
    return threads, out


def test_concurrent_calls_overlap_instead_of_serializing():
    llm = SlowLLM(delay_s=0.4)
    cp = Copilot(store_with(llm_data_consent=True), llm, max_inflight=4)
    t0 = time.perf_counter()
    threads, out = run_threads(lambda i: cp.ask(f"question {i}", SNAP), 4)
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    assert all(o["route"] == "llm" for o in out) and llm.calls == 4
    assert wall < 1.0, wall  # ~one call (0.4 s); the old lock serialized them to ~1.6 s


def test_in_flight_limit_fails_fast_with_typed_error_and_recovers():
    llm = SlowLLM()
    cp = Copilot(store_with(llm_data_consent=True), llm, max_inflight=2)
    threads, out = run_threads(lambda i: cp.ask(f"question {i}", SNAP), 2)
    for _ in range(2):
        assert llm.entered.acquire(timeout=5)
    t0 = time.perf_counter()
    with pytest.raises(CopilotBusyError):
        cp.ask("one more", SNAP)
    assert time.perf_counter() - t0 < 0.5
    assert not issubclass(CopilotBusyError, ValueError)
    assert cp.ask("", SNAP)["route"] == "deterministic"  # no provider call needed: never busy
    llm.release.set()
    for t in threads:
        t.join()
    assert cp.reserved() == {"tokens": 0, "usd": 0.0}  # busy rejection leaked no reservation
    assert cp.ask("after", SNAP)["route"] == "llm"


def test_concurrent_requests_near_the_limit_never_exceed_the_token_budget(monkeypatch):
    monkeypatch.setenv("MY_AI_TRADING_PRICES", '{"test-model": [4.0, 20.0]}')
    store = store_with(llm_data_consent=True)
    probe_llm = SlowLLM()
    probe = Copilot(store, probe_llm)
    threads, _ = run_threads(lambda i: probe.ask("question 0", SNAP), 1)
    assert probe_llm.entered.acquire(timeout=5)
    one = probe.reserved()["tokens"]  # worst-case reservation of one request of this size
    assert one > 1200 and probe.reserved()["usd"] > 0
    probe_llm.release.set()
    threads[0].join()

    budget = 1200 + 2 * one + one // 2  # room for exactly two more in-flight reservations
    store = store_with(llm_data_consent=True, monthly_ai_token_budget=budget)
    store.record_ai_usage(user="local", feature="seed", provider="fake", model="test-model", input_tokens=1000,
                          output_tokens=200, cost_usd=None, latency_ms=0, status="ok")
    llm = SlowLLM()
    cp = Copilot(store, llm, max_inflight=8)
    threads, out = run_threads(lambda i: cp.ask(f"question {i}", SNAP), 6)
    for _ in range(2):
        assert llm.entered.acquire(timeout=5)
    time.sleep(0.2)  # give the other four every chance to (wrongly) start a call
    assert llm.calls == 2
    assert cp.reserved()["tokens"] == 2 * one
    llm.release.set()
    for t in threads:
        t.join()
    routes = sorted(o["route"] for o in out)
    assert routes == ["deterministic"] * 4 + ["llm"] * 2
    assert all("token budget" in o["fallback_reason"] for o in out if o["route"] == "deterministic")
    assert store.usage_summary()["total_tokens"] == 3 * 1200 <= budget
    # Reconciled: the unused part of each reservation was refunded, so capacity is available again.
    assert cp.reserved() == {"tokens": 0, "usd": 0.0}
    assert cp.ask("question 9", SNAP)["route"] == "llm"


def test_dollar_budget_counts_in_flight_reservations(monkeypatch):
    monkeypatch.setenv("MY_AI_TRADING_PRICES", '{"test-model": [4.0, 20.0]}')
    llm = SlowLLM()
    # One reservation of ~4000 output tokens costs ~0.08 USD; the budget fits one, not two.
    cp = Copilot(store_with(llm_data_consent=True, monthly_ai_budget_usd=0.12), llm, max_inflight=4)
    threads, out = run_threads(lambda i: cp.ask(f"question {i}", SNAP), 1)
    assert llm.entered.acquire(timeout=5)
    second = cp.ask("question 1", SNAP)
    assert second["route"] == "deterministic" and "dollar budget" in second["fallback_reason"]
    llm.release.set()
    threads[0].join()
    assert out[0]["route"] == "llm"


def test_slow_provider_call_does_not_block_budget_reads_or_gated_requests():
    llm = SlowLLM()
    store = store_with(llm_data_consent=True)
    cp = Copilot(store, llm)
    threads, _ = run_threads(lambda i: cp.ask("slow question", SNAP), 1)
    assert llm.entered.acquire(timeout=5)
    t0 = time.perf_counter()
    assert cp.reserved()["tokens"] > 0                       # budget read takes the budget lock
    store.usage_summary()                                    # usage read
    gated = cp.ask("deep question", SNAP, deep=True)         # gate check takes the budget lock too
    elapsed = time.perf_counter() - t0
    assert gated["route"] == "deterministic" and "opting in" in gated["fallback_reason"]
    assert elapsed < 0.1, elapsed  # the old code waited for the in-flight call (up to its 120 s timeout)
    llm.release.set()
    threads[0].join()


def test_provider_error_releases_the_reservation():
    class Boom:
        name, model = "fake", "test-model"

        def complete(self, **kw):
            raise RuntimeError("socket closed")
    cp = Copilot(store_with(llm_data_consent=True), Boom())
    out = cp.ask("q", SNAP)
    assert out["route"] == "deterministic" and out["fallback_reason"] == "AI provider error"
    assert cp.reserved() == {"tokens": 0, "usd": 0.0}


class WorstCaseLLM(SlowLLM):
    """Reports the most a real call can use: every output token, and input at the upper-bound rate."""

    def complete(self, *, system, content, effort, max_tokens):
        self.entered.release()
        assert self.release.wait(10)
        text = len(system.encode()) + sum(len(c.get("text", "").encode()) for c in content)
        return LLMResult("ok", self.model, text // 2, max_tokens, "max_tokens", 1)


def test_worst_case_concurrent_usage_never_exceeds_the_token_budget():
    # Re-audit: the input estimate left out the system prompt, so 16 concurrent asks used 52,000 of 20,000 tokens.
    budget = 40_000
    st = store_with(llm_data_consent=True, monthly_ai_token_budget=budget)
    llm = WorstCaseLLM()
    cp = Copilot(st, llm, max_inflight=16)
    threads, out = run_threads(lambda i: cp.ask(f"question {i} " + "x" * 400, SNAP), 16)
    time.sleep(0.3)
    llm.release.set()
    for t in threads:
        t.join(10)
    assert st.usage_summary()["total_tokens"] <= budget
    assert cp.reserved()["tokens"] == 0
