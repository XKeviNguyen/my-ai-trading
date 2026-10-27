// Instrument freshness, multi-timeframe summary and methodology perspectives (GET /api/v1/analysis).
// Perspectives are shown one by one: no BUY/SELL badge, no vote or aggregate across frameworks.

import type { App, View } from "../app.js";
import { badge, h, list, setChildren, status } from "../dom.js";
import { fmtAge, fmtJson, fmtMs, fmtNum, statusLabel } from "../format.js";
import { marketLabel } from "../symbol.js";
import type { Alignment, AnalysisResponse, MarketEvent, Perspective, Snapshot, TimeframeReport, PatternDetection } from "../types.js";

export function tfTone(s: string | undefined): "ok" | "warn" | "err" | "muted" {
  if (s === "OK") return "ok";
  if (s === "INSUFFICIENT_DATA") return "warn";
  if (s === "STALE_DATA" || s === "PROVIDER_ERROR") return "err";
  return "muted";
}

function applicabilityTone(s: string): "ok" | "warn" | "err" | "muted" | "info" {
  switch (s) {
    case "SUPPORTED_NATIVE":
      return "ok";
    case "SUPPORTED_HYBRID":
      return "info";
    case "ADAPTED_UNVALIDATED":
    case "INSUFFICIENT_DATA":
      return "warn";
    case "STALE_DATA":
      return "err";
    default:
      return "muted";
  }
}

function kv(rows: [string, string | HTMLElement][]): HTMLElement {
  return h(
    "dl",
    { class: "kv" },
    rows.map(([k, v]) => [h("dt", null, k), h("dd", null, v)]),
  );
}

/** Recent geometric pattern detections; always shown with their caveat and invalidation. */
export function patternsList(patterns: PatternDetection[] | undefined): HTMLElement | null {
  if (!patterns || patterns.length === 0) return null;
  return h(
    "details",
    null,
    h("summary", null, `Patterns (${patterns.length}) — geometric detections, not signals`),
    h("p", { class: "small muted" }, "These detectors also fire on random data; treat them as prompts to look."),
    h("ul", { class: "plain" }, patterns.map((p) =>
      h("li", null, badge(p.state, p.state === "FAILED" ? "muted" : "info"), " ", p.pattern.replaceAll("_", " ").toLowerCase(),
        p.invalidation ? invalidation(p.invalidation) : null))),
  );
}

export function eventsList(events: MarketEvent[] | undefined): HTMLElement {
  if (!events || events.length === 0) return h("p", { class: "muted" }, "No events on the last closed bar.");
  return h(
    "ul",
    { class: "plain events event-list" },
    events.map((e) =>
      h(
        "li",
        null,
        h(
          "div",
          { class: "event-head" },
          h("span", { class: "event-type" }, e.type),
          e.bar_close_time_utc ? h("span", { class: "small muted" }, `bar closed ${e.bar_close_time_utc}`) : null,
        ),
        e.invalidation ? invalidation(e.invalidation) : null,
      ),
    ),
  );
}

/** Invalidation is risk information: always in the risk lane, never styled like a signal. */
export function invalidation(text: string): HTMLElement {
  return h("p", { class: "invalidation" }, h("b", null, "Invalidation: "), text);
}

type Row = [label: string, cell: (r: TimeframeReport, now: number) => string | HTMLElement, group?: boolean];

function trendCell(state: string | undefined): HTMLElement | string {
  if (state === "UP") return h("span", { class: "trend-up" }, "Up");
  if (state === "DOWN") return h("span", { class: "trend-down" }, "Down");
  if (state === "MIXED") return "Mixed";
  return "—";
}

const iv = (r: TimeframeReport, k: string, digits = 6): string => fmtNum(r.indicator_values?.[k], digits);

const MATRIX_ROWS: Row[] = [
  ["Status", (r) => badge(statusLabel(r.status), tfTone(r.status))],
  ["Trend", (r) => trendCell(r.trend?.state)],
  ["Close", (r) => fmtNum(r.last_close), true],
  ["SMA50", (r) => iv(r, "sma50")],
  ["SMA200", (r) => iv(r, "sma200")],
  ["RSI14", (r) => iv(r, "rsi14_wilder", 4), true],
  ["MACD hist", (r) => iv(r, "macd_histogram")],
  ["Boll. %B", (r) => iv(r, "bollinger_20_2_percent_b", 4)],
  ["ADX14", (r) => iv(r, "adx14", 4)],
  ["+DI / −DI", (r) => `${iv(r, "plus_di14", 4)} / ${iv(r, "minus_di14", 4)}`],
  ["Vol ratio", (r) => iv(r, "volume_ratio_20", 4)],
  ["Bar close UTC", (r) => (r.bar_close_time_utc ? fmtMs(Date.parse(r.bar_close_time_utc)).slice(5, 16) : "—"), true],
  ["Bar age", (r, now) => (r.bar_close_time_utc ? fmtAge(r.bar_close_time_utc, now) || "—" : "—")],
];

/** All timeframes side by side so values can be compared across a row. */
export function timeframeMatrix(tfs: [string, TimeframeReport][], now: number): HTMLElement {
  if (tfs.length === 0) return h("p", { class: "muted" }, "No timeframes in this snapshot.");
  return h(
    "div",
    { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Indicator values by timeframe" },
    h(
      "table",
      { class: "matrix" },
      h("caption", { class: "sr-only" }, "Indicator values by timeframe, from the last closed bar of each timeframe"),
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "Metric"), tfs.map(([tf]) => h("th", { scope: "col" }, tf)))),
      h(
        "tbody",
        null,
        MATRIX_ROWS.map(([label, cell, group]) =>
          h(
            "tr",
            { class: group ? "group" : null },
            h("th", { scope: "row" }, label),
            tfs.map(([, r]) => h("td", null, r.status === "PROVIDER_ERROR" && label !== "Status" ? "—" : cell(r, now))),
          ),
        ),
      ),
    ),
  );
}

/** Events, patterns, provider errors and data notes, only for timeframes that have any. */
function timeframeNotes(tf: string, r: TimeframeReport): HTMLElement | null {
  const events = r.events ?? [];
  const notes = r.missing_inputs ?? [];
  if (r.status === "PROVIDER_ERROR") {
    return h("article", { class: "card lane lane-fault" }, h("h4", null, tf), status("error", r.error || "Provider error for this timeframe."));
  }
  const reason = r.trend?.reason;
  if (!events.length && !r.patterns?.length && !notes.length && !reason) return null;
  return h(
    "article",
    { class: `card lane${events.length ? " lane-event" : ""}` },
    h("header", { class: "card-head" }, h("h4", null, tf), events.length ? badge(`${events.length} event${events.length > 1 ? "s" : ""}`, "muted") : null),
    events.length ? eventsList(events) : null,
    reason ? h("p", { class: "small muted" }, `Trend: ${reason}`) : null,
    patternsList(r.patterns),
    notes.length ? h("details", null, h("summary", null, `Data notes (${notes.length})`), list(notes)) : null,
  );
}

export function alignmentBlock(a: Alignment | undefined): HTMLElement {
  if (!a) return h("p", { class: "muted" }, "No alignment data.");
  const tfs = Object.entries(a.trend_by_timeframe ?? {}).map(([tf, s]) => `${tf}: ${s}`);
  return h(
    "div",
    { class: "alignment" },
    h("p", null, "Alignment: ", badge(a.verdict ?? "UNKNOWN", a.verdict === "CONFLICTING" ? "warn" : "muted")),
    tfs.length ? h("p", { class: "small" }, tfs.join(" · ")) : null,
    a.note ? h("p", { class: "small muted" }, a.note) : null,
  );
}

function headlineBar(main: TimeframeReport | undefined, atf: string, now: number): string {
  if (!main) return `${atf} is not in this snapshot.`;
  if (main.status === "PROVIDER_ERROR") return `${atf} data could not be loaded.`;
  if (!main.bar_close_time_utc) return `No closed ${atf} bar yet.`;
  const age = fmtAge(main.bar_close_time_utc, now);
  return `Last ${atf} bar closed ${fmtMs(Date.parse(main.bar_close_time_utc))}${age ? ` (${age})` : ""}.`;
}

function headline(snap: Snapshot, atf: string, now: number): HTMLElement {
  const inst = snap.instrument ?? {};
  const reports = Object.values(snap.timeframes ?? {});
  const stale = reports.filter((r) => r.status === "STALE_DATA").length;
  const errors = reports.filter((r) => r.status === "PROVIDER_ERROR").length;
  const main = snap.timeframes?.[atf];
  return h(
    "section",
    { class: "headline", "aria-label": "Instrument and data freshness" },
    h(
      "div",
      { class: "headline-top" },
      h("h2", { class: "headline-symbol" }, inst.symbol ?? "—"),
      badge(inst.market_type === "spot" ? marketLabel("spot") : marketLabel("usdm_perpetual"), "info"),
      Number.isFinite(main?.last_close)
        ? h("p", { class: "headline-close" }, h("span", { class: "headline-close-label" }, `Last ${atf} close`), fmtNum(main?.last_close))
        : null,
    ),
    h(
      "p",
      { class: "headline-meta" },
      stale ? badge(`Stale: ${stale}`, "err") : null,
      errors ? badge(`Provider error: ${errors}`, "err") : null,
      headlineBar(main, atf, now),
    ),
    h("p", { class: "headline-meta" }, `Data from ${inst.venue ?? "—"} (${snap.source ?? "unknown source"}), as of ${fmtMs(snap.as_of_ms)}.`),
  );
}

export class AnalysisView implements View {
  private readonly root: HTMLElement;
  private readonly app: App;
  private seq = 0;
  private loadedKey = "";

  constructor(root: HTMLElement, app: App) {
    this.root = root;
    this.app = app;
    this.renderIdle();
  }

  private renderIdle(): void {
    setChildren(
      this.root,
      h("p", { class: "muted" }, "Choose an instrument above (open a Binance futures/spot page or type a symbol)."),
    );
  }

  show(): void {
    const key = this.key();
    if (key && key !== this.loadedKey) void this.load();
  }

  onInstrument(): void {
    if (!this.root.hidden) void this.load();
    else this.loadedKey = "";
  }

  private key(): string {
    const i = this.app.instrument;
    return i ? `${i.symbol}|${i.market}|${this.app.analysisTf}` : "";
  }

  async load(): Promise<void> {
    const inst = this.app.instrument;
    if (!inst) return this.renderIdle();
    const my = ++this.seq;
    this.loadedKey = this.key();
    const refresh = h("button", { type: "button", onclick: () => void this.load() }, "Refresh");
    setChildren(this.root, status("loading", `Loading ${inst.symbol} analysis…`));
    try {
      const data = await this.app.client.analysis(inst.symbol, inst.market, this.app.analysisTf);
      if (my !== this.seq) return;
      setChildren(this.root, h("div", { class: "toolbar" }, refresh), ...this.render(data));
    } catch (err) {
      if (my !== this.seq) return;
      this.loadedKey = "";
      setChildren(this.root, h("div", { class: "toolbar" }, refresh), this.app.errorView(err));
    }
  }

  private render(data: AnalysisResponse): HTMLElement[] {
    const now = Date.now();
    const snap = data.snapshot ?? {};
    const atf = data.perspectives?.analysis_timeframe ?? this.app.analysisTf;
    const tfs = Object.entries(snap.timeframes ?? {});
    const notes = tfs.map(([tf, r]) => timeframeNotes(tf, r)).filter((n): n is HTMLElement => n !== null);
    const persp = data.perspectives?.perspectives ?? [];
    return [
      headline(snap, atf, now),
      h(
        "section",
        { "aria-labelledby": "mtf-h" },
        h("h3", { id: "mtf-h" }, "Timeframes"),
        timeframeMatrix(tfs, now),
        alignmentBlock(snap.alignment),
      ),
      h(
        "section",
        { "aria-labelledby": "events-h" },
        h("h3", { id: "events-h" }, "Events, patterns and data notes"),
        notes.length ? h("div", { class: "cards" }, notes) : h("p", { class: "muted" }, "No events, patterns or data notes on the last closed bars."),
      ),
      h(
        "section",
        { "aria-labelledby": "persp-h" },
        h("h3", { id: "persp-h" }, `Methodology perspectives (${persp.length})`),
        persp.length === 0
          ? h(
              "div",
              { class: "empty" },
              h("p", null, "No methodology perspectives selected. That is a valid choice."),
              h("button", { type: "button", onclick: () => this.app.selectTab("methods") }, "Choose methodologies"),
            )
          : h("div", { class: "cards" }, persp.map((p) => this.perspectiveCard(p, atf))),
        data.perspectives?.aggregation ? h("p", { class: "small muted" }, `Aggregation: ${data.perspectives.aggregation}`) : null,
      ),
    ];
  }

  private perspectiveCard(p: Perspective, atf: string): HTMLElement {
    const a = p.assessment;
    const ev = p.evaluation ?? {};
    const checks = Object.entries(ev.checks ?? {});
    const routes = (a.offered_routes ?? []).filter((r) => r.mode !== a.requested_mode);
    const msg = h("div", { "aria-live": "polite" });
    return h(
      "article",
      { class: "card perspective" },
      h("header", { class: "card-head" }, h("h4", null, a.name), badge(a.status, applicabilityTone(a.status))),
      h("p", { class: "mode-label" }, a.label),
      kv([
        ["Requested mode", a.requested_mode],
        ["Evaluation timeframe", a.evaluation_timeframe ?? "not evaluated"],
        ["Analysis timeframe", a.analysis_timeframe || atf],
        ["Evaluation state", ev.state ?? "—"],
        ["Evidence / maturity", `${p.evidence_quality ?? "—"} / ${p.implementation_maturity ?? "—"}`],
      ]),
      ev.reason ? h("p", { class: "small" }, ev.reason) : null,
      ev.bar_close_time_utc ? h("p", { class: "small muted" }, `Evaluated bar closed ${ev.bar_close_time_utc}`) : null,
      checks.length
        ? h(
            "details",
            null,
            h("summary", null, `Checks (${checks.length})`),
            h("ul", { class: "plain" }, checks.map(([k, v]) => h("li", null, `${k}: ${fmtJson(v)}`))),
          )
        : null,
      ev.unevaluated_criteria && ev.unevaluated_criteria.length
        ? h("details", null, h("summary", null, "Unevaluated criteria"), list(ev.unevaluated_criteria))
        : null,
      p.tactical_layer
        ? h(
            "div",
            { class: "tactical" },
            h("p", { class: "mode-label" }, p.tactical_layer.label ?? "Custom tactical layer"),
            h(
              "p",
              { class: "small" },
              `${p.tactical_layer.timeframe ?? ""} · ${p.tactical_layer.status ?? ""} · trend ${p.tactical_layer.trend?.state ?? "—"}`,
            ),
            eventsList(p.tactical_layer.events),
          )
        : null,
      h("h5", null, "Reasons"),
      h("div", { class: "small" }, list(a.reasons)),
      a.requires_user_approval
        ? h("p", { class: "small warn-text" }, "This adaptation requires a preview and your approval before it runs.")
        : null,
      routes.length
        ? h(
            "div",
            { class: "routes" },
            h("h5", null, "Offered routes"),
            routes.map((r) =>
              h(
                "div",
                { class: "route" },
                h("p", { class: "small" }, `${r.mode}: ${r.description}`),
                r.mode === "CUSTOM"
                  ? h("button", { type: "button", onclick: () => this.app.selectTab("studio") }, "Open Strategy Studio")
                  : h(
                      "button",
                      { type: "button", onclick: () => void this.switchMode(a.methodology_id, r.mode, msg) },
                      `Use ${r.mode} mode`,
                    ),
              ),
            ),
            msg,
          )
        : null,
      p.provenance ? h("details", null, h("summary", null, "Provenance & limitations"), h("p", { class: "small" }, p.provenance), list(p.limitations, "No limitations listed.")) : null,
    );
  }

  private async switchMode(mid: string, mode: string, msg: HTMLElement): Promise<void> {
    if (mode !== "NATIVE" && mode !== "HYBRID" && mode !== "ADAPTED") return;
    setChildren(msg, status("loading", "Saving mode…"));
    try {
      await this.app.updateSettings((s) => {
        const sel = s.selected_methodologies.find((x) => x.methodology_id === mid);
        if (sel) sel.mode = mode;
      });
      await this.load();
    } catch (err) {
      setChildren(msg, this.app.errorView(err));
    }
  }
}
