// Instrument freshness, multi-timeframe summary and methodology perspectives (GET /api/v1/analysis).
// Perspectives are shown one by one: no BUY/SELL badge, no vote or aggregate across frameworks.

import type { App, View } from "../app.js";
import { badge, h, list, setChildren, status } from "../dom.js";
import { fmtAge, fmtJson, fmtNum } from "../format.js";
import { marketLabel } from "../symbol.js";
import type { Alignment, AnalysisResponse, MarketEvent, Perspective, Snapshot, TimeframeReport } from "../types.js";

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

export function eventsList(events: MarketEvent[] | undefined): HTMLElement {
  if (!events || events.length === 0) return h("p", { class: "muted" }, "No events on the last closed bar.");
  return h(
    "ul",
    { class: "plain events" },
    events.map((e) =>
      h(
        "li",
        null,
        h("strong", null, e.type),
        e.bar_close_time_utc ? ` · bar closed ${e.bar_close_time_utc}` : "",
        e.invalidation ? h("div", { class: "small" }, `Invalidation: ${e.invalidation}`) : null,
      ),
    ),
  );
}

function timeframeCard(tf: string, r: TimeframeReport, now: number): HTMLElement {
  const iv = r.indicator_values ?? {};
  const head = h(
    "header",
    { class: "card-head" },
    h("h4", null, tf),
    badge(r.status, tfTone(r.status)),
    r.trend?.state ? badge(`trend ${r.trend.state}`, "muted") : null,
  );
  if (r.status === "PROVIDER_ERROR") {
    return h("article", { class: "card" }, head, status("error", r.error || "Provider error for this timeframe."));
  }
  return h(
    "article",
    { class: "card" },
    head,
    kv([
      ["Bar close", `${r.bar_close_time_utc ?? "—"} ${r.bar_close_time_utc ? `(${fmtAge(r.bar_close_time_utc, now)})` : ""}`],
      ["Close", fmtNum(r.last_close)],
      ["SMA50", fmtNum(iv["sma50"])],
      ["SMA200", fmtNum(iv["sma200"])],
      ["RSI14 (Wilder)", fmtNum(iv["rsi14_wilder"], 4)],
      ["Volume ratio (20)", fmtNum(iv["volume_ratio_20"], 4)],
    ]),
    r.trend?.reason ? h("p", { class: "small muted" }, r.trend.reason) : null,
    h("h5", null, "Events"),
    eventsList(r.events),
    r.missing_inputs && r.missing_inputs.length
      ? h("details", null, h("summary", null, `Data notes (${r.missing_inputs.length})`), list(r.missing_inputs))
      : null,
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

function snapshotHeader(snap: Snapshot, atf: string, now: number): HTMLElement {
  const inst = snap.instrument ?? {};
  const reports = Object.values(snap.timeframes ?? {});
  const stale = reports.filter((r) => r.status === "STALE_DATA").length;
  const errors = reports.filter((r) => r.status === "PROVIDER_ERROR").length;
  return h(
    "section",
    { class: "card", "aria-label": "Data freshness" },
    h(
      "header",
      { class: "card-head" },
      h("h3", null, inst.symbol ?? "—"),
      badge(inst.market_type === "spot" ? marketLabel("spot") : marketLabel("usdm_perpetual"), "info"),
      stale ? badge(`STALE ×${stale}`, "err") : null,
      errors ? badge(`PROVIDER_ERROR ×${errors}`, "err") : null,
    ),
    kv([
      ["Venue", inst.venue ?? "—"],
      ["As of", `${snap.as_of_utc ?? "—"} (${fmtAge(snap.as_of_ms, now)})`],
      ["Analysis timeframe", atf],
      ["Source", snap.source ?? "—"],
    ]),
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
    const tfCards = Object.entries(snap.timeframes ?? {}).map(([tf, r]) => timeframeCard(tf, r, now));
    const persp = data.perspectives?.perspectives ?? [];
    return [
      snapshotHeader(snap, atf, now),
      h(
        "section",
        { "aria-labeledby": "mtf-h" },
        h("h3", { id: "mtf-h" }, "Multi-timeframe summary"),
        alignmentBlock(snap.alignment),
        h("div", { class: "cards" }, tfCards),
      ),
      h(
        "section",
        { "aria-labeledby": "persp-h" },
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
      list(a.reasons),
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
