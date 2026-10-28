"""Offline copilot concurrency benchmark (PERF-2). Fake provider only: no network, no paid calls.

    PYTHONPATH=src python3 tools/bench_copilot.py --concurrent 4 --delay 0.5

Reports the wall time of N concurrent asks against a provider that takes ``delay`` seconds, and the
latency of a request that is refused by a gate (deep analysis without opt-in) while those calls are in
flight. Before PERF-2 the budget lock serialized both behind the provider calls.
"""
from __future__ import annotations

import argparse
import threading
import time

from my_ai_trading.copilot import Copilot, LLMResult
from my_ai_trading.store import Store, UserSettings


class SlowFake:
    name, model = "fake", "bench-model"

    def __init__(self, delay: float):
        self.delay = delay

    def complete(self, *, system, content, effort, max_tokens):
        time.sleep(self.delay)
        return LLMResult("ok", self.model, 1000, 200, "end_turn", int(self.delay * 1000))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrent", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.5)
    args = ap.parse_args()
    store = Store()
    store.save_settings(UserSettings(llm_data_consent=True))
    cp = Copilot(store, SlowFake(args.delay))
    snap = {"as_of_utc": "x", "timeframes": {}}
    done: list[float] = []
    t0 = time.perf_counter()
    threads = [threading.Thread(target=lambda i=i: (cp.ask(f"question {i}", snap),
                                                     done.append(time.perf_counter() - t0)))
               for i in range(args.concurrent)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    g0 = time.perf_counter()
    cp.ask("gated", snap, deep=True)  # refused by the deep opt-in gate; needs only the budget lock
    gated = time.perf_counter() - g0
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    print(f"concurrent={args.concurrent} provider_delay_s={args.delay} wall_s={wall:.3f} "
          f"completions_s={[round(x, 2) for x in sorted(done)]} gated_request_latency_s={gated:.3f}")


if __name__ == "__main__":
    main()
