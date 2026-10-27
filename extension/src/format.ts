// Pure formatting helpers (unit-tested). Output is plain text, inserted with textContent.

import type { Alert, Json } from "./types.js";

export function fmtNum(v: unknown, digits = 6): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs !== 0 && (abs >= 1e9 || abs < 1e-6)) return v.toExponential(3);
  return Number(v.toPrecision(Math.max(1, Math.min(digits, 12)))).toString();
}

export function fmtInt(v: unknown): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return Math.round(v).toLocaleString("en-US");
}

export function fmtUsd(v: unknown): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "unknown";
  return v < 0.01 && v > 0 ? `$${v.toFixed(6)}` : `$${v.toFixed(2)}`;
}

/** Milliseconds since epoch → "2026-10-26 13:00 UTC". */
export function fmtMs(ms: unknown): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms <= 0) return "—";
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

/** Short human age such as "3 min ago" relative to `nowMs`. */
export function fmtAge(isoOrMs: unknown, nowMs: number): string {
  const t = typeof isoOrMs === "number" ? isoOrMs : typeof isoOrMs === "string" ? Date.parse(isoOrMs) : NaN;
  if (!Number.isFinite(t)) return "";
  const s = Math.round((nowMs - t) / 1000);
  if (s < 0) return "in the future";
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** Compact one-line rendering of a JSON value (for checks / leaves). */
export function fmtJson(v: Json | unknown, max = 200): string {
  let s: string;
  if (typeof v === "string") s = v;
  else if (typeof v === "number") s = fmtNum(v);
  else if (v === null || v === undefined) s = "—";
  else {
    try {
      s = JSON.stringify(v);
    } catch {
      s = String(v);
    }
  }
  return s.length > max ? s.slice(0, max) + "…" : s;
}

export function symbolFromKey(key: string): string {
  const parts = key.split(":");
  return parts[parts.length - 1] || key;
}

export function isQuiet(alert: Pick<Alert, "payload">): boolean {
  return alert.payload?.["quiet"] === true;
}

/** One-line summary of an alert payload, built only from known fields. */
export function alertSummary(alert: Alert): string {
  const p = alert.payload ?? {};
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  if (alert.event_type === "STRATEGY_MATCH") {
    return `Strategy "${str(p["name"])}" v${fmtJson(p["version"])} matched (custom strategy, closed bar)`;
  }
  if (alert.event_type === "METHODOLOGY_STATE") {
    return `${str(p["methodology_id"])}: ${str(p["from"])} → ${str(p["to"])} (${str(p["label"])})`;
  }
  if (alert.event_type === "STALE_DATA") return str(p["message"]) || "Data is stale";
  const ev = p["event"];
  if (ev && typeof ev === "object" && !Array.isArray(ev)) {
    const inv = str((ev as Record<string, Json>)["invalidation"]);
    return inv ? `Invalidated by ${inv}` : str((ev as Record<string, Json>)["type"]);
  }
  return "";
}

/** Plain-text notification title/body (no HTML), length-bounded. */
export function notificationText(alert: Alert): { title: string; message: string } {
  const sym = symbolFromKey(alert.instrument_key);
  const title = `${sym} ${alert.timeframe} · ${alert.event_type}`.slice(0, 80);
  const message = (`${alertSummary(alert)} — bar ${fmtMs(alert.bar_open_time_ms)} open`).slice(0, 200);
  return { title, message };
}

/** Merge newly fetched alerts into the cache: dedupe by seq, newest first, bounded. */
export function mergeAlerts(existing: Alert[], incoming: Alert[], max = 200): Alert[] {
  const bySeq = new Map<number, Alert>();
  for (const a of [...existing, ...incoming]) {
    if (a && typeof a.seq === "number") bySeq.set(a.seq, a);
  }
  return [...bySeq.values()].sort((a, b) => b.seq - a.seq).slice(0, max);
}

export function maxSeq(alerts: Alert[], fallback = 0): number {
  return alerts.reduce((m, a) => (typeof a.seq === "number" && a.seq > m ? a.seq : m), fallback);
}

const STATUS_LABELS: ReadonlyMap<string, string> = new Map([
  ["OK", "OK"],
  ["INSUFFICIENT_DATA", "Too few bars"],
  ["STALE_DATA", "Stale"],
  ["PROVIDER_ERROR", "Error"],
]);

/** Readable timeframe status. A Map, not an object literal: API strings such as "constructor" must stay text. */
export function statusLabel(status: unknown): string {
  if (typeof status !== "string" || status === "") return "—";
  return STATUS_LABELS.get(status) ?? status;
}
