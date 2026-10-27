// Response shapes of the local API (src/my_ai_trading/api.py). Fields are optional where the server
// may omit them; renderers must cope with missing values. All string content is untrusted.

import type { Market } from "./symbol.js";

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export interface HealthResponse {
  status: string;
  version: string;
  read_only: boolean;
}

export interface Selection {
  methodology_id: string;
  mode: "NATIVE" | "HYBRID" | "ADAPTED";
}

export interface WatchItem {
  symbol: string;
  market: Market;
}

export interface UserSettings {
  selected_methodologies: Selection[];
  analysis_timeframe: string;
  timeframes: string[];
  watchlist: WatchItem[];
  quiet_hours_utc: [number, number] | null;
  alerts_paused: boolean;
  monthly_ai_budget_usd: number;
  monthly_ai_token_budget: number;
  deep_analysis_opt_in: boolean;
  llm_data_consent: boolean;
  [extra: string]: unknown; // preserved verbatim on PUT
}

export interface Profile {
  methodology_id: string;
  name: string;
  focus: string;
  published_assets: string[];
  documented_timeframes: string[] | "UNVERIFIED";
  context_timeframes: string[];
  execution_timeframes: string[] | string;
  required_data: string[];
  evidence_quality: string;
  provenance: string;
  implementation_maturity: string;
  evaluator: string | null;
  limitations: string[];
  price_only: boolean;
}

export interface MethodologiesResponse {
  profiles: Profile[];
  count: number;
  note: string;
}

export interface MarketEvent {
  type: string;
  bar_close_time_utc?: string;
  invalidation?: string;
  conditions_met?: Record<string, Json>;
  rule_version?: string;
}

export interface TimeframeReport {
  timeframe?: string;
  status: string;
  error?: string;
  bar_close_time_utc?: string;
  last_close?: number;
  indicator_values?: Record<string, number | null>;
  trend?: { state?: string; reason?: string; conditions?: Record<string, boolean> };
  levels?: Record<string, number | null>;
  events?: MarketEvent[];
  patterns?: PatternDetection[];
  missing_inputs?: string[];
  data_quality?: { closed_bars?: number; contiguous_bars?: number; gaps?: number; missing_bars?: number; stale?: boolean };
}

export interface PatternDetection {
  pattern: string;
  state: string;
  end_bar_open_ms?: number;
  key_levels?: Record<string, number | boolean | null>;
  invalidation?: string;
  note?: string;
}

export interface Alignment {
  trend_by_timeframe?: Record<string, string>;
  verdict?: string;
  note?: string;
}

export interface InstrumentInfo {
  venue?: string;
  market_type?: string;
  symbol?: string;
  quote_asset?: string;
  calendar?: string;
  key?: string;
}

export interface Snapshot {
  instrument?: InstrumentInfo;
  as_of_ms?: number;
  as_of_utc?: string;
  source?: string;
  timeframes?: Record<string, TimeframeReport>;
  alignment?: Alignment;
}

export interface Assessment {
  methodology_id: string;
  name: string;
  requested_mode: string;
  status: string;
  label: string;
  evaluation_timeframe: string | null;
  analysis_timeframe: string;
  reasons?: string[];
  offered_routes?: { mode: string; description: string }[];
  requires_user_approval?: boolean;
}

export interface Perspective {
  assessment: Assessment;
  provenance?: string;
  evidence_quality?: string;
  implementation_maturity?: string;
  limitations?: string[];
  evaluation?: {
    state?: string;
    reason?: string;
    method?: string;
    timeframe?: string;
    bar_close_time_utc?: string | null;
    checks?: Record<string, Json>;
    unevaluated_criteria?: string[];
    [k: string]: unknown;
  };
  tactical_layer?: {
    label?: string;
    timeframe?: string;
    status?: string;
    events?: MarketEvent[];
    trend?: { state?: string } | null;
  };
}

export interface AnalysisResponse {
  snapshot: Snapshot;
  perspectives: {
    analysis_timeframe?: string;
    perspectives?: Perspective[];
    aggregation?: string;
  };
  selected_count: number;
}

export interface FactRow {
  timeframe: string;
  status?: string;
  error?: string;
  bar_close_time_utc?: string;
  close?: number | null;
  trend?: string | null;
  sma50?: number | null;
  sma200?: number | null;
  rsi14?: number | null;
  volume_ratio_20?: number | null;
  events?: string[];
}

export interface Interpretation {
  label?: string;
  text?: string;
  model?: string;
  input_tokens?: number;
  output_tokens?: number;
  estimated_cost_usd?: number | null;
  latency_ms?: number;
}

export interface AskResponse {
  question?: string;
  facts?: {
    label?: string;
    timeframes?: FactRow[];
    alignment?: Alignment;
    perspectives?: { methodology?: string; status?: string; label?: string; evaluation_timeframe?: string | null; state?: string }[];
  };
  interpretation?: Interpretation | null;
  route?: string;
  fallback_reason?: string;
  snapshot_as_of_utc?: string;
}

export interface ThesisAudit {
  thesis?: string;
  direction?: string | null;
  focus_timeframe?: string | null;
  observations?: string[];
  supporting_evidence?: string[];
  contradicting_evidence?: string[];
  assumptions?: string[];
  unknowns?: string[];
  falsification_conditions?: string[];
  proposed_tests?: string[];
  assessment?: string;
  assessment_note?: string;
  method?: string;
}

export interface ChallengeResponse {
  audit?: ThesisAudit;
  debate?: { label?: string; text?: string; model?: string; estimated_cost_usd?: number | null } | null;
  route?: string;
  fallback_reason?: string;
}

export interface VisionCheck {
  check?: string;
  result?: string;
  expected?: string;
  read?: string[];
  timeframe?: string;
  read_median?: number;
  exchange_range_last_300_bars?: number[];
  reason?: string;
  note?: string;
}

export interface LocalVisionAnalysis {
  method?: string;
  label?: string;
  status?: string;
  reason?: string;
  privacy?: string;
  image?: { width?: number; height?: number };
  geometry?: {
    state?: string;
    candles_detected?: number;
    green?: number;
    red?: number;
    visual_drift?: string;
    last_visible_candle_color?: string;
    note?: string;
  };
  ocr?: {
    status?: string;
    reason?: string;
    mean_confidence?: number | null;
    confidence_note?: string;
    parsed?: {
      symbols?: string[];
      timeframes?: string[];
      indicator_labels?: string[];
      price_axis_labels?: number[];
      market_hint?: string | null;
    };
  };
  cross_checks?: VisionCheck[];
  ambiguities?: string[];
}

export interface VisionResponse {
  route?: string;
  fallback_reason?: string;
  local_analysis?: LocalVisionAnalysis;
  visual_hypotheses?: { label?: string; text?: string; model?: string; estimated_cost_usd?: number | null } | null;
}

export type StrategyRule = Record<string, Json>;

export interface RulePreview {
  name?: string;
  rule_hash?: string;
  when?: string;
  condition?: string;
  then?: string;
  cooldown_bars?: number;
  timeframes_used?: string[];
  bar_finality?: string;
  attribution?: string;
}

export interface CompileResponse {
  status: string;
  assumptions?: string[];
  unresolved?: string[];
  rejected_reasons?: string[];
  clauses?: { text: string; kind: string }[];
  rule?: StrategyRule | null;
  preview?: RulePreview | null;
  activation?: string;
  llm_proposal?: (Partial<CompileResponse> & { reason?: string; model?: string }) | null;
}

export interface DryRunResponse {
  preview?: RulePreview;
  current?: {
    rule_hash?: string;
    result?: string;
    action?: string;
    as_of_utc?: string;
    bar_close_times?: Record<string, string | null>;
    leaves?: { condition?: string; result?: Json; left?: Json; right?: Json; observed?: Json }[];
    note?: string;
  };
  replay?: { bars?: number; counts?: Record<string, number>; recent_matches?: (string | null)[]; note?: string };
}

export interface StrategyVersion {
  version: number;
  status: string;
  rule_hash: string;
  rule: StrategyRule;
  prompts: string[];
  approved_at: number | null;
}

export interface Strategy {
  id: string;
  name: string;
  kind: string;
  parent_methodology: string | null;
  versions: StrategyVersion[];
  label: string;
}

export interface StrategiesResponse {
  strategies: Strategy[];
}

export interface CreateStrategyResponse {
  strategy_id: string;
  version: number;
  status: string;
  preview?: RulePreview;
  next?: string;
}

export interface Alert {
  seq: number;
  event_id: string;
  instrument_key: string;
  timeframe: string;
  source: string;
  event_type: string;
  bar_open_time_ms: number;
  priority: number;
  created_at: number;
  payload: Record<string, Json>;
}

export interface AlertsResponse {
  alerts: Alert[];
}

export interface UsageRow {
  feature: string;
  provider: string;
  model: string;
  n: number;
  i: number | null;
  o: number | null;
  c: number | null;
  unknown_cost: number | null;
  lat: number | null;
}

export interface ReportSummary {
  id: string;
  created_at: number;
  instrument_key: string;
  title: string;
}

export interface ReportsResponse {
  reports: ReportSummary[];
}

export interface UsageResponse {
  since_ms: number;
  cache_hits?: number;
  cache_hit_rate?: number | null;
  by_feature_model: UsageRow[];
  total_tokens: number;
  total_cost_usd: number;
  calls_with_unknown_cost: number;
  monthly_ai_token_budget: number;
  monthly_ai_budget_usd: number;
  note: string;
}

// ---- exchange accounts (R8) ----

export type Provenance = "exchange_reported" | "derived" | "estimate";

export interface KeyPermissions {
  read: boolean | null;
  spot_trade: boolean | null;
  derivatives_trade: boolean | null;
  margin_trade: boolean | null;
  withdraw: boolean | null;
  transfer: boolean | null;
  ip_restricted: boolean | null;
  expires_ms: number | null;
}

export interface ConnectionMeta {
  connection_id: string;
  exchange: string;
  environment: "live" | "testnet";
  label: string;
  lifetime: string;
  created_ms: number;
  expires_ms: number | null;
  key_hint: string;
  permissions: KeyPermissions;
  trading_enabled: boolean;
}

export interface ExchangeInfo {
  id: string;
  name: string;
  kind: string;
  environments: ("live" | "testnet")[];
  needs_passphrase: boolean;
  markets: string[];
  key_setup_url: string;
  oauth: string;
  notes: string[];
  key_label: string;
  secret_label: string;
}

export interface ExchangesResponse {
  exchanges: ExchangeInfo[];
  lifetimes: string[];
  default_lifetime: string;
}

export interface AccountOrder {
  order_id: string;
  client_order_id: string | null;
  symbol: string;
  market: string;
  side: "buy" | "sell";
  type: string;
  status: string;
  price: string | null;
  quantity: string | null;
  filled_quantity: string | null;
  average_price: string | null;
}

export interface AccountSnapshot {
  connection_id: string;
  exchange: string;
  environment: "live" | "testnet";
  as_of_ms: number;
  totals: { wallet: string; equity: string | null; available_balance: string | null; unrealized_pnl: string | null; maintenance_margin: string | null }[];
  balances: { asset: string; wallet: string; free: string | null; locked: string | null; total: string | null }[];
  positions: unknown[];
  open_orders: AccountOrder[];
  unavailable: string[];
  warnings: string[];
}

export interface PortfolioPosition {
  exchange: string;
  symbol: string;
  side: "long" | "short";
  quantity: string;
  notional_usd: string | null;
  unrealized_pnl: string | null;
  liquidation_distance_pct: string | null;
  leverage: string | null;
}

export interface PortfolioResponse {
  total_value_usd: { value: string; provenance: Provenance; note: string };
  by_exchange: Record<string, string>;
  by_wallet: Record<string, string>;
  allocation: { asset: string; exposure_usd: string; share_pct: string | null }[];
  exposure_by_direction: { long: string; short: string };
  net_exposure_usd: string;
  positions: PortfolioPosition[];
  margin: { exchange: string; wallet: string; maintenance_margin_pct_of_equity: string }[];
  unpriced_assets: string[];
  warnings: string[];
  connections: { connection_id: string; exchange: string; environment: string; as_of_ms: number; unavailable: string[] }[];
  errors: { connection_id: string; exchange: string; error: string }[];
  environment: "live" | "testnet";
  available_environments: ("live" | "testnet")[];
}

export interface PerformanceResponse {
  symbol: string;
  market: string;
  trades: number;
  win_rate_pct?: string;
  net_pnl?: string;
  average_win?: string | null;
  average_loss?: string | null;
  expectancy_per_trade?: string;
  profit_factor?: string | null;
  max_drawdown?: string;
  fees_paid?: string;
  fills_with_fees_in_other_assets?: number;
  unmatched_sells?: number;
  provenance?: string[];
  warning?: string;
  note?: string;
}

export interface OrderDraft {
  symbol: string;
  market: "spot" | "usdm_perpetual" | "stock";
  side: "buy" | "sell";
  type: "market" | "limit";
  quantity: string;
  price?: string;
  reduce_only?: boolean;
}

export interface TradeEstimate {
  value: string | null;
  asset?: string;
  provenance: Provenance;
  note?: string;
  rate?: string;
  leverage?: string;
}

export interface TradePreview {
  preview_id: string;
  confirm_token: string;
  connection_id: string;
  exchange: string;
  environment: "testnet";
  order: Record<string, string | boolean | null>;
  checks: { name: string; ok: boolean; detail: string }[];
  estimates: Record<string, TradeEstimate | Record<string, string>>;
  warnings: string[];
  expires_in_s: number;
}

export interface TradeResult {
  client_order_id: string;
  state: string;
  order?: AccountOrder;
  note?: string;
}

export interface JournalRow {
  client_order_id: string;
  connection_id: string;
  exchange: string;
  environment: string;
  state: string;
  message: string | null;
  request_json: string;
  created_at: number;
}

export interface JournalResponse {
  orders: JournalRow[];
  needs_reconcile: string[];
}
