// Pure helpers used by the views (no DOM needed).
import test from "node:test";
import assert from "node:assert/strict";
import { alertSummary, fmtAge, fmtMs, fmtNum, fmtUsd, isQuiet, maxSeq, mergeAlerts, notificationText } from "../build/src/format.js";
import { buildSelections } from "../build/src/views/methods.js";
import { splitPrompts, NAME_RE } from "../build/src/views/studio.js";
import { cleanWatchlist, TOKEN_RE } from "../build/src/views/settings.js";
import { checkImage } from "../build/src/views/copilot.js";

const alert = (seq, extra = {}) => ({
  seq,
  event_id: `e${seq}`,
  instrument_key: "binance:usdm_perpetual:SANDUSDT",
  timeframe: "1h",
  source: "engine",
  event_type: "BREAKOUT",
  bar_open_time_ms: 1_761_480_000_000,
  priority: 3,
  created_at: 1_761_483_700_000,
  payload: { event: { type: "BREAKOUT", invalidation: "a closed 1h bar back below 0.31" }, quiet: false },
  ...extra,
});

test("mergeAlerts dedupes by seq, newest first, bounded", () => {
  const merged = mergeAlerts([alert(1), alert(2)], [alert(2), alert(5), alert(3)], 3);
  assert.deepEqual(merged.map((a) => a.seq), [5, 3, 2]);
  assert.equal(maxSeq(merged, 0), 5);
  assert.equal(maxSeq([], 7), 7);
});

test("alert summaries and notification text are built from fields", () => {
  const a = alert(1);
  assert.equal(alertSummary(a), "Invalidated by a closed 1h bar back below 0.31");
  const n = notificationText(a);
  assert.equal(n.title, "SANDUSDT 1h · BREAKOUT");
  assert.ok(n.message.startsWith("Invalidated by"));
  assert.ok(n.message.length <= 200);
  const s = alert(2, { event_type: "STRATEGY_MATCH", payload: { name: "My rule", version: 3, quiet: true } });
  assert.equal(isQuiet(s), true);
  assert.match(alertSummary(s), /My rule.*v3/);
  const m = alert(3, { event_type: "METHODOLOGY_STATE", payload: { methodology_id: "weinstein", from: "STAGE_1", to: "STAGE_2", label: "x" } });
  assert.match(alertSummary(m), /weinstein: STAGE_1 → STAGE_2/);
  const html = alert(4, { payload: { event: { type: "X", invalidation: "<img src=x onerror=alert(1)>" } } });
  assert.equal(alertSummary(html), "Invalidated by <img src=x onerror=alert(1)>"); // stays plain text
});

test("number/time formatting", () => {
  assert.equal(fmtNum(0.3123456789), "0.312346");
  assert.equal(fmtNum(null), "—");
  assert.equal(fmtNum(NaN), "—");
  assert.equal(fmtUsd(1.5), "$1.50");
  assert.equal(fmtUsd(0.000123), "$0.000123");
  assert.equal(fmtUsd(null), "unknown");
  assert.equal(fmtMs(Date.UTC(2026, 9, 26, 13, 0)), "2026-10-26 13:00 UTC");
  assert.equal(fmtAge(1000, 31_000), "30 s ago");
  assert.equal(fmtAge("2026-10-26T10:00:00Z", Date.parse("2026-10-26T13:00:00Z")), "3 h ago");
});

test("methodology selection keeps saved order, allows none", () => {
  const prev = [{ methodology_id: "weinstein", mode: "NATIVE" }, { methodology_id: "darvas", mode: "NATIVE" }];
  const enabled = new Map([["darvas", "HYBRID"], ["minervini", "ADAPTED"], ["weinstein", "NATIVE"]]);
  assert.deepEqual(buildSelections(prev, ["minervini", "weinstein", "darvas"], enabled), [
    { methodology_id: "weinstein", mode: "NATIVE" },
    { methodology_id: "darvas", mode: "HYBRID" },
    { methodology_id: "minervini", mode: "ADAPTED" },
  ]);
  assert.deepEqual(buildSelections(prev, ["weinstein", "darvas"], new Map()), []);
});

test("strategy prompt splitting and name check", () => {
  assert.deepEqual(splitPrompts("  a \n\n b\r\nc  "), ["a", "b", "c"]);
  assert.equal(NAME_RE.test("Breakout (1h) v2"), true);
  assert.equal(NAME_RE.test("<script>"), false);
});

test("watchlist cleaning", () => {
  const r = cleanWatchlist([
    { symbol: "sandusdt", market: "usdm_perpetual" },
    { symbol: "SAND/USDT", market: "usdm_perpetual" },
    { symbol: "btc_usdt", market: "spot" },
    { symbol: "", market: "spot" },
    { symbol: "<bad>", market: "spot" },
    { symbol: "ETHUSDT", market: "evil" },
  ]);
  assert.deepEqual(r.items, [
    { symbol: "SANDUSDT", market: "usdm_perpetual" },
    { symbol: "BTCUSDT", market: "spot" },
    { symbol: "ETHUSDT", market: "usdm_perpetual" },
  ]);
  assert.equal(r.errors.length, 1);
});

test("token format check", () => {
  assert.equal(TOKEN_RE.test("a".repeat(43)), true);
  assert.equal(TOKEN_RE.test("short"), false);
  assert.equal(TOKEN_RE.test("a".repeat(40) + "\nX"), false);
  assert.equal(TOKEN_RE.test("a".repeat(20) + " " + "a".repeat(20)), false);
});

test("screenshot client-side checks", () => {
  assert.equal(checkImage({ type: "image/png", size: 1000 }), null);
  assert.equal(checkImage({ type: "image/webp", size: 5 * 1024 * 1024 }), null);
  assert.match(checkImage({ type: "image/png", size: 5 * 1024 * 1024 + 1 }), /5 MB/);
  assert.match(checkImage({ type: "image/gif", size: 10 }), /PNG, JPEG or WEBP/);
  assert.match(checkImage({ type: "image/png", size: 0 }), /empty/);
  assert.match(checkImage(null), /Choose/);
});
