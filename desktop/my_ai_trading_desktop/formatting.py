"""Pure formatting and validation helpers (no Qt). All output is plain text."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import IMAGE_TYPES, MAX_IMAGE_BYTES, SYMBOL_RE

DASH = "—"
MODES = ("NATIVE", "HYBRID", "ADAPTED")
# Exact user-visible labels from PRD F13.
MODE_HINT = {
    "NATIVE": "Methodology-inspired / native scope",
    "HYBRID": "Hybrid: native context + user timing",
    "ADAPTED": "User-adapted — unvalidated; not the trader's original system",
}
NAME_RE = re.compile(r"^[\w .,:()+/-]{1,80}$")
MAX_PROMPTS = 10
MAX_WATCHLIST = 50
MAX_NOTIFICATIONS_PER_POLL = 5


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def fmt_num(v: Any, digits: int = 6) -> str:
    if not _is_num(v):
        return DASH
    a = abs(v)
    if a != 0 and (a >= 1e9 or a < 1e-6):
        return f"{v:.3e}"
    digits = max(1, min(digits, 12))
    out = f"{float(v):.{digits}g}"
    if "e" in out:  # %g switched to exponent; fall back to a fixed rendering
        out = f"{float(v):.{digits}f}".rstrip("0").rstrip(".")
    return out


def fmt_int(v: Any) -> str:
    if not _is_num(v):
        return DASH
    return f"{round(v):,}"


def fmt_usd(v: Any) -> str:
    if not _is_num(v):
        return "unknown"
    return f"${v:.6f}" if 0 < v < 0.01 else f"${v:.2f}"


def fmt_ms(ms: Any) -> str:
    """Milliseconds since epoch → '2026-10-26 13:00 UTC'."""
    if not _is_num(ms) or ms <= 0:
        return DASH
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return DASH


def _parse_time_ms(v: Any) -> float | None:
    if _is_num(v):
        return float(v)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp() * 1000
    return None


def fmt_age(iso_or_ms: Any, now_ms: float) -> str:
    """Short human age such as '3 min ago' relative to now_ms."""
    t = _parse_time_ms(iso_or_ms)
    if t is None:
        return ""
    s = round((now_ms - t) / 1000)
    if s < 0:
        return "in the future"
    if s < 90:
        return f"{s} s ago"
    if s < 5400:
        return f"{round(s / 60)} min ago"
    if s < 172800:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} d ago"


def fmt_json(v: Any, max_len: int = 200) -> str:
    if isinstance(v, str):
        s = v
    elif isinstance(v, bool):
        s = "true" if v else "false"
    elif _is_num(v):
        s = fmt_num(v)
    elif v is None:
        s = DASH
    else:
        try:
            s = json.dumps(v, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            s = str(v)
    return s if len(s) <= max_len else s[:max_len] + "…"


def text(v: Any, default: str = DASH) -> str:
    """Coerce an untrusted API value into display text."""
    if v is None or v == "":
        return default
    return v if isinstance(v, str) else fmt_json(v)


def str_list(v: Any) -> list[str]:
    return [text(x, "") for x in v] if isinstance(v, list) else []


def market_label(market: str | None) -> str:
    return "Spot" if market == "spot" else "USD-M perpetual"


def normalize_symbol(raw: str) -> str | None:
    s = re.sub(r"[\s/_-]", "", (raw or "").strip().upper())
    return s if SYMBOL_RE.fullmatch(s) else None


def symbol_from_key(key: str) -> str:
    parts = (key or "").split(":")
    return parts[-1] or key


def pct_of_budget(used: Any, budget: Any) -> str:
    if not _is_num(budget) or budget == 0:
        return "no limit set"
    u = used if _is_num(used) else 0
    return f"{round(u / budget * 100)}% of budget"


# ---------------------------------------------------------------- strategy studio
def split_prompts(raw: str) -> list[str]:
    """One prompt per non-empty line."""
    return [s.strip() for s in (raw or "").splitlines() if s.strip()]


def validate_compile_input(prompts: list[str], name: str) -> str | None:
    if not 1 <= len(prompts) <= MAX_PROMPTS:
        return "Enter 1 to 10 prompts (one per line)."
    if any(len(p) > 4000 for p in prompts):
        return "Each prompt must be at most 4000 characters."
    if not NAME_RE.fullmatch(name):
        return "Name contains unsupported characters."
    return None


def preview_rows(p: dict[str, Any] | None) -> list[tuple[str, str]]:
    if not isinstance(p, dict):
        return []
    cooldown = p.get("cooldown_bars")
    return [
        ("Name", text(p.get("name"))),
        ("When", text(p.get("when"))),
        ("Condition", text(p.get("condition"))),
        ("Then", text(p.get("then"))),
        ("Timeframes used", ", ".join(str_list(p.get("timeframes_used"))) or DASH),
        ("Cooldown bars", DASH if cooldown is None else text(cooldown)),
        ("Bar finality", text(p.get("bar_finality"))),
        ("Attribution", text(p.get("attribution"))),
        ("rule_hash", text(p.get("rule_hash"))),
    ]


# ---------------------------------------------------------------- methods
def build_selections(previous: list[dict[str, Any]], catalog_order: list[str],
                     enabled: dict[str, str]) -> list[dict[str, str]]:
    """Keep the saved order, then append newly enabled profiles in catalog order."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for s in previous:
        mid = s.get("methodology_id") if isinstance(s, dict) else None
        if mid in enabled and mid not in seen:
            out.append({"methodology_id": mid, "mode": enabled[mid]})
            seen.add(mid)
    for mid in catalog_order:
        if mid in enabled and mid not in seen:
            out.append({"methodology_id": mid, "mode": enabled[mid]})
            seen.add(mid)
    return out


# ---------------------------------------------------------------- settings
def clean_watchlist(rows: list[tuple[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    items: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw_symbol, raw_market in rows:
        if not raw_symbol.strip():
            continue
        sym = normalize_symbol(raw_symbol)
        if not sym:
            errors.append(f'"{raw_symbol[:30]}" is not a valid symbol')
            continue
        market = "spot" if raw_market == "spot" else "usdm_perpetual"
        if (sym, market) not in seen:
            seen.add((sym, market))
            items.append({"symbol": sym, "market": market})
    if len(items) > MAX_WATCHLIST:
        errors.append("At most 50 watchlist entries.")
    return items, errors


def check_image(filename: str, size: int) -> tuple[str | None, str | None]:
    """Return (content_type, None) or (None, problem). The server re-validates magic bytes."""
    lower = (filename or "").lower()
    ctype = next((t for t, exts in IMAGE_TYPES.items() if lower.endswith(exts)), None)
    if ctype is None:
        return None, "Only PNG, JPEG or WEBP screenshots are accepted."
    if size <= 0:
        return None, "The file is empty."
    if size > MAX_IMAGE_BYTES:
        return None, "Image is larger than 5 MB."
    return ctype, None


# ---------------------------------------------------------------- alerts
def is_quiet(alert: dict[str, Any]) -> bool:
    p = alert.get("payload")
    return isinstance(p, dict) and p.get("quiet") is True


def alert_summary(alert: dict[str, Any]) -> str:
    p = alert.get("payload") if isinstance(alert.get("payload"), dict) else {}

    def s(v: Any) -> str:
        return v if isinstance(v, str) else ""
    et = alert.get("event_type")
    if et == "STRATEGY_MATCH":
        return f'Strategy "{s(p.get("name"))}" v{fmt_json(p.get("version"))} matched (custom strategy, closed bar)'
    if et == "METHODOLOGY_STATE":
        return f'{s(p.get("methodology_id"))}: {s(p.get("from"))} → {s(p.get("to"))} ({s(p.get("label"))})'
    if et == "STALE_DATA":
        return s(p.get("message")) or "Data is stale"
    ev = p.get("event")
    if isinstance(ev, dict):
        inv = s(ev.get("invalidation"))
        return f"Invalidated by {inv}" if inv else s(ev.get("type"))
    return ""


_NOTIFY_STRIP = re.compile(r"[<>&\x00-\x1f\x7f]")


def notification_safe(raw: str, max_len: int) -> str:
    """Notification servers may interpret markup in the body: drop markup characters and controls."""
    return _NOTIFY_STRIP.sub(" ", raw)[:max_len]


def notification_text(alert: dict[str, Any]) -> tuple[str, str]:
    sym = symbol_from_key(text(alert.get("instrument_key"), ""))
    title = f"{sym} {text(alert.get('timeframe'), '')} · {text(alert.get('event_type'), '')}"
    body = f"{alert_summary(alert)} — bar {fmt_ms(alert.get('bar_open_time_ms'))} open"
    return notification_safe(title, 80), notification_safe(body, 200)


def merge_alerts(existing: list[dict[str, Any]], incoming: list[dict[str, Any]],
                 max_items: int = 200) -> list[dict[str, Any]]:
    by_seq: dict[int, dict[str, Any]] = {}
    for a in [*existing, *incoming]:
        if isinstance(a, dict) and isinstance(a.get("seq"), int):
            by_seq[a["seq"]] = a
    return [by_seq[k] for k in sorted(by_seq, reverse=True)][:max_items]


@dataclass
class AlertFeed:
    """Client-side alert cursor. The first successful poll only sets a baseline (no notifications)."""

    last_seq: int = 0
    baseline_done: bool = False
    alerts: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        """Merge a /alerts response and return the alerts that deserve a desktop notification."""
        rows = [a for a in response.get("alerts", []) if isinstance(a, dict) and isinstance(a.get("seq"), int)]
        fresh = [a for a in rows if a["seq"] > self.last_seq]
        self.alerts = merge_alerts(self.alerts, fresh)
        nxt = response.get("next_after_seq")
        candidates = [self.last_seq, *(a["seq"] for a in fresh)]
        if isinstance(nxt, int) and not isinstance(nxt, bool):
            candidates.append(nxt)
        self.last_seq = max(candidates)
        first = not self.baseline_done
        self.baseline_done = True
        if first:
            return []
        loud = sorted((a for a in fresh if not is_quiet(a)), key=lambda a: a["seq"])
        return loud[:MAX_NOTIFICATIONS_PER_POLL]

    def reset(self) -> None:
        self.last_seq, self.baseline_done, self.alerts = 0, False, []


# ---------------------------------------------------------------- analysis
def timeframe_rows(r: dict[str, Any], now_ms: float) -> list[tuple[str, str]]:
    iv = r.get("indicator_values") if isinstance(r.get("indicator_values"), dict) else {}
    bc = r.get("bar_close_time_utc")
    age = fmt_age(bc, now_ms)
    return [
        ("Bar close", f"{text(bc)}" + (f" ({age})" if bc and age else "")),
        ("Close", fmt_num(r.get("last_close"))),
        ("SMA50", fmt_num(iv.get("sma50"))),
        ("SMA200", fmt_num(iv.get("sma200"))),
        ("RSI14 (Wilder)", fmt_num(iv.get("rsi14_wilder"), 4)),
        ("Volume ratio (20)", fmt_num(iv.get("volume_ratio_20"), 4)),
    ]


def levels_text(levels: Any) -> str:
    if not isinstance(levels, dict) or not levels:
        return "No levels reported."
    return " · ".join(f"{k.replace('_', ' ')} {fmt_num(v)}" for k, v in levels.items())


def event_lines(events: Any) -> list[str]:
    """One line per event: type, bar close and invalidation (when present)."""
    out = []
    for e in events if isinstance(events, list) else []:
        if not isinstance(e, dict):
            continue
        line = text(e.get("type"), "event")
        if e.get("bar_close_time_utc"):
            line += f" · bar closed {text(e.get('bar_close_time_utc'))}"
        if e.get("invalidation"):
            line += f"\n    Invalidation: {text(e.get('invalidation'))}"
        out.append(line)
    return out


def alignment_lines(a: Any) -> list[str]:
    if not isinstance(a, dict):
        return ["No alignment data."]
    lines = [f"Alignment: {text(a.get('verdict'), 'UNKNOWN')}"]
    tbt = a.get("trend_by_timeframe")
    if isinstance(tbt, dict) and tbt:
        lines.append(" · ".join(f"{k}: {text(v)}" for k, v in tbt.items()))
    if a.get("note"):
        lines.append(text(a.get("note")))
    return lines


def model_meta(m: dict[str, Any]) -> str:
    meta = []
    if m.get("model"):
        meta.append(f"model {text(m.get('model'))}")
    if _is_num(m.get("input_tokens")) or _is_num(m.get("output_tokens")):
        meta.append(f"tokens in {fmt_int(m.get('input_tokens'))} / out {fmt_int(m.get('output_tokens'))}")
    meta.append(f"est. cost {fmt_usd(m.get('estimated_cost_usd'))}")
    if _is_num(m.get("latency_ms")):
        meta.append(f"{fmt_int(m.get('latency_ms'))} ms")
    return " · ".join(meta)


def fallback_text(route: Any, reason: Any) -> str | None:
    if not reason:
        return None
    return f"Route: {text(route, 'deterministic')}. AI interpretation not used: {text(reason)}"


# ---------------------------------------------------------------- usage / reports
def fmt_rate(v: Any) -> str:
    """0.25 → '25.0%'; missing → 'n/a (no AI calls)'."""
    if not _is_num(v):
        return "n/a (no AI calls)"
    return f"{v * 100:.1f}%"


def usage_rows(u: dict[str, Any]) -> list[tuple[str, str]]:
    tok_budget, usd_budget = u.get("monthly_ai_token_budget"), u.get("monthly_ai_budget_usd")
    usd_cap = fmt_usd(usd_budget) if _is_num(usd_budget) and usd_budget else "no USD cap"
    return [
        ("Tokens", f"{fmt_int(u.get('total_tokens'))} / {fmt_int(tok_budget)} "
                   f"({pct_of_budget(u.get('total_tokens'), tok_budget)})"),
        ("Estimated cost", f"{fmt_usd(u.get('total_cost_usd'))} / {usd_cap} "
                           f"({pct_of_budget(u.get('total_cost_usd'), usd_budget)})"),
        ("Calls with unknown cost", fmt_int(u.get("calls_with_unknown_cost"))),
        ("Cache hits", fmt_int(u.get("cache_hits")) if u.get("cache_hits") is not None else DASH),
        ("Cache hit rate", fmt_rate(u.get("cache_hit_rate"))),
    ]


def usage_table_row(r: dict[str, Any]) -> list[str]:
    unknown = r.get("unknown_cost")
    cost = fmt_usd(r.get("c") or 0)
    return [
        text(r.get("feature")),
        f"{text(r.get('provider'))}/{text(r.get('model'))}",
        fmt_int(r.get("n")),
        fmt_int(r.get("i") or 0),
        fmt_int(r.get("o") or 0),
        f"{cost} + {fmt_int(unknown)} unknown" if _is_num(unknown) and unknown else cost,
        fmt_int(r.get("lat")),
    ]


def report_filename(report_id: str) -> str:
    safe = re.sub(r"[^0-9a-f]", "", report_id or "")[:12] or "report"
    return f"report-{safe}.md"
