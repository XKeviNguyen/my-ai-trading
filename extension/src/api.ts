// Typed client for the local My AI Trading API. Pure logic: no DOM, no chrome.* — fetch is injected.
// The base URL is a constant; the bearer token is sent only to it and never appears in error text.

import { isMarket, isValidSymbol, type Market } from "./symbol.js";
import type {
  AlertsResponse,
  AnalysisResponse,
  AskResponse,
  ChallengeResponse,
  CompileResponse,
  CreateStrategyResponse,
  DryRunResponse,
  HealthResponse,
  MethodologiesResponse,
  StrategiesResponse,
  StrategyRule,
  ReportsResponse,
  UsageResponse,
  UserSettings,
  ConnectionMeta,
  ExchangesResponse,
  AccountSnapshot,
  PortfolioResponse,
  PerformanceResponse,
  OrderDraft,
  TradePreview,
  TradeResult,
  JournalResponse,
  VisionResponse,
} from "./types.js";

export const API_BASE = "http://127.0.0.1:8765";
export const TIMEFRAMES = ["1h", "4h", "1d", "1w", "1M"] as const;
const PATH_RE = /^\/api\/v1(?:\/[A-Za-z0-9_-]+)*$/;
const RULE_HASH_RE = /^[0-9a-f]{16}$/;
const STRATEGY_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const DEFAULT_TIMEOUT_MS = 30_000;
const LLM_TIMEOUT_MS = 180_000;
const MAX_DETAIL_CHARS = 600;

export type ErrorKind =
  | "unauthorized" // 401 or no token saved
  | "engine_unavailable" // 421 or connection refused / network failure
  | "rate_limited" // 429
  | "upstream_unavailable" // 503 (provider rate limited)
  | "upstream_error" // 502 (market data provider failed)
  | "validation" // 422 or client-side validation
  | "not_found" // 404
  | "conflict" // 409 (e.g. rule_hash mismatch, trading refusal)
  | "forbidden" // 403 (outside the account/trading policy)
  | "expired" // 410 (exchange connection expired; its key was deleted)
  | "exchange_auth" // 424 (the exchange rejected the API key)
  | "not_supported" // 501
  | "outcome_unknown" // 504 (an exchange request may or may not have taken effect)
  | "timeout"
  | "http"; // anything else

export class ApiError extends Error {
  readonly kind: ErrorKind;
  readonly status: number;
  readonly retryAfterS: number | null;
  readonly detail: string;

  constructor(kind: ErrorKind, status: number, detail: string, retryAfterS: number | null = null) {
    super(userMessage(kind, detail, retryAfterS));
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
    this.retryAfterS = retryAfterS;
  }
}

export function userMessage(kind: ErrorKind, detail: string, retryAfterS: number | null): string {
  const retry = retryAfterS !== null ? ` Retry after ${retryAfterS} s.` : " Wait a few seconds and retry.";
  switch (kind) {
    case "unauthorized":
      return "API token missing or invalid. Open Settings and paste the token from `my-ai-trading token`.";
    case "engine_unavailable":
      return "Local engine not running or not reachable: run `my-ai-trading serve`.";
    case "rate_limited":
      return "Too many requests to the local engine." + retry;
    case "upstream_unavailable":
      return "Market data provider is rate limiting or unavailable." + retry + (detail ? ` (${detail})` : "");
    case "upstream_error":
      return "Market data provider error" + (detail ? `: ${detail}` : ".");
    case "validation":
      return "Request rejected: " + (detail || "invalid input");
    case "not_found":
      return "Not found" + (detail ? `: ${detail}` : ".");
    case "conflict":
      return "Conflict: " + (detail || "state changed; reload and review");
    case "forbidden":
      return "Not allowed: " + (detail || "this action is outside the app's account policy");
    case "expired":
      return detail || "This exchange connection expired; connect again.";
    case "exchange_auth":
      return "The exchange rejected the key" + (detail ? `: ${detail}` : ".");
    case "not_supported":
      return "Not supported" + (detail ? `: ${detail}` : ".");
    case "outcome_unknown":
      return "The exchange did not confirm the result. Check the order status; do not resend." + (detail ? ` (${detail})` : "");
    case "timeout":
      return "The local engine did not answer in time.";
    default:
      return "Unexpected API error" + (detail ? `: ${detail}` : ".");
  }
}

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export interface ClientOptions {
  fetch: FetchLike;
  getToken: () => Promise<string | null | undefined>;
  timeoutMs?: number;
}

interface RequestOptions {
  query?: Record<string, string | number | boolean | undefined | null>;
  json?: unknown;
  form?: FormData;
  auth?: boolean;
  timeoutMs?: number;
  /** Return the raw response body as text instead of parsing JSON. */
  text?: boolean;
}

function scrub(text: string, token: string | null | undefined): string {
  let out = text;
  if (token && token.length >= 8) out = out.split(token).join("[redacted]");
  return out.length > MAX_DETAIL_CHARS ? out.slice(0, MAX_DETAIL_CHARS) + "…" : out;
}

/** Turn a FastAPI error body (`detail` string or validation list) into one readable line. */
export function extractDetail(body: unknown): string {
  if (!body || typeof body !== "object") return "";
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (!d || typeof d !== "object") return String(d);
        const item = d as { loc?: unknown; msg?: unknown };
        const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body").join(".") : "";
        const msg = typeof item.msg === "string" ? item.msg : "invalid";
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("; ");
  }
  return "";
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const n = Number(value.trim());
  return Number.isFinite(n) && n >= 0 ? Math.ceil(n) : null;
}

function kindForStatus(status: number): ErrorKind {
  switch (status) {
    case 401:
      return "unauthorized";
    case 403:
      return "forbidden";
    case 410:
      return "expired";
    case 424:
      return "exchange_auth";
    case 501:
      return "not_supported";
    case 504:
      return "outcome_unknown";
    case 404:
      return "not_found";
    case 409:
      return "conflict";
    case 421:
      return "engine_unavailable";
    case 422:
    case 400:
      return "validation";
    case 429:
      return "rate_limited";
    case 502:
      return "upstream_error";
    case 503:
      return "upstream_unavailable";
    default:
      return "http";
  }
}

function requireSymbol(symbol: string, market: Market): void {
  if (!isValidSymbol(symbol)) throw new ApiError("validation", 0, "symbol must match ^[A-Z0-9]{3,20}$");
  if (!isMarket(market)) throw new ApiError("validation", 0, "market must be usdm_perpetual or spot");
}

export class ApiClient {
  private readonly fetchFn: FetchLike;
  private readonly getToken: () => Promise<string | null | undefined>;
  private readonly timeoutMs: number;

  constructor(opts: ClientOptions) {
    this.fetchFn = opts.fetch;
    this.getToken = opts.getToken;
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  /** Build an absolute URL under the fixed base; refuses anything that would leave it. */
  static buildUrl(path: string, query?: RequestOptions["query"]): string {
    if (!PATH_RE.test(path)) throw new ApiError("validation", 0, "invalid API path");
    const url = new URL(API_BASE + path);
    if (url.origin !== API_BASE) throw new ApiError("validation", 0, "refusing non-local API host");
    for (const [k, v] of Object.entries(query ?? {})) {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
    }
    return url.toString();
  }

  async request<T>(method: "GET" | "POST" | "PUT" | "DELETE", path: string, opts: RequestOptions = {}): Promise<T> {
    const url = ApiClient.buildUrl(path, opts.query);
    const headers: Record<string, string> = { Accept: "application/json" };
    let token: string | null | undefined = null;
    if (opts.auth !== false) {
      token = (await this.getToken())?.trim();
      if (!token) throw new ApiError("unauthorized", 0, "no API token saved");
      if (/[\r\n]/.test(token)) throw new ApiError("unauthorized", 0, "saved token is malformed");
      headers["Authorization"] = `Bearer ${token}`;
    }
    let body: BodyInit | undefined;
    if (opts.form) {
      body = opts.form; // the browser sets the multipart boundary
    } else if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.json);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? this.timeoutMs);
    let res: Response;
    try {
      res = await this.fetchFn(url, {
        method,
        headers,
        body,
        signal: controller.signal,
        credentials: "omit",
        redirect: "error", // never follow a redirect that could carry the token elsewhere
        cache: "no-store",
        referrerPolicy: "no-referrer",
      });
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") throw new ApiError("timeout", 0, "");
      throw new ApiError("engine_unavailable", 0, "connection failed");
    } finally {
      clearTimeout(timer);
    }

    let parsed: unknown = null;
    let raw = "";
    try {
      raw = await res.text();
      parsed = raw && !(opts.text && res.ok) ? JSON.parse(raw) : null;
    } catch {
      parsed = null;
    }
    if (opts.text && res.ok) return raw as unknown as T; // e.g. a Markdown export
    if (!res.ok) {
      const kind = kindForStatus(res.status);
      const retry =
        kind === "rate_limited" || kind === "upstream_unavailable"
          ? parseRetryAfter(res.headers.get("Retry-After"))
          : null;
      throw new ApiError(kind, res.status, scrub(extractDetail(parsed), token), retry);
    }
    if (parsed === null || typeof parsed !== "object") {
      throw new ApiError("http", res.status, "response was not a JSON object");
    }
    return parsed as T;
  }

  // ---- endpoints ----
  async health(): Promise<HealthResponse> {
    return this.request("GET", "/api/v1/health", { auth: false, timeoutMs: 5_000 });
  }
  async methodologies(): Promise<MethodologiesResponse> {
    return this.request("GET", "/api/v1/methodologies");
  }
  async getSettings(): Promise<UserSettings> {
    return this.request("GET", "/api/v1/settings");
  }
  async putSettings(settings: UserSettings): Promise<UserSettings> {
    return this.request("PUT", "/api/v1/settings", { json: settings });
  }
  async analysis(symbol: string, market: Market, analysisTimeframe?: string): Promise<AnalysisResponse> {
    requireSymbol(symbol, market);
    return this.request("GET", "/api/v1/analysis", {
      query: { symbol, market, analysis_timeframe: analysisTimeframe },
    });
  }
  async ask(symbol: string, market: Market, question: string, deep: boolean): Promise<AskResponse> {
    requireSymbol(symbol, market);
    return this.request("POST", "/api/v1/copilot/ask", {
      json: { symbol, market, question, deep },
      timeoutMs: LLM_TIMEOUT_MS,
    });
  }
  async challenge(
    symbol: string,
    market: Market,
    thesis: string,
    focusTimeframe: string | null,
    deep: boolean,
  ): Promise<ChallengeResponse> {
    requireSymbol(symbol, market);
    return this.request("POST", "/api/v1/copilot/challenge", {
      json: { symbol, market, thesis, focus_timeframe: focusTimeframe, deep },
      timeoutMs: LLM_TIMEOUT_MS,
    });
  }
  async vision(form: FormData): Promise<VisionResponse> {
    return this.request("POST", "/api/v1/copilot/vision", { form, timeoutMs: LLM_TIMEOUT_MS });
  }
  async compile(prompts: string[], name: string, defaultTimeframe: string | null, useLlm: boolean): Promise<CompileResponse> {
    return this.request("POST", "/api/v1/strategies/compile", {
      json: { prompts, name, default_timeframe: defaultTimeframe, use_llm_fallback: useLlm },
      timeoutMs: useLlm ? LLM_TIMEOUT_MS : DEFAULT_TIMEOUT_MS,
    });
  }
  async dryRun(rule: StrategyRule, symbol: string, market: Market, replayBars = 200): Promise<DryRunResponse> {
    requireSymbol(symbol, market);
    return this.request("POST", "/api/v1/strategies/dry-run", {
      json: { rule, symbol, market, replay_bars: replayBars },
    });
  }
  async strategies(): Promise<StrategiesResponse> {
    return this.request("GET", "/api/v1/strategies");
  }
  async createStrategy(rule: StrategyRule, prompts: string[]): Promise<CreateStrategyResponse> {
    return this.request("POST", "/api/v1/strategies", { json: { rule, prompts, kind: "CUSTOM" } });
  }
  async approve(strategyId: string, version: number, ruleHash: string): Promise<{ status: string }> {
    if (!STRATEGY_ID_RE.test(strategyId) || !Number.isInteger(version) || version < 1) {
      throw new ApiError("validation", 0, "invalid strategy id or version");
    }
    if (!RULE_HASH_RE.test(ruleHash)) throw new ApiError("validation", 0, "invalid rule_hash");
    return this.request("POST", `/api/v1/strategies/${strategyId}/versions/${version}/approve`, {
      json: { rule_hash: ruleHash },
    });
  }
  async deactivate(strategyId: string): Promise<{ status: string }> {
    if (!STRATEGY_ID_RE.test(strategyId)) throw new ApiError("validation", 0, "invalid strategy id");
    return this.request("POST", `/api/v1/strategies/${strategyId}/deactivate`);
  }
  async alerts(afterSeq: number, limit = 100): Promise<AlertsResponse> {
    const after = Number.isInteger(afterSeq) && afterSeq >= 0 ? afterSeq : 0;
    return this.request("GET", "/api/v1/alerts", { query: { after_seq: after, limit } });
  }
  async usage(): Promise<UsageResponse> {
    return this.request("GET", "/api/v1/usage");
  }
  async saveReport(symbol: string, market: Market, analysisTimeframe?: string): Promise<{ id: string; title: string }> {
    requireSymbol(symbol, market);
    return this.request("POST", "/api/v1/reports", {
      json: { symbol, market, ...(analysisTimeframe ? { analysis_timeframe: analysisTimeframe } : {}) },
    });
  }
  async reports(): Promise<ReportsResponse> {
    return this.request("GET", "/api/v1/reports");
  }
  async reportMarkdown(id: string): Promise<string> {
    if (!/^[0-9a-f]{12}$/.test(id)) throw new ApiError("validation", 0, "invalid report id");
    return this.request("GET", `/api/v1/reports/${id}`, { query: { format: "markdown" }, text: true });
  }

  // ---- exchange accounts (private routes) ----
  async exchanges(): Promise<ExchangesResponse> {
    return this.request("GET", "/api/v1/accounts/exchanges");
  }
  async connections(): Promise<{ connections: ConnectionMeta[] }> {
    return this.request("GET", "/api/v1/accounts/connections");
  }
  /** Sends the key once. The response never contains it; the caller must clear its inputs. */
  async connect(body: {
    exchange: string; environment: "live" | "testnet"; lifetime: string; label: string;
    api_key: string; api_secret: string; passphrase?: string;
  }): Promise<{ connection: ConnectionMeta; note: string }> {
    return this.request("POST", "/api/v1/accounts/connections", { json: body });
  }
  async disconnect(id: string): Promise<{ deleted: string; note: string }> {
    return this.request("DELETE", `/api/v1/accounts/connections/${requireId(id)}`);
  }
  async snapshot(id: string, fresh = false): Promise<AccountSnapshot> {
    return this.request("GET", `/api/v1/accounts/connections/${requireId(id)}/snapshot`, { query: { fresh } });
  }
  async portfolio(environment?: "live" | "testnet"): Promise<PortfolioResponse> {
    return this.request("GET", "/api/v1/accounts/portfolio", { query: { environment } });
  }
  async performance(id: string, symbol: string, market: string): Promise<PerformanceResponse> {
    return this.request("GET", `/api/v1/accounts/connections/${requireId(id)}/performance`, { query: { symbol, market } });
  }

  // ---- testnet trading ----
  async tradingConsent(): Promise<{ consent_text: string }> {
    return this.request("GET", "/api/v1/trading/consent");
  }
  async setTradingMode(id: string, enabled: boolean, consent: string | null): Promise<{ connection: ConnectionMeta }> {
    return this.request("PUT", "/api/v1/trading/mode", { json: { connection_id: requireId(id), enabled, consent } });
  }
  async previewOrder(id: string, draft: OrderDraft): Promise<TradePreview> {
    return this.request("POST", "/api/v1/trading/preview", { json: { connection_id: requireId(id), draft } });
  }
  async submitOrder(previewId: string, confirmToken: string): Promise<TradeResult> {
    return this.request("POST", "/api/v1/trading/orders", { json: { preview_id: previewId, confirm_token: confirmToken } });
  }
  async journal(): Promise<JournalResponse> {
    return this.request("GET", "/api/v1/trading/orders");
  }
  async reconcile(clientOrderId: string): Promise<TradeResult> {
    return this.request("POST", `/api/v1/trading/orders/${requireOrderId(clientOrderId)}/reconcile`);
  }
  async cancelOrder(clientOrderId: string): Promise<TradeResult> {
    return this.request("POST", `/api/v1/trading/orders/${requireOrderId(clientOrderId)}/cancel`, { json: { confirm: true } });
  }
}

const CONN_ID_RE = /^[0-9a-f]{16}$/;
const ORDER_ID_RE = /^mat[0-9a-f]{16}$/;

function requireId(id: string): string {
  if (!CONN_ID_RE.test(id)) throw new ApiError("validation", 0, "invalid connection id");
  return id;
}

function requireOrderId(id: string): string {
  if (!ORDER_ID_RE.test(id)) throw new ApiError("validation", 0, "invalid client order id");
  return id;
}
