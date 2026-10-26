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
  missing_inputs?: string[];
  data_quality?: { closed_bars?: number; contiguous_bars?: number; gaps?: number; missing_bars?: number; stale?: boolean };
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

export interface VisionResponse {
  route?: string;
  fallback_reason?: string;
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

export interface UsageResponse {
  since_ms: number;
  by_feature_model: UsageRow[];
  total_tokens: number;
  total_cost_usd: number;
  calls_with_unknown_cost: number;
  monthly_ai_token_budget: number;
  monthly_ai_budget_usd: number;
  note: string;
}
