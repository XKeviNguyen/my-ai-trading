import test from "node:test";
import assert from "node:assert/strict";
import { API_BASE, ApiClient, ApiError, extractDetail } from "../build/src/api.js";

const TOKEN = "tok_" + "x".repeat(40);

function fakeFetch(responder) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return responder(url, init);
  };
  return { fn, calls };
}

function json(status, body, headers = {}) {
  return new Response(body === undefined ? "" : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function client(responder, token = TOKEN) {
  const f = fakeFetch(responder);
  return { c: new ApiClient({ fetch: f.fn, getToken: async () => token }), calls: f.calls };
}

test("sends bearer token only to the fixed local base URL", async () => {
  const { c, calls } = client(() => json(200, { alerts: [] }));
  await c.alerts(5);
  assert.equal(calls.length, 1);
  const { url, init } = calls[0];
  assert.equal(new URL(url).origin, API_BASE);
  assert.equal(url, `${API_BASE}/api/v1/alerts?after_seq=5&limit=100`);
  assert.equal(init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(init.credentials, "omit");
  assert.equal(init.redirect, "error");
});

test("health is unauthenticated and sends no token", async () => {
  const { c, calls } = client(() => json(200, { status: "ok", version: "0.2.0", read_only: true }));
  const r = await c.health();
  assert.equal(r.status, "ok");
  assert.equal(calls[0].init.headers.Authorization, undefined);
});

test("refuses paths that would leave the base or traverse", () => {
  for (const p of ["//evil.example/x", "/api/v1/../../x", "http://evil.example/api/v1/x", "/api/v1/a?b", "/other", "/api/v1/@evil"]) {
    assert.throws(() => ApiClient.buildUrl(p), ApiError, p);
  }
  assert.equal(ApiClient.buildUrl("/api/v1/strategies/abc123/versions/2/approve"), `${API_BASE}/api/v1/strategies/abc123/versions/2/approve`);
});

test("query values are encoded, not spliced", () => {
  const u = new URL(ApiClient.buildUrl("/api/v1/analysis", { symbol: "A&b=c#", market: "spot" }));
  assert.equal(u.origin, API_BASE);
  assert.equal(u.searchParams.get("symbol"), "A&b=c#");
});

test("no request is made without a token", async () => {
  const { c, calls } = client(() => json(200, {}), null);
  await assert.rejects(c.getSettings(), (e) => e instanceof ApiError && e.kind === "unauthorized");
  assert.equal(calls.length, 0);
});

test("invalid symbols are rejected before any request", async () => {
  const { c, calls } = client(() => json(200, {}));
  await assert.rejects(c.analysis("btc usdt", "usdm_perpetual"), (e) => e.kind === "validation");
  await assert.rejects(c.ask("BTCUSDT", "margin", "q", false), (e) => e.kind === "validation");
  assert.equal(calls.length, 0);
});

test("maps HTTP status codes to typed errors", async () => {
  const cases = [
    [401, { detail: "missing or invalid API token" }, {}, "unauthorized", null],
    [421, { detail: "host not allowed" }, {}, "engine_unavailable", null],
    [429, { detail: "too many requests; slow down" }, {}, "rate_limited", null],
    [503, { detail: "provider rate limited" }, { "Retry-After": "17" }, "upstream_unavailable", 17],
    [502, { detail: "provider down" }, {}, "upstream_error", null],
    [404, { detail: "strategy not found" }, {}, "not_found", null],
    [409, { detail: "rule_hash does not match" }, {}, "conflict", null],
    [500, { detail: "boom" }, {}, "http", null],
  ];
  for (const [status, body, headers, kind, retry] of cases) {
    const { c } = client(() => json(status, body, headers));
    await assert.rejects(c.getSettings(), (e) => {
      assert.ok(e instanceof ApiError);
      assert.equal(e.kind, kind, `status ${status}`);
      assert.equal(e.status, status);
      assert.equal(e.retryAfterS, retry);
      return true;
    });
  }
});

test("401 message tells the user to set the token; 421 and refused connections say run serve", async () => {
  const a = client(() => json(401, { detail: "x" })).c;
  await assert.rejects(a.usage(), /my-ai-trading token/);
  const b = client(() => json(421, { detail: "host not allowed" })).c;
  await assert.rejects(b.usage(), /my-ai-trading serve/);
  const refused = client(() => {
    throw new TypeError("Failed to fetch");
  }).c;
  await assert.rejects(refused.usage(), (e) => e.kind === "engine_unavailable" && /my-ai-trading serve/.test(e.message));
});

test("503 shows Retry-After in the message", async () => {
  const { c } = client(() => json(503, { detail: "slow" }, { "Retry-After": "30" }));
  await assert.rejects(c.usage(), /Retry after 30 s/);
});

test("422 validation detail lists are flattened", async () => {
  const body = { detail: [{ loc: ["body", "symbol"], msg: "String should match pattern", type: "x" }, { loc: ["query", "limit"], msg: "too big", type: "y" }] };
  assert.equal(extractDetail(body), "symbol: String should match pattern; query.limit: too big");
  const { c } = client(() => json(422, body));
  await assert.rejects(c.putSettings({}), (e) => e.kind === "validation" && e.message.includes("symbol: String should match pattern"));
});

test("token never appears in thrown messages, even if echoed by the server", async () => {
  const { c } = client(() => json(422, { detail: `bad header Bearer ${TOKEN}` }));
  await assert.rejects(c.getSettings(), (e) => {
    assert.ok(!e.message.includes(TOKEN));
    assert.ok(!e.detail.includes(TOKEN));
    assert.ok(!String(e.stack).includes(TOKEN));
    return true;
  });
  const n = client(() => {
    throw new TypeError(`network failure for ${TOKEN}`);
  }).c;
  await assert.rejects(n.getSettings(), (e) => !e.message.includes(TOKEN) && !e.detail.includes(TOKEN));
});

test("timeouts map to a timeout error", async () => {
  const f = async (_url, init) =>
    new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" }))));
  const c = new ApiClient({ fetch: f, getToken: async () => TOKEN, timeoutMs: 10 });
  await assert.rejects(c.getSettings(), (e) => e.kind === "timeout");
});

test("approve validates ids and rule_hash and posts the hash", async () => {
  const { c, calls } = client(() => json(200, { status: "ACTIVE" }));
  await assert.rejects(c.approve("abc/../x", 1, "0123456789abcdef"), ApiError);
  await assert.rejects(c.approve("abc", 1, "XYZ"), ApiError);
  await assert.rejects(c.approve("abc", 0, "0123456789abcdef"), ApiError);
  await c.approve("abc123", 2, "0123456789abcdef");
  assert.equal(calls[0].url, `${API_BASE}/api/v1/strategies/abc123/versions/2/approve`);
  assert.deepEqual(JSON.parse(calls[0].init.body), { rule_hash: "0123456789abcdef" });
});

test("PUT settings sends the full object as JSON", async () => {
  const settings = { selected_methodologies: [], analysis_timeframe: "4h", timeframes: ["1h"], watchlist: [], quiet_hours_utc: null, alerts_paused: false, monthly_ai_budget_usd: 0, monthly_ai_token_budget: 1, deep_analysis_opt_in: false, llm_data_consent: false };
  const { c, calls } = client((_u, init) => json(200, JSON.parse(init.body)));
  const r = await c.putSettings(settings);
  assert.equal(calls[0].init.method, "PUT");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(r, settings);
});

test("report markdown is returned as text; report ids and symbols are validated", async () => {
  const md = "# Report\n\n| 1h | OK |";
  const { c, calls } = client(() => new Response(md, { status: 200, headers: { "Content-Type": "text/markdown" } }));
  assert.equal(await c.reportMarkdown("0123456789ab"), md);
  assert.match(calls[0].url, /\/api\/v1\/reports\/0123456789ab\?format=markdown$/);
  await assert.rejects(c.reportMarkdown("../../etc"), ApiError);
  await assert.rejects(c.saveReport("bad sym", "usdm_perpetual"), ApiError);
  assert.equal(calls.length, 1);
  const err = client(() => json(404, { detail: "report not found" }));
  await assert.rejects(err.c.reportMarkdown("0123456789ab"), (e) => e instanceof ApiError && e.status === 404);
});

test("account and trading statuses map to their own kinds; 403 is a policy refusal, not a token problem", async () => {
  const cases = [
    [403, "live keys must be read-only", "forbidden", /Not allowed: live keys must be read-only/],
    [410, "this connection expired and its stored key was deleted", "expired", /expired/],
    [424, "Binance rejected the key (code -2015)", "exchange_auth", /exchange rejected the key/],
    [501, "order history is not implemented", "not_supported", /Not supported/],
    [504, "exchange did not answer in time", "outcome_unknown", /do not resend/],
  ];
  for (const [status, detail, kind, message] of cases) {
    const { c } = client(() => json(status, { detail }));
    await assert.rejects(c.connections(), (e) => {
      assert.equal(e.kind, kind, `status ${status}`);
      assert.match(e.message, message);
      assert.doesNotMatch(e.message, /my-ai-trading token/);
      return true;
    });
  }
});

test("disconnect uses DELETE on a validated id; bad ids never reach the network", async () => {
  const { c, calls } = client(() => json(200, { deleted: "0123456789abcdef", note: "deleted locally" }));
  await c.disconnect("0123456789abcdef");
  assert.equal(calls[0].init.method, "DELETE");
  assert.equal(calls[0].url, `${API_BASE}/api/v1/accounts/connections/0123456789abcdef`);
  for (const bad of ["../settings", "0123456789ABCDEF", "x".repeat(16), ""]) {
    await assert.rejects(c.disconnect(bad), (e) => e.kind === "validation");
    await assert.rejects(c.snapshot(bad), (e) => e.kind === "validation");
  }
  for (const bad of ["mat123", "abc0123456789abcdef", "mat0123456789abcdeg"]) {
    await assert.rejects(c.cancelOrder(bad), (e) => e.kind === "validation");
  }
  assert.equal(calls.length, 1);
});

test("the API secret is sent once in the body and never appears in error text", async () => {
  const secret = "very-secret-value-0123456789";
  const { c, calls } = client(() => json(422, { detail: [{ loc: ["body", "api_secret"], msg: "too long", type: "x" }] }));
  await assert.rejects(c.connect({ exchange: "binance", environment: "testnet", lifetime: "session", label: "",
                                   api_key: "k".repeat(16), api_secret: secret }), (e) => {
    assert.doesNotMatch(e.message, new RegExp(secret));
    return true;
  });
  assert.equal(JSON.parse(calls[0].init.body).api_secret, secret);
  assert.doesNotMatch(calls[0].url, new RegExp(secret));
});

test("cancelling a testnet order always sends explicit confirmation", async () => {
  const { c, calls } = client(() => json(200, { client_order_id: "mat0123456789abcdef", state: "CANCELED" }));
  await c.cancelOrder("mat0123456789abcdef");
  assert.deepEqual(JSON.parse(calls[0].init.body), { confirm: true });
});
