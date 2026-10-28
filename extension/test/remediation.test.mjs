// Regression tests for the 2026-10-27 six-breaker audit fixes in the side panel:
// FLOW-3 (journal actions and verification), FLOW-8 (settings compare-and-set), SIMP-5 (account markets and
// symbols), ENV-8 / ENV-9 (forced colors order, input border contrast).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { API_BASE, ApiClient, ApiError } from "../build/src/api.js";
import { App } from "../build/src/app.js";
import {
  CANCEL_CONFIRM_TEXT, NOT_LISTED_TEXT, RESOLVE_CONFIRM_TEXT, canCancel, canResolve, isFinal, verificationLines,
} from "../build/src/journal.js";
import { isValidAccountSymbol, isValidSymbol, offeredAccountMarkets } from "../build/src/symbol.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const TOKEN = "tok_" + "x".repeat(40);
const COID = "mat0123456789abcdef";

function json(status, body, headers = {}) {
  return new Response(body === undefined ? "" : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function client(responder) {
  const calls = [];
  const c = new ApiClient({ fetch: async (url, init) => (calls.push({ url, init }), responder(url, init, calls.length)), getToken: async () => TOKEN });
  return { c, calls };
}

// ---------------------------------------------------------------- FLOW-3: journal rules
const STATES = ["SENDING", "UNKNOWN", "NOT_FOUND", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL",
  "FILLED", "CANCELED", "REJECTED", "EXPIRED", "USER_RESOLVED"];

test("Cancel is offered for every non-final state, including UNKNOWN, NOT_FOUND and SENDING", () => {
  const cancellable = STATES.filter((state) => canCancel({ state }));
  assert.deepEqual(cancellable, ["SENDING", "UNKNOWN", "NOT_FOUND", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"]);
  assert.equal(isFinal("USER_RESOLVED"), true);
  assert.equal(isFinal("NOT_FOUND"), false); // older rows: unsettled, never final
  // acknowledged-but-not-listed orders keep Cancel
  assert.equal(canCancel({ state: "NEW", acknowledged: 1, verification: "not_listed" }), true);
});

test("Mark resolved is offered only for never-acknowledged SENDING / UNKNOWN / NOT_FOUND orders", () => {
  for (const state of STATES) {
    const expected = ["SENDING", "UNKNOWN", "NOT_FOUND"].includes(state);
    assert.equal(canResolve({ state, acknowledged: 0 }), expected, state);
    assert.equal(canResolve({ state, acknowledged: 1 }), false, `${state} acknowledged`);
    assert.equal(canResolve({ state, acknowledged: true }), false, `${state} acknowledged (bool)`);
  }
});

test("verification lines show not-listed warnings and last checked / seen times", () => {
  const t = Date.UTC(2026, 9, 27, 13, 5);
  const lines = verificationLines({ verification: "not_listed", acknowledged: 0, last_checked_at: t, last_seen_at: null, lookups: 2 });
  assert.equal(lines[0], NOT_LISTED_TEXT);
  assert.match(NOT_LISTED_TEXT, /^Exchange did not list this order at the last check — do not place it again\.$/);
  assert.ok(lines.includes("Last checked: 2026-10-27 13:05 UTC"));
  assert.ok(lines.includes("Last seen by the exchange: never"));
  assert.ok(lines.includes("Not listed in 2 spaced checks."));
  const ack = verificationLines({ verification: "not_listed", acknowledged: 1, last_checked_at: t, last_seen_at: t - 60_000, lookups: 1 });
  assert.ok(ack.some((l) => l.includes("confirmed this order before")));
  assert.ok(ack.includes("Last seen by the exchange: 2026-10-27 13:04 UTC"));
  assert.match(verificationLines({ verification: "lookup_failed" })[0], /check failed/);
  assert.deepEqual(verificationLines({ verification: null }), []);
});

test("journal texts never suggest placing the order again", () => {
  const all = [
    NOT_LISTED_TEXT, RESOLVE_CONFIRM_TEXT, CANCEL_CONFIRM_TEXT,
    ...["not_listed", "lookup_failed", "confirmed"].flatMap((v) => [0, 1].flatMap((a) =>
      verificationLines({ verification: v, acknowledged: a, last_checked_at: 1, last_seen_at: 1, lookups: 3 }))),
  ];
  for (const text of all) {
    for (const m of text.matchAll(/\b(place|resend|re-?submit|new order)\b/gi)) {
      const before = text.slice(Math.max(0, m.index - 12), m.index).toLowerCase();
      assert.match(before, /(do not|don't|never)\s*$/, `"${text}" suggests placing an order`);
    }
  }
  assert.match(RESOLVE_CONFIRM_TEXT, /checked this order in the exchange's order history/);
  assert.match(RESOLVE_CONFIRM_TEXT, /does not cancel anything/);
});

test("journal view wires Cancel / Mark resolved to the shared rules behind confirmations", () => {
  const src = readFileSync(join(root, "src", "views", "accounts.ts"), "utf8");
  assert.ok(!/\["NEW", "PARTIALLY_FILLED"\]\.includes/.test(src), "Cancel must not be limited to NEW / PARTIALLY_FILLED");
  assert.match(src, /canCancel\(row\)/);
  assert.match(src, /canResolve\(row\)/);
  assert.match(src, /confirm\(RESOLVE_CONFIRM_TEXT\)\) void act\(\(\) => this\.app\.client\.resolveOrder/);
  assert.match(src, /confirm\(CANCEL_CONFIRM_TEXT\)\) void act\(\(\) => this\.app\.client\.cancelOrder/);
  assert.match(src, /"Mark resolved…"/);
});

test("resolveOrder posts explicit confirmation to a validated id; bad ids never reach the network", async () => {
  const { c, calls } = client(() => json(200, { client_order_id: COID, state: "USER_RESOLVED", note: "Marked resolved by you." }));
  const r = await c.resolveOrder(COID);
  assert.equal(r.state, "USER_RESOLVED");
  assert.equal(calls[0].url, `${API_BASE}/api/v1/trading/orders/${COID}/resolve`);
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), { confirm: true });
  for (const bad of ["mat123", "../settings", "MAT0123456789abcdef", `${COID}/../x`]) {
    await assert.rejects(c.resolveOrder(bad), (e) => e instanceof ApiError && e.kind === "validation");
  }
  assert.equal(calls.length, 1);
  const refused = client(() => json(409, { detail: "the exchange acknowledged this order" })).c;
  await assert.rejects(refused.resolveOrder(COID), (e) => e.kind === "conflict");
});

// ---------------------------------------------------------------- FLOW-8: settings compare-and-set
const SETTINGS = { selected_methodologies: [], analysis_timeframe: "1h", timeframes: ["1h"], watchlist: [], quiet_hours_utc: null,
  alerts_paused: false, monthly_ai_budget_usd: 0, monthly_ai_token_budget: 1, deep_analysis_opt_in: false, llm_data_consent: true };

test("412 maps to changed_elsewhere with a clear 'changed elsewhere, reloaded' message", async () => {
  const { c } = client(() => json(412, { detail: "settings changed since you loaded them; reload and apply your change again" }));
  await assert.rejects(c.putSettings(SETTINGS, '"0123456789abcdef"'), (e) => {
    assert.equal(e.kind, "changed_elsewhere");
    assert.equal(e.status, 412);
    assert.match(e.message, /changed elsewhere/);
    assert.match(e.message, /reloaded/);
    assert.match(e.message, /nothing was overwritten/);
    return true;
  });
});

test("settings GET returns the ETag and PUT sends it as If-Match; malformed tags are refused", async () => {
  const { c, calls } = client((_u, init) =>
    init.method === "GET" ? json(200, SETTINGS, { ETag: '"aaaaaaaaaaaaaaaa"' }) : json(200, JSON.parse(init.body), { ETag: '"bbbbbbbbbbbbbbbb"' }));
  const got = await c.getSettingsVersioned();
  assert.equal(got.etag, '"aaaaaaaaaaaaaaaa"');
  const put = await c.putSettingsVersioned(got.data, got.etag);
  assert.equal(calls[1].init.headers["If-Match"], '"aaaaaaaaaaaaaaaa"');
  assert.equal(put.etag, '"bbbbbbbbbbbbbbbb"');
  await assert.rejects(c.putSettings(SETTINGS, '"x"\r\nX-Evil: 1'), (e) => e.kind === "validation");
  assert.equal(calls.length, 2);
  // an unconditional PUT stays possible for callers that pass no tag
  await c.putSettings(SETTINGS);
  assert.equal(calls[2].init.headers["If-Match"], undefined);
});

function settingsServer() {
  // A tiny engine stand-in with real compare-and-set semantics.
  const state = { s: structuredClone(SETTINGS), v: 1 };
  const tag = () => `"${String(state.v).padStart(16, "0")}"`;
  const fetch = async (_url, init) => {
    if (init.method === "GET") return json(200, state.s, { ETag: tag() });
    const ifMatch = init.headers["If-Match"];
    if (ifMatch !== undefined && ifMatch !== tag()) return json(412, { detail: "settings changed since you loaded them" });
    state.s = JSON.parse(init.body);
    state.v += 1;
    return json(200, state.s, { ETag: tag() });
  };
  return { state, tag, fetch };
}

test("App.updateSettings: GET tag -> mutate -> PUT If-Match; a stale form gets 412, is reloaded, never retried", async () => {
  const srv = settingsServer();
  const puts = [];
  const app = new App(new ApiClient({ fetch: async (u, init) => (init.method === "PUT" && puts.push(init), srv.fetch(u, init)), getToken: async () => TOKEN }));
  let notified = 0;
  app.onSettingsChange(() => (notified += 1));

  // Tab A renders the form (consent on), tab B revokes consent meanwhile.
  await app.settings(true);
  const formTag = app.settingsEtag;
  assert.equal(formTag, srv.tag());
  srv.state.s.llm_data_consent = false;
  srv.state.v += 1;

  // Tab A saves its stale form: refused, nothing written, no silent retry, cache reloaded with the revocation.
  await assert.rejects(app.updateSettings((cur) => { cur.llm_data_consent = true; cur.analysis_timeframe = "4h"; }, formTag),
    (e) => e instanceof ApiError && e.kind === "changed_elsewhere");
  assert.equal(puts.length, 1);
  assert.equal(srv.state.s.llm_data_consent, false, "a revoked consent must not be restored");
  assert.equal(app.cachedSettings.llm_data_consent, false);
  assert.equal(app.settingsEtag, srv.tag());
  assert.equal(notified, 1);

  // After reloading, the save uses the new tag and succeeds.
  await app.updateSettings((cur) => { cur.analysis_timeframe = "4h"; }, app.settingsEtag);
  assert.equal(srv.state.s.analysis_timeframe, "4h");
  assert.equal(srv.state.s.llm_data_consent, false);
  assert.equal(app.settingsEtag, srv.tag());
  assert.equal(puts.length, 2);
  // Partial updates without a form tag still send the tag of the GET they mutate.
  const before = srv.tag();
  await app.updateSettings((cur) => { cur.alerts_paused = true; });
  assert.equal(puts[2].headers["If-Match"], before);
});

test("settings and methodology forms send the tag of what they show and re-render on conflict", () => {
  for (const f of ["settings.ts", "methods.ts"]) {
    const src = readFileSync(join(root, "src", "views", f), "utf8");
    assert.match(src, /this\.formEtag = this\.app\.settingsEtag/, f);
    assert.match(src, /\}, this\.formEtag\);/, f);
    assert.match(src, /err\.kind === "changed_elsewhere"/, f);
  }
});

// ---------------------------------------------------------------- SIMP-5: account markets and symbols
test("account markets come from the exchange catalog, including stock", () => {
  assert.deepEqual(offeredAccountMarkets(["stock"]), ["stock"]);
  assert.deepEqual(offeredAccountMarkets(["spot", "usdm_perpetual", "stock"]), ["spot", "usdm_perpetual", "stock"]);
  assert.deepEqual(offeredAccountMarkets(["coinm_futures"]), ["spot", "usdm_perpetual"]);
  assert.deepEqual(offeredAccountMarkets(undefined), ["spot", "usdm_perpetual"]);
  const src = readFileSync(join(root, "src", "views", "accounts.ts"), "utf8");
  const perf = src.slice(src.indexOf("private async renderPerformance"));
  assert.match(perf, /this\.marketOptions\(/, "Performance must use the connection's catalog markets");
});

test("account and trading calls accept the engine's account symbol rule; market data stays strict", async () => {
  for (const ok of ["BRK.B", "X", "BTCUSDT", "A".repeat(30)]) assert.ok(isValidAccountSymbol(ok), ok);
  for (const bad of ["brk.b", ".X", "A".repeat(31), "BTC USDT", "BTC/USDT", ""]) assert.ok(!isValidAccountSymbol(bad), bad);
  assert.ok(!isValidSymbol("BRK.B"));
  const { c, calls } = client(() => json(200, { trades: 0 }));
  await c.performance("0123456789abcdef", "BRK.B", "stock");
  assert.match(calls[0].url, /symbol=BRK.B&market=stock/);
  await assert.rejects(c.performance("0123456789abcdef", "brk.b", "stock"), (e) => e.kind === "validation");
  await assert.rejects(c.performance("0123456789abcdef", "BTCUSDT", "options"), (e) => e.kind === "validation");
  await c.previewOrder("0123456789abcdef", { symbol: "BRK.B", market: "stock", side: "buy", type: "limit", quantity: "1", price: "400" });
  await assert.rejects(c.previewOrder("0123456789abcdef", { symbol: "BRK B", market: "stock", side: "buy", type: "market", quantity: "1" }),
    (e) => e.kind === "validation");
  await assert.rejects(c.analysis("BRK.B", "spot"), (e) => e.kind === "validation");
  assert.equal(calls.length, 2);
});

// ---------------------------------------------------------------- ENV-8 / ENV-9: stylesheet
function contrast(a, b) {
  const lum = (hex) => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((x) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const css = readFileSync(join(root, "static", "sidepanel.css"), "utf8");
const noComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
const rootBlock = noComments.slice(noComments.indexOf(":root {"), noComments.indexOf("}", noComments.indexOf(":root {")));
const tokens = Object.fromEntries([...rootBlock.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/g)].map((m) => [m[1], m[2]]));

test("input, select and textarea borders reach 3:1 non-text contrast on every background (WCAG 1.4.11)", () => {
  const rule = noComments.match(/(^|\n)input, select, textarea \{([^}]*)\}/);
  assert.ok(rule, "input/select/textarea rule missing");
  const edge = rule[2].match(/border-color:\s*var\(--([\w-]+)\)/);
  assert.ok(edge, "inputs must set an explicit border token");
  const color = tokens[edge[1]];
  assert.ok(color, `token --${edge[1]} not defined in :root`);
  for (const bg of ["ground", "surface", "raised"]) {
    assert.ok(contrast(color, tokens[bg]) >= 3, `--${edge[1]} on --${bg}: ${contrast(color, tokens[bg]).toFixed(2)}:1`);
  }
  assert.ok(contrast(tokens["rule-strong"], tokens.ground) < 3); // the old border token was the defect
});

test("the forced-colors block comes last, so the selected-tab indicator wins over the neon layer", () => {
  const at = noComments.lastIndexOf("@media (forced-colors: active)");
  assert.ok(at > 0, "forced-colors block missing");
  assert.equal(noComments.indexOf("@media (forced-colors: active)"), at, "only one forced-colors block");
  const lastTabRule = noComments.lastIndexOf('[role="tab"][aria-selected="true"]');
  assert.ok(lastTabRule > at, "the forced-colors selected-tab rule must be the last one for that selector");
  const tail = noComments.slice(at);
  let depth = 0;
  let end = -1;
  for (let i = tail.indexOf("{"); i < tail.length; i++) {
    if (tail[i] === "{") depth++;
    else if (tail[i] === "}" && --depth === 0) { end = i; break; }
  }
  assert.equal(tail.slice(end + 1).trim(), "", "nothing may follow the forced-colors block");
  assert.match(tail, /\[role="tab"\]\[aria-selected="true"\] \{[^}]*Highlight/);
});
