"""Local SQLite persistence: user settings, versioned strategies, alerts, cooldowns and AI usage.

Every row carries ``user_id`` so the schema is ready for tenancy; V0.2 runs single-user ("local").
Strategy activation is bound to the exact ``rule_hash`` the user previewed (PRD F05).
"""
from __future__ import annotations

import calendar
import json
import os
import sqlite3
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .accounts.analytics import RiskLimits
from .methodology import BY_ID
from .rules import StrategyRule

LOCAL_USER = "local"
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (user_id TEXT PRIMARY KEY, json TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS strategies (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
  parent_methodology TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_versions (
  strategy_id TEXT NOT NULL REFERENCES strategies(id), version INTEGER NOT NULL, rule_json TEXT NOT NULL,
  rule_hash TEXT NOT NULL, prompts_json TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL,
  approved_at INTEGER, PRIMARY KEY (strategy_id, version));
CREATE TABLE IF NOT EXISTS alerts (
  event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, instrument_key TEXT NOT NULL, timeframe TEXT NOT NULL,
  source TEXT NOT NULL, event_type TEXT NOT NULL, bar_open_time_ms INTEGER NOT NULL, priority INTEGER NOT NULL,
  payload_json TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS alerts_user_time ON alerts(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS alerts_created ON alerts(created_at);
CREATE TABLE IF NOT EXISTS cooldowns (key TEXT PRIMARY KEY, last_bar_open_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS monitor_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, feature TEXT NOT NULL, provider TEXT NOT NULL,
  model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL,
  latency_ms INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ai_cache (
  key TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at INTEGER NOT NULL, instrument_key TEXT NOT NULL,
  title TEXT NOT NULL, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS reports_user_time ON reports(user_id, created_at DESC);
-- Exchange connections: metadata ONLY. Secrets live in process memory or the OS vault (accounts/credentials.py).
CREATE TABLE IF NOT EXISTS exchange_connections (
  connection_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, meta_json TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS exchange_connections_user ON exchange_connections(user_id);
-- Testnet order journal. Written BEFORE an order is sent, so a crash mid-send leaves a row to reconcile.
CREATE TABLE IF NOT EXISTS trading_orders (
  client_order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, connection_id TEXT NOT NULL, exchange TEXT NOT NULL,
  environment TEXT NOT NULL, request_json TEXT NOT NULL, state TEXT NOT NULL, order_json TEXT, message TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, lookups INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS trading_orders_user ON trading_orders(user_id, created_at DESC);
"""


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    methodology_id: str
    mode: Literal["NATIVE", "HYBRID", "ADAPTED"] = "NATIVE"


class WatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]{3,20}$")
    market: Literal["usdm_perpetual", "spot"] = "usdm_perpetual"


class UserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_methodologies: list[Selection] = Field(default_factory=list, max_length=20)
    analysis_timeframe: str = "1h"
    timeframes: list[str] = Field(default_factory=lambda: ["1h", "4h", "1d", "1w"], max_length=8)
    watchlist: list[WatchItem] = Field(default_factory=list, max_length=50)
    quiet_hours_utc: tuple[int, int] | None = None
    alerts_paused: bool = False
    monthly_ai_budget_usd: float = Field(default=0.0, ge=0, le=10_000)
    monthly_ai_token_budget: int = Field(default=200_000, ge=0, le=100_000_000)
    deep_analysis_opt_in: bool = False
    llm_data_consent: bool = False
    risk_limits: RiskLimits = Field(default_factory=RiskLimits)

    def model_post_init(self, _: Any) -> None:
        from .domain import Timeframe
        for code in [self.analysis_timeframe, *self.timeframes]:
            Timeframe(code)
        for s in self.selected_methodologies:
            if s.methodology_id not in BY_ID:
                raise ValueError(f"unknown methodology_id {s.methodology_id!r}")
        if self.quiet_hours_utc and not all(0 <= h <= 23 for h in self.quiet_hours_utc):
            raise ValueError("quiet hours must be UTC hours 0..23")


ALERT_RETENTION_MS = 45 * 86_400_000  # longer than a monthly bar can stay the last closed one
ALERT_PRUNE_EVERY_MS = 3_600_000


class SettingsConflict(Exception):
    """The settings changed since the caller read them (HTTP 412)."""


def settings_etag(data: str) -> str:
    import hashlib
    return '"' + hashlib.sha256(data.encode()).hexdigest()[:16] + '"'


def xdg_dir(var: str, default: Path) -> Path:
    """An XDG base directory; empty or relative values are ignored, as the XDG spec requires (ENV-3)."""
    value = os.environ.get(var, "")
    return Path(value) if os.path.isabs(value) else default


def default_db_path() -> Path:
    return xdg_dir("XDG_DATA_HOME", Path.home() / ".local" / "share") / "my-ai-trading" / "app.db"


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self._lock = threading.Lock()
        self._last_prune_ms = 0
        if path != ":memory:":
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if p.is_symlink():  # a dangling link would make SQLite create its target elsewhere (SEC-3)
                raise PermissionError(f"{p} is a symbolic link; refusing to open it")
            if not p.exists():
                os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
            elif p.stat().st_mode & 0o077:
                p.chmod(0o600)  # an existing database readable by others is tightened (SEC-3)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._finalizer = weakref.finalize(self, self._db.close)  # never leak the connection
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._migrate()

    # Columns added after a table was first released: added in place, never by recreating the table.
    _ADDED_COLUMNS = {"trading_orders": (
        ("exchange_order_id", "TEXT"),       # the exchange's own id, once it acknowledged the order
        ("acknowledged", "INTEGER NOT NULL DEFAULT 0"),  # 1 once the exchange has ever confirmed the order
        ("verification", "TEXT"),            # last lookup result: confirmed / not_listed / lookup_failed
        ("last_checked_at", "INTEGER"), ("last_seen_at", "INTEGER"), ("last_miss_at", "INTEGER"),
        ("key_hint", "TEXT"),                # the sending key's hint: a fallback connection must use the same key
    )}

    def _migrate(self) -> None:
        for table, cols in self._ADDED_COLUMNS.items():
            have = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # Rows written before acknowledgement tracking: an order with stored exchange data was acknowledged.
        self._db.execute("UPDATE trading_orders SET acknowledged=1 WHERE order_json IS NOT NULL AND acknowledged=0")

    def close(self) -> None:
        self._finalizer()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # ---- settings ----
    def settings(self, user: str = LOCAL_USER) -> UserSettings:
        rows = self._q("SELECT json FROM settings WHERE user_id=?", (user,))
        return UserSettings.model_validate_json(rows[0]["json"]) if rows else UserSettings()

    def save_settings(self, s: UserSettings, user: str = LOCAL_USER, *, if_match: str | None = None) -> str:
        """Save and return the new version tag. With `if_match`, save only if the stored settings still have that
        tag (compare-and-set under the lock), else raise SettingsConflict: concurrent editors cannot silently undo
        each other (e.g. restore a revoked AI consent)."""
        with self._lock:
            if if_match is not None:
                row = self._db.execute("SELECT json FROM settings WHERE user_id=?", (user,)).fetchone()
                current = settings_etag(row["json"] if row else UserSettings().model_dump_json())
                if current != if_match:
                    raise SettingsConflict("settings changed since you loaded them; reload and apply your change again")
            data = s.model_dump_json()
            self._db.execute("INSERT INTO settings(user_id,json,updated_at) VALUES(?,?,?) "
                             "ON CONFLICT(user_id) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at",
                             (user, data, _now()))
        return settings_etag(data)

    def settings_etag(self, user: str = LOCAL_USER) -> str:
        rows = self._q("SELECT json FROM settings WHERE user_id=?", (user,))
        return settings_etag(rows[0]["json"] if rows else UserSettings().model_dump_json())

    # ---- strategies ----
    def create_strategy(self, rule: StrategyRule, prompts: list[str], *, user: str = LOCAL_USER,
                        kind: Literal["CUSTOM", "ADAPTED"] = "CUSTOM",
                        parent_methodology: str | None = None) -> tuple[str, int]:
        if kind == "ADAPTED" and parent_methodology not in BY_ID:
            raise ValueError("an ADAPTED strategy must name its parent methodology")
        sid = uuid.uuid4().hex[:12]
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute("INSERT INTO strategies VALUES(?,?,?,?,?,?)",
                                 (sid, user, rule.name, kind, parent_methodology, _now()))
                self._insert_version(sid, 1, rule, prompts)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return sid, 1

    def _insert_version(self, sid: str, version: int, rule: StrategyRule, prompts: list[str]) -> None:
        self._db.execute("INSERT INTO strategy_versions VALUES(?,?,?,?,?,?,?,NULL)",
                         (sid, version, rule.canonical_json(), rule.rule_hash(), json.dumps(prompts), "DRAFT", _now()))

    def revise_strategy(self, sid: str, rule: StrategyRule, prompts: list[str], *, user: str = LOCAL_USER) -> int:
        """A materially changed rule becomes a new DRAFT; the active version keeps running until approval."""
        self._owned(sid, user)
        with self._lock:  # read and insert in one transaction, or two revisions race for one number (FLOW-9)
            self._db.execute("BEGIN IMMEDIATE")
            try:
                latest = self._db.execute("SELECT version, rule_hash FROM strategy_versions WHERE strategy_id=? "
                                          "ORDER BY version DESC LIMIT 1", (sid,)).fetchone()
                version = latest["version"] if latest["rule_hash"] == rule.rule_hash() else latest["version"] + 1
                if version != latest["version"]:
                    self._insert_version(sid, version, rule, prompts)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return version

    def approve(self, sid: str, version: int, rule_hash: str, *, user: str = LOCAL_USER) -> None:
        self._owned(sid, user)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT status, rule_hash FROM strategy_versions WHERE strategy_id=? "
                                       "AND version=?", (sid, version)).fetchone()
                if row is None:
                    raise KeyError("version not found")
                if row["rule_hash"] != rule_hash:
                    raise PermissionError("rule_hash does not match the previewed rule; review the new preview")
                if row["status"] != "DRAFT":
                    raise PermissionError(f"version is {row['status']}, not DRAFT")
                self._db.execute("UPDATE strategy_versions SET status='ARCHIVED' WHERE strategy_id=? AND status='ACTIVE'",
                                 (sid,))
                self._db.execute("UPDATE strategy_versions SET status='ACTIVE', approved_at=? WHERE strategy_id=? "
                                 "AND version=?", (_now(), sid, version))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def deactivate(self, sid: str, *, user: str = LOCAL_USER) -> None:
        self._owned(sid, user)
        self._x("UPDATE strategy_versions SET status='ARCHIVED' WHERE strategy_id=? AND status='ACTIVE'", (sid,))

    def _owned(self, sid: str, user: str) -> None:
        if not self._q("SELECT 1 FROM strategies WHERE id=? AND user_id=?", (sid, user)):
            raise KeyError("strategy not found")

    def strategies(self, user: str = LOCAL_USER) -> list[dict[str, Any]]:
        out = []
        for s in self._q("SELECT * FROM strategies WHERE user_id=? ORDER BY created_at", (user,)):
            versions = [{"version": v["version"], "status": v["status"], "rule_hash": v["rule_hash"],
                         "rule": json.loads(v["rule_json"]), "prompts": json.loads(v["prompts_json"]),
                         "approved_at": v["approved_at"]}
                        for v in self._q("SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY version",
                                         (s["id"],))]
            out.append({"id": s["id"], "name": s["name"], "kind": s["kind"],
                        "parent_methodology": s["parent_methodology"], "versions": versions,
                        "label": "Custom strategy" if s["kind"] == "CUSTOM" else
                                 "User-adapted — unvalidated; not the trader's original system"})
        return out

    def active_rules(self, user: str = LOCAL_USER) -> list[tuple[str, int, StrategyRule]]:
        rows = self._q("SELECT v.strategy_id, v.version, v.rule_json FROM strategy_versions v JOIN strategies s "
                       "ON s.id=v.strategy_id WHERE s.user_id=? AND v.status='ACTIVE'", (user,))
        return [(r["strategy_id"], r["version"], StrategyRule.model_validate_json(r["rule_json"])) for r in rows]

    # ---- alerts ----
    def record_alert(self, event_id: str, *, user: str, instrument_key: str, timeframe: str, source: str,
                     event_type: str, bar_open_time_ms: int, priority: int, payload: dict[str, Any]) -> bool:
        """Idempotent: the same event_id is stored once; returns False for a duplicate."""
        now = _now()
        stored = self._x("INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (event_id, user, instrument_key, timeframe, source, event_type, bar_open_time_ms, priority,
                          json.dumps(payload, default=str), now)) == 1
        if stored and now - self._last_prune_ms >= ALERT_PRUNE_EVERY_MS:
            self.prune_alerts(now)
        return stored

    def prune_alerts(self, now_ms: int) -> int:
        """Delete alerts older than the retention window (PERF-3). The window is longer than the longest bar, so
        a deleted event can no longer recur and slip past the event_id dedup. The newest alert is always kept, so
        rowids keep growing and the after_seq cursor never skips or repeats one."""
        with self._lock:
            self._last_prune_ms = now_ms
            return self._db.execute("DELETE FROM alerts WHERE created_at < ? AND rowid < (SELECT MAX(rowid) FROM alerts)",
                                    (now_ms - ALERT_RETENTION_MS,)).rowcount

    def alerts(self, user: str = LOCAL_USER, *, limit: int = 100, after_seq: int = 0) -> list[dict[str, Any]]:
        """after_seq=0: the newest ``limit`` alerts (newest first) for an initial view. after_seq>0: the
        next alerts after that cursor in ascending order, so an incremental poller never skips any."""
        order = "ASC" if after_seq > 0 else "DESC"
        rows = self._q(f"SELECT rowid AS seq, * FROM alerts WHERE user_id=? AND rowid>? ORDER BY rowid {order} LIMIT ?",
                       (user, after_seq, min(max(limit, 1), 500)))
        return [{**{k: r[k] for k in r.keys() if k != "payload_json"}, "payload": json.loads(r["payload_json"])}
                for r in rows]

    def cooldown_last(self, key: str) -> int | None:
        """Bar open time of the last alert for ``key`` (callers count bars with Timeframe.bars_between)."""
        rows = self._q("SELECT last_bar_open_ms FROM cooldowns WHERE key=?", (key,))
        return rows[0]["last_bar_open_ms"] if rows else None

    def mark_cooldown(self, key: str, bar_open_ms: int) -> None:
        self._x("INSERT INTO cooldowns VALUES(?,?) ON CONFLICT(key) DO UPDATE SET "
                "last_bar_open_ms=MAX(last_bar_open_ms, excluded.last_bar_open_ms)", (key, bar_open_ms))

    def get_state(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM monitor_state WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_state(self, key: str, value: str) -> None:
        self._x("INSERT INTO monitor_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))

    # ---- AI usage ledger ----
    def record_ai_usage(self, *, user: str, feature: str, provider: str, model: str, input_tokens: int,
                        output_tokens: int, cost_usd: float | None, latency_ms: int, status: str) -> None:
        self._x("INSERT INTO ai_usage(user_id,feature,provider,model,input_tokens,output_tokens,cost_usd,latency_ms,"
                "status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (user, feature, provider, model, input_tokens, output_tokens, cost_usd, latency_ms, status, _now()))

    # ---- AI response cache (per user; local only) ----
    def cache_get(self, key: str, user: str) -> dict[str, Any] | None:
        rows = self._q("SELECT payload_json FROM ai_cache WHERE key=? AND user_id=? AND expires_at>?", (key, user, _now()))
        return json.loads(rows[0]["payload_json"]) if rows else None

    def cache_put(self, key: str, user: str, payload: dict[str, Any], *, ttl_ms: int) -> None:
        now = _now()
        with self._lock:
            self._db.execute("DELETE FROM ai_cache WHERE expires_at<=?", (now,))
            self._db.execute("INSERT INTO ai_cache VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                             "payload_json=excluded.payload_json, expires_at=excluded.expires_at, "
                             "created_at=excluded.created_at", (key, user, now, now + ttl_ms, json.dumps(payload)))

    # ---- saved analysis reports ----
    def save_report(self, report: dict[str, Any], *, title: str, instrument_key: str, user: str = LOCAL_USER) -> str:
        rid = uuid.uuid4().hex[:12]
        self._x("INSERT INTO reports VALUES(?,?,?,?,?,?)",
                (rid, user, _now(), instrument_key, title[:120], json.dumps(report, default=str)))
        return rid

    def reports(self, user: str = LOCAL_USER, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._q("SELECT id, created_at, instrument_key, title FROM reports WHERE user_id=? "
                       "ORDER BY created_at DESC LIMIT ?", (user, min(max(limit, 1), 200)))
        return [dict(r) for r in rows]

    def report(self, rid: str, user: str = LOCAL_USER) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM reports WHERE id=? AND user_id=?", (rid, user))
        if not rows:
            return None
        r = rows[0]
        return {"id": r["id"], "created_at": r["created_at"], "instrument_key": r["instrument_key"],
                "title": r["title"], "report": json.loads(r["json"])}

    def usage_summary(self, user: str = LOCAL_USER, *, since_ms: int | None = None) -> dict[str, Any]:
        since = since_ms if since_ms is not None else _month_start_ms()
        rows = self._q("SELECT feature, provider, model, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o, "
                       "SUM(cost_usd) c, SUM(cost_usd IS NULL) unknown_cost, AVG(latency_ms) lat FROM ai_usage "
                       "WHERE user_id=? AND created_at>=? GROUP BY feature, provider, model", (user, since))
        items = [dict(r) for r in rows]
        hits = self._q("SELECT COUNT(*) n FROM ai_usage WHERE user_id=? AND created_at>=? AND status='cache_hit'",
                       (user, since))[0]["n"]
        calls = sum(r["n"] for r in items)
        return {"since_ms": since, "by_feature_model": items, "cache_hits": hits,
                "cache_hit_rate": round(hits / calls, 4) if calls else None,
                "total_tokens": sum((r["i"] or 0) + (r["o"] or 0) for r in items),
                "total_cost_usd": round(sum(r["c"] or 0 for r in items), 6),
                "calls_with_unknown_cost": sum(r["unknown_cost"] or 0 for r in items)}

    # ---- exchange connections (metadata only; see accounts/credentials.py) ----
    def save_connection(self, meta: Any) -> None:
        self._x("INSERT INTO exchange_connections VALUES(?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET "
                "meta_json=excluded.meta_json, updated_at=excluded.updated_at",
                (meta.connection_id, meta.user_id, meta.model_dump_json(), _now()))

    def connection(self, connection_id: str, user: str) -> Any:
        from .accounts.credentials import ConnectionMeta
        rows = self._q("SELECT meta_json FROM exchange_connections WHERE connection_id=? AND user_id=?",
                       (connection_id, user))
        return ConnectionMeta.model_validate_json(rows[0]["meta_json"]) if rows else None

    def connections(self, user: str) -> list[Any]:
        from .accounts.credentials import ConnectionMeta
        return [ConnectionMeta.model_validate_json(r["meta_json"]) for r in
                self._q("SELECT meta_json FROM exchange_connections WHERE user_id=? ORDER BY updated_at", (user,))]

    def all_connections(self) -> list[Any]:
        from .accounts.credentials import ConnectionMeta
        return [ConnectionMeta.model_validate_json(r["meta_json"])
                for r in self._q("SELECT meta_json FROM exchange_connections")]

    def delete_connection(self, connection_id: str) -> None:
        self._x("DELETE FROM exchange_connections WHERE connection_id=?", (connection_id,))

    # ---- testnet order journal (accounts/trading.py) ----
    def journal_insert(self, row: dict[str, Any]) -> None:
        """Fails (IntegrityError) if the client order ID already exists: one preview, at most one order."""
        now = _now()
        self._x("INSERT INTO trading_orders (client_order_id, user_id, connection_id, exchange, environment, "
                "request_json, state, order_json, message, created_at, updated_at, lookups, key_hint) "
                "VALUES(?,?,?,?,?,?,?,NULL,NULL,?,?,0,?)",
                (row["client_order_id"], row["user_id"], row["connection_id"], row["exchange"], row["environment"],
                 row["request_json"], row["state"], row.get("created_at", now), now, row.get("key_hint")))

    _TERMINAL = ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "USER_RESOLVED")
    # Exchange order states only move forward. USER_RESOLVED is the user's assertion, so any exchange report wins.
    _RANK = "CASE state WHEN 'NEW' THEN 1 WHEN 'PARTIALLY_FILLED' THEN 2 WHEN 'PENDING_CANCEL' THEN 2 " \
            "WHEN 'FILLED' THEN 3 WHEN 'CANCELED' THEN 3 WHEN 'REJECTED' THEN 3 WHEN 'EXPIRED' THEN 3 ELSE 0 END"
    _RANKS = {"NEW": 1, "PARTIALLY_FILLED": 2, "PENDING_CANCEL": 2, "FILLED": 3, "CANCELED": 3, "REJECTED": 3,
              "EXPIRED": 3}

    def journal_update(self, client_order_id: str, *, state: str, order_json: str | None = None,
                       message: str | None = None, only_from: str | None = None) -> str | None:
        """Set the state, except that a final state is never replaced by a non-final one. The check and the write
        are one statement, so a concurrent cancel and status check cannot reorder them. Returns the stored state."""
        terminal = ",".join("?" * len(self._TERMINAL))
        with self._lock:
            self._db.execute(
                f"UPDATE trading_orders SET state=?, order_json=COALESCE(?, order_json), message=?, updated_at=? "
                f"WHERE client_order_id=? AND (state NOT IN ({terminal}) OR ? IN ({terminal})) AND (? IS NULL OR state=?)",
                (state, order_json, message, _now(), client_order_id, *self._TERMINAL, state, *self._TERMINAL,
                 only_from, only_from))
            row = self._db.execute("SELECT state FROM trading_orders WHERE client_order_id=?",
                                   (client_order_id,)).fetchone()
        return row[0] if row else None

    def journal_seen(self, client_order_id: str, *, state: str, order_json: str, exchange_order_id: str | None,
                     now_ms: int) -> str | None:
        """The exchange confirmed the order: mark it acknowledged and reset misses. The state never moves back (a
        slow, older answer cannot undo a newer one), and an exchange report replaces USER_RESOLVED."""
        rank = self._RANKS.get(state, 0)
        with self._lock:
            self._db.execute(
                f"UPDATE trading_orders SET state=CASE WHEN {self._RANK} > ? THEN state ELSE ? END, "
                f"order_json=CASE WHEN {self._RANK} > ? THEN order_json ELSE ? END, "
                f"exchange_order_id=COALESCE(?, exchange_order_id), acknowledged=1, "
                f"verification='confirmed', last_checked_at=?, last_seen_at=?, lookups=0, message=NULL, updated_at=? "
                f"WHERE client_order_id=?",
                (rank, state, rank, order_json, exchange_order_id, now_ms, now_ms, _now(), client_order_id))
            row = self._db.execute("SELECT state FROM trading_orders WHERE client_order_id=?",
                                   (client_order_id,)).fetchone()
        return row[0] if row else None

    def journal_miss(self, client_order_id: str, *, now_ms: int, started_ms: int, min_spacing_ms: int,
                     verification: str = "not_listed") -> int | None:
        """A lookup did not find the order. Never changes the state. Counts a miss only if the previous counted miss
        was at least `min_spacing_ms` ago, and ignores the result if the order was seen after this lookup started
        (a late "not seen" must not override a newer confirmation). Returns the miss count."""
        with self._lock:
            counted = verification == "not_listed"  # a failed lookup is no evidence either way: not a miss
            spaced = "(? AND (last_miss_at IS NULL OR ?-last_miss_at>=?))"
            self._db.execute(
                "UPDATE trading_orders SET verification=?, last_checked_at=?, "
                f"lookups=CASE WHEN {spaced} THEN lookups+1 ELSE lookups END, "
                f"last_miss_at=CASE WHEN {spaced} THEN ? ELSE last_miss_at END, "
                "updated_at=? WHERE client_order_id=? AND (last_seen_at IS NULL OR last_seen_at<?)",
                (verification, now_ms, counted, now_ms, min_spacing_ms, counted, now_ms, min_spacing_ms, now_ms,
                 _now(), client_order_id, started_ms))
            row = self._db.execute("SELECT lookups FROM trading_orders WHERE client_order_id=?",
                                   (client_order_id,)).fetchone()
        return row[0] if row else None

    def journal_resolve(self, client_order_id: str, *, note: str) -> str | None:
        """The user checked at the exchange and says this order needs no more tracking (user-asserted, final)."""
        with self._lock:
            self._db.execute("UPDATE trading_orders SET state='USER_RESOLVED', message=?, updated_at=? "
                             "WHERE client_order_id=? AND state IN ('SENDING','UNKNOWN','NOT_FOUND') AND acknowledged=0",
                             (note[:300], _now(), client_order_id))
            row = self._db.execute("SELECT state FROM trading_orders WHERE client_order_id=?",
                                   (client_order_id,)).fetchone()
        return row[0] if row else None

    def journal_row(self, client_order_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM trading_orders WHERE client_order_id=?", (client_order_id,))
        return dict(rows[0]) if rows else None

    def journal_open(self, user: str) -> list[dict[str, Any]]:
        """Orders whose outcome is not settled: never acknowledged (SENDING/UNKNOWN, or NOT_FOUND from older versions)
        or acknowledged but not final, including those the exchange no longer lists."""
        return [dict(r) for r in self._q(
            "SELECT * FROM trading_orders WHERE user_id=? AND state NOT IN ('FILLED','CANCELED','REJECTED','EXPIRED',"
            "'USER_RESOLVED') ORDER BY created_at", (user,))]

    def journal(self, user: str, *, limit: int = 100, client_order_id: str | None = None) -> list[dict[str, Any]]:
        if client_order_id is not None:
            rows = self._q("SELECT * FROM trading_orders WHERE user_id=? AND client_order_id=?", (user, client_order_id))
        else:
            rows = self._q("SELECT * FROM trading_orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                           (user, min(max(limit, 1), 500)))
        return [dict(r) for r in rows]


def _now() -> int:
    return int(time.time() * 1000)


def _month_start_ms() -> int:
    t = time.gmtime()
    return calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0)) * 1000
