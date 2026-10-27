"""Exportable analysis reports (PRD F11): the same facts the API returns, as readable Markdown."""
from __future__ import annotations

from typing import Any

DISCLAIMER = ("Research observations from closed candles only. Not financial advice, not a trade instruction, "
              "and not evidence of a trading edge.")


def _md(text: Any) -> str:
    """Escape characters that would change Markdown table or emphasis structure."""
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("*", "\\*").replace("_", "\\_") \
        .replace("\n", " ")


def to_markdown(saved: dict[str, Any]) -> str:
    r = saved["report"]
    snap, persp = r["snapshot"], r["perspectives"]
    inst = snap["instrument"]
    lines = [f"# {_md(saved['title'])}", "",
             f"**Instrument:** {_md(inst['symbol'])} ({_md(inst['venue'])}, {_md(inst['market_type'])})  ",
             f"**Data as of:** {_md(snap['as_of_utc'])}  ",
             f"**Analysis timeframe:** {_md(persp['analysis_timeframe'])}", "",
             "## Timeframes", "",
             "| Timeframe | Status | Last closed bar | Close | Trend | SMA50 | SMA200 | RSI14 | Events |",
             "|---|---|---|---|---|---|---|---|---|"]
    for tf, t in snap["timeframes"].items():
        iv = t.get("indicator_values", {})
        events = ", ".join(e["type"] for e in t.get("events", [])) or "none"
        lines.append(f"| {_md(tf)} | {_md(t.get('status'))} | {_md(t.get('bar_close_time_utc', '-'))} | "
                     f"{_md(t.get('last_close', '-'))} | {_md(t.get('trend', {}).get('state', '-'))} | "
                     f"{_md(iv.get('sma50'))} | {_md(iv.get('sma200'))} | {_md(iv.get('rsi14_wilder'))} | {_md(events)} |")
    al = snap.get("alignment", {})
    lines += ["", f"**Alignment:** {_md(al.get('verdict'))}. {_md(al.get('note', ''))}", ""]
    invalidations = [(tf, e) for tf, t in snap["timeframes"].items() for e in t.get("events", [])]
    if invalidations:
        lines += ["## Events and what would invalidate them", ""]
        lines += [f"- **{_md(tf)} {_md(e['type'])}**: invalidated by {_md(e['invalidation'])}" for tf, e in invalidations]
        lines.append("")
    lines += ["## Methodology perspectives", ""]
    if not persp["perspectives"]:
        lines.append("No methodology profiles selected.")
    for p in persp["perspectives"]:
        a = p["assessment"]
        ev = p.get("evaluation", {})
        lines.append(f"- **{_md(a['name'])}** — {_md(a['label'])}; applicability {_md(a['status'])}; "
                     f"evaluated on {_md(a['evaluation_timeframe'] or '-')}; state {_md(ev.get('state', '-'))}")
        lines += [f"  - {_md(reason)}" for reason in a.get("reasons", [])]
    lines += ["", f"_{_md(persp['aggregation'])}_", "", "---", "", DISCLAIMER, ""]
    return "\n".join(lines)
