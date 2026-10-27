"""Six-breaker remediation: FLOW-9 (concurrent strategy revisions) and PERF-3 (alert retention)."""
import threading
import time

from my_ai_trading import store as S
from my_ai_trading.compiler import compile_prompts
from my_ai_trading.store import Store


def _rule(n):
    return compile_prompts([f"close is above SMA {n} on the 1h"]).rule


def test_concurrent_revisions_get_distinct_versions_instead_of_a_server_error(monkeypatch):
    st = Store()
    insert = Store._insert_version

    def slow_insert(self, *args):
        time.sleep(0.02)  # widen the window between reading the latest version and inserting the next
        insert(self, *args)
    monkeypatch.setattr(Store, "_insert_version", slow_insert)
    sid, _ = st.create_strategy(_rule(10), ["x"])
    errors, versions = [], []
    barrier = threading.Barrier(8)

    def revise(n):
        barrier.wait()
        try:
            versions.append(st.revise_strategy(sid, _rule(n), ["x"]))
        except Exception as exc:  # FLOW-9: sqlite3.IntegrityError -> HTTP 500
            errors.append(exc)
    threads = [threading.Thread(target=revise, args=(20 + i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and sorted(versions) == list(range(2, 10))


def _alert(st, i):
    return st.record_alert(f"e{i}", user="local", instrument_key="k", timeframe="1h", source="s", event_type="t",
                           bar_open_time_ms=i, priority=1, payload={"i": i})


def test_old_alerts_are_pruned_while_cursor_pagination_and_dedup_keep_working(monkeypatch):
    st = Store()
    now = [1_000_000_000_000]
    monkeypatch.setattr(S, "_now", lambda: now[0])
    for i in range(100):
        _alert(st, i)
        now[0] += 3_600_000
    seen = [a["seq"] for a in st.alerts(after_seq=0, limit=500)]
    assert len(seen) == 100
    now[0] += S.ALERT_RETENTION_MS
    assert _alert(st, 100)  # triggers a prune: everything but this one is outside the window
    rows = st.alerts(after_seq=0, limit=500)
    assert [r["event_id"] for r in rows] == ["e100"]
    assert rows[0]["seq"] > max(seen)  # rowids keep growing: a poller at the old cursor sees the new alert
    assert [r["event_id"] for r in st.alerts(after_seq=max(seen), limit=10)] == ["e100"]
    assert not _alert(st, 100)  # dedup still works for a recent event


def test_the_newest_alert_is_kept_so_rowids_never_restart(monkeypatch):
    st = Store()
    now = [1_000_000_000_000]
    monkeypatch.setattr(S, "_now", lambda: now[0])
    _alert(st, 1)
    assert st.prune_alerts(now[0] + 10 * S.ALERT_RETENTION_MS) == 0
    assert len(st.alerts()) == 1
