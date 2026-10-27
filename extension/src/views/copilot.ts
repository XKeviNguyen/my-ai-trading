// Copilot chat, Challenge My Thesis and optional chart-screenshot vision.
// Deterministic facts are always shown separately from model interpretation (untrusted text).

import { TIMEFRAMES } from "../api.js";

import type { App, View } from "../app.js";
import { badge, field, h, list, setChildren, status } from "../dom.js";
import { fmtInt, fmtNum, fmtUsd, statusLabel } from "../format.js";
import type { AskResponse, ChallengeResponse, FactRow, Interpretation, LocalVisionAnalysis, VisionResponse } from "../types.js";
import { alignmentBlock } from "./analysis.js";

export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"];

/** Client-side screenshot check; the server re-validates magic bytes. */
export function checkImage(file: { type: string; size: number } | null | undefined): string | null {
  if (!file) return "Choose an image file.";
  if (!IMAGE_TYPES.includes(file.type)) return "Only PNG, JPEG or WEBP screenshots are accepted.";
  if (file.size <= 0) return "The file is empty.";
  if (file.size > MAX_IMAGE_BYTES) return "Image is larger than 5 MB.";
  return null;
}

function modelText(
  title: string,
  m: { label?: string; text?: string; model?: string; estimated_cost_usd?: number | null } & Partial<Interpretation>,
): HTMLElement {
  const meta: string[] = [];
  if (m.model) meta.push(`model ${m.model}`);
  if (typeof m.input_tokens === "number" || typeof m.output_tokens === "number") {
    meta.push(`tokens in ${fmtInt(m.input_tokens)} / out ${fmtInt(m.output_tokens)}`);
  }
  meta.push(`est. cost ${fmtUsd(m.estimated_cost_usd)}`);
  if (typeof m.latency_ms === "number") meta.push(`${fmtInt(m.latency_ms)} ms`);
  return h(
    "section",
    { class: "card model-output" },
    h("h4", null, title),
    h("p", { class: "mode-label" }, m.label ?? "Model output — unverified"),
    h("div", { class: "model-text" }, m.text ?? ""),
    h("p", { class: "small muted" }, meta.join(" · ")),
  );
}

function fallback(route: string | undefined, reason: string | undefined): HTMLElement | null {
  if (!reason) return null;
  return h(
    "p",
    { class: "status status-info", role: "status" },
    `Route: ${route ?? "deterministic"}. AI interpretation not used: ${reason}`,
  );
}

const FACT_STATUSES = ["OK", "INSUFFICIENT_DATA", "STALE_DATA"];

/** Deterministic facts as one table, timeframes as columns (same reading pattern as the Analysis tab). */
function factsTable(rows: FactRow[]): HTMLElement {
  const ok = (t: FactRow) => !t.error && (!t.status || FACT_STATUSES.includes(t.status));
  const lines: [string, (t: FactRow) => string | HTMLElement][] = [
    ["Status", (t) => badge(statusLabel(t.status), ok(t) ? (t.status === "OK" ? "ok" : "warn") : "err")],
    ["Trend", (t) => t.trend ?? "—"],
    ["Close", (t) => fmtNum(t.close)],
    ["SMA50", (t) => fmtNum(t.sma50)],
    ["SMA200", (t) => fmtNum(t.sma200)],
    ["RSI14", (t) => fmtNum(t.rsi14, 4)],
    ["Vol ratio", (t) => fmtNum(t.volume_ratio_20, 3)],
    ["Events", (t) => String(Array.isArray(t.events) ? t.events.length : 0)],
  ];
  return h(
    "div",
    { class: "table-wrap", tabindex: "0", role: "region", "aria-label": "Deterministic facts by timeframe" },
    h(
      "table",
      { class: "matrix" },
      h("caption", { class: "sr-only" }, "Deterministic facts by timeframe"),
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "Fact"), rows.map((t) => h("th", { scope: "col" }, t.timeframe)))),
      h(
        "tbody",
        null,
        lines.map(([label, cell]) =>
          h("tr", null, h("th", { scope: "row" }, label), rows.map((t) => h("td", null, ok(t) || label === "Status" ? cell(t) : "—"))),
        ),
      ),
    ),
  );
}

function renderAsk(r: AskResponse): HTMLElement[] {
  const f = r.facts ?? {};
  const tfRows = f.timeframes ?? [];
  const details = tfRows.flatMap((t) => [
    t.error ? `${t.timeframe}: ${t.status ?? "error"} ${t.error}` : null,
    Array.isArray(t.events) && t.events.length ? `${t.timeframe} events: ${t.events.join(", ")}` : null,
    t.bar_close_time_utc ? `${t.timeframe} bar closed ${t.bar_close_time_utc}` : null,
  ]).filter((x): x is string => x !== null);
  const persp = (f.perspectives ?? []).map(
    (p) => `${p.methodology ?? ""}: ${p.status ?? ""} — ${p.label ?? ""} — eval ${p.evaluation_timeframe ?? "none"} — ${p.state ?? "—"}`,
  );
  return [
    h(
      "section",
      { class: "card facts" },
      h("h4", null, "Facts (deterministic)"),
      h("p", { class: "mode-label" }, f.label ?? "Observed facts"),
      r.snapshot_as_of_utc ? h("p", { class: "small muted" }, `Snapshot as of ${r.snapshot_as_of_utc}`) : null,
      tfRows.length ? factsTable(tfRows) : h("p", { class: "muted" }, "No timeframe facts."),
      details.length ? h("details", null, h("summary", null, `Bar times and events (${details.length})`), list(details)) : null,
      alignmentBlock(f.alignment),
      persp.length ? [h("h5", null, "Perspectives (each on its own)"), list(persp)] : null,
    ),
    r.interpretation ? modelText("Interpretation", r.interpretation) : h("p", { class: "small muted" }, `Route: ${r.route ?? "deterministic"}`),
    fallback(r.route, r.fallback_reason) as HTMLElement,
  ].filter(Boolean) as HTMLElement[];
}

function renderChallenge(r: ChallengeResponse): HTMLElement[] {
  const a = r.audit ?? {};
  const sec = (title: string, items: string[] | undefined, cls = "") =>
    h("section", { class: `card ${cls}` }, h("h4", null, title), list(items));
  return [
    h(
      "section",
      { class: "card" },
      h("h4", null, "Assessment"),
      h("p", null, badge(a.assessment ?? "—", a.assessment === "MIXED_EVIDENCE" ? "warn" : "muted"), ` direction: ${a.direction ?? "unknown"} · focus ${a.focus_timeframe ?? "—"}`),
      a.assessment_note ? h("p", { class: "small" }, a.assessment_note) : null,
      a.method ? h("p", { class: "small muted" }, a.method) : null,
    ),
    sec("Supporting evidence", a.supporting_evidence, "support"),
    sec("Contradicting evidence", a.contradicting_evidence, "contra"),
    sec("Assumptions", a.assumptions),
    sec("Unknowns", a.unknowns),
    sec("Would falsify the thesis", a.falsification_conditions),
    sec("Proposed tests", a.proposed_tests),
    h("details", null, h("summary", null, `Observations (${a.observations?.length ?? 0})`), list(a.observations)),
    r.debate ? modelText("Deep debate", r.debate) : null,
    fallback(r.route, r.fallback_reason),
  ].filter(Boolean) as HTMLElement[];
}

function checkTone(result: string | undefined): "ok" | "warn" | "err" | "muted" {
  if (result === "MATCH" || result === "AGREES") return "ok";
  if (result === "MISMATCH" || result === "DISAGREES") return "err";
  return "muted";
}

/** Local OpenCV/OCR result: zero-token visual hypotheses, cross-checked against exchange data. */
export function renderLocalVision(l: LocalVisionAnalysis | undefined): HTMLElement | null {
  if (!l) return null;
  if (l.status !== "OK") {
    return h("section", { class: "card" }, h("h3", null, "Local image analysis"),
      h("p", { class: "muted" }, `${l.status ?? "UNAVAILABLE"}: ${l.reason ?? ""}`));
  }
  const g = l.geometry ?? {};
  const o = l.ocr ?? {};
  const p = o.parsed ?? {};
  const geometry = g.state === "DETECTED"
    ? `${fmtInt(g.candles_detected)} candle shapes (${fmtInt(g.green)} green / ${fmtInt(g.red)} red), visual drift ${g.visual_drift}, last visible candle ${g.last_visible_candle_color}`
    : (g.note ?? g.state ?? "not detected");
  const ocrLines = o.status === "OK"
    ? [
        `Symbols read: ${(p.symbols ?? []).join(", ") || "none"}`,
        `Timeframes read: ${(p.timeframes ?? []).join(", ") || "none"}`,
        `Indicator labels: ${(p.indicator_labels ?? []).join(", ") || "none"}`,
        `Price-axis labels: ${(p.price_axis_labels ?? []).map((x) => fmtNum(x)).join(", ") || "none"}`,
        `Mean OCR confidence: ${fmtNum(o.mean_confidence ?? null)} (${o.confidence_note ?? "uncalibrated"})`,
      ]
    : [`OCR ${o.status ?? "unavailable"}: ${o.reason ?? ""}`];
  return h("section", { class: "card" },
    h("h3", null, "Local image analysis ", badge("0 tokens", "info")),
    h("p", { class: "small muted" }, l.label ?? ""),
    h("p", null, h("strong", null, "Shapes: "), geometry),
    list(ocrLines),
    h("h4", null, "Checks against exchange data"),
    h("ul", { class: "plain" }, (l.cross_checks ?? []).map((c) =>
      h("li", null, badge(c.result ?? "?", checkTone(c.result)), " ", c.check ?? "",
        c.reason ? ` — ${c.reason}` : "", c.note ? ` — ${c.note}` : ""))),
    (l.ambiguities ?? []).length ? list(l.ambiguities) : null,
    h("p", { class: "small muted" }, l.privacy ?? ""));
}

function renderVision(r: VisionResponse): HTMLElement[] {
  return [
    renderLocalVision(r.local_analysis),
    r.visual_hypotheses ? modelText("Visual hypotheses", r.visual_hypotheses) : null,
    fallback(r.route, r.fallback_reason),
  ].filter(Boolean) as HTMLElement[];
}

export class CopilotView implements View {
  private readonly app: App;
  private readonly instLine: HTMLElement;

  constructor(root: HTMLElement, app: App) {
    this.app = app;
    this.instLine = h("p", { class: "small muted" });
    setChildren(root, this.instLine, this.askSection(), this.challengeSection(), this.visionSection());
    this.onInstrument();
  }

  onInstrument(): void {
    const i = this.app.instrument;
    this.instLine.textContent = i
      ? `Questions use ${i.symbol} (${i.market}) and your saved timeframes.`
      : "Select an instrument above first.";
  }

  private needInstrument(out: HTMLElement): { symbol: string; market: "usdm_perpetual" | "spot" } | null {
    const i = this.app.instrument;
    if (!i) setChildren(out, status("error", "Select an instrument first."));
    return i;
  }

  private deepToggle(): HTMLInputElement {
    return h("input", { type: "checkbox" });
  }

  private askSection(): HTMLElement {
    const q = h("textarea", { rows: 3, maxlength: 4000, required: true, placeholder: "e.g. Is 4h momentum fading against the 1d trend?" });
    const deep = this.deepToggle();
    const out = h("div", { "aria-live": "polite" });
    const btn = h("button", { type: "submit", class: "primary" }, "Ask");
    const form = h(
      "form",
      { class: "stack" },
      field("Question", q),
      h("label", { class: "check" }, deep, " Deep analysis (higher effort, more tokens; needs opt-in in Settings)"),
      btn,
      out,
    );
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const inst = this.needInstrument(out);
      const question = q.value.trim();
      if (!inst) return;
      if (!question) return void setChildren(out, status("error", "Type a question."));
      btn.disabled = true;
      setChildren(out, status("loading", "Asking…"));
      this.app.client
        .ask(inst.symbol, inst.market, question, deep.checked)
        .then((r) => setChildren(out, ...renderAsk(r)))
        .catch((err) => setChildren(out, this.app.errorView(err)))
        .finally(() => (btn.disabled = false));
    });
    return h("section", { "aria-labelledby": "ask-h" }, h("h3", { id: "ask-h" }, "Copilot"), form);
  }

  private challengeSection(): HTMLElement {
    const t = h("textarea", { rows: 3, maxlength: 4000, placeholder: "e.g. Bullish: 4h breakout will continue above the 1d range." });
    const focus = h(
      "select",
      null,
      h("option", { value: "" }, "Auto (smallest)"),
      TIMEFRAMES.map((tf) => h("option", { value: tf }, tf)),
    );
    const deep = this.deepToggle();
    const out = h("div", { "aria-live": "polite" });
    const btn = h("button", { type: "submit", class: "primary" }, "Challenge my thesis");
    const form = h(
      "form",
      { class: "stack" },
      field("Thesis", t, "State the direction (bullish/bearish) and what you expect."),
      field("Focus timeframe", focus),
      h("label", { class: "check" }, deep, " Add deep bull/bear debate (uses tokens; needs opt-in)"),
      btn,
      out,
    );
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const inst = this.needInstrument(out);
      const thesis = t.value.trim();
      if (!inst) return;
      if (!thesis) return void setChildren(out, status("error", "Write your thesis first."));
      btn.disabled = true;
      setChildren(out, status("loading", "Auditing evidence…"));
      this.app.client
        .challenge(inst.symbol, inst.market, thesis, focus.value || null, deep.checked)
        .then((r) => setChildren(out, ...renderChallenge(r)))
        .catch((err) => setChildren(out, this.app.errorView(err)))
        .finally(() => (btn.disabled = false));
    });
    return h("section", { "aria-labelledby": "ch-h" }, h("h3", { id: "ch-h" }, "Challenge My Thesis"), form);
  }

  private visionSection(): HTMLElement {
    const file = h("input", { type: "file", accept: IMAGE_TYPES.join(",") });
    const q = h("textarea", { rows: 2, maxlength: 4000, placeholder: "Optional question about the screenshot" });
    const withSym = h("input", { type: "checkbox", checked: true });
    const out = h("div", { "aria-live": "polite" });
    const btn = h("button", { type: "submit" }, "Analyse screenshot");
    const form = h(
      "form",
      { class: "stack" },
      field("Chart screenshot (PNG/JPEG/WEBP, ≤ 5 MB)", file, "You choose the file; the extension never captures the page."),
      field("Question", q),
      h("label", { class: "check" }, withSym, " Compare with exchange data for the current instrument"),
      btn,
      out,
    );
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const f = file.files?.[0] ?? null;
      const problem = checkImage(f);
      if (problem || !f) return void setChildren(out, status("error", problem ?? "Choose an image file."));
      const fd = new FormData();
      fd.append("image", f, f.name);
      fd.append("question", q.value.trim());
      const inst = this.app.instrument;
      if (withSym.checked && inst) {
        fd.append("symbol", inst.symbol);
        fd.append("market", inst.market);
      }
      btn.disabled = true;
      setChildren(out, status("loading", "Uploading to the local engine…"));
      this.app.client
        .vision(fd)
        .then((r) => setChildren(out, ...renderVision(r)))
        .catch((err) => setChildren(out, this.app.errorView(err)))
        .finally(() => (btn.disabled = false));
    });
    return h(
      "details",
      { class: "section-details" },
      h("summary", null, h("h3", { class: "inline" }, "Chart screenshot (optional)")),
      h("p", { class: "small muted" }, "Sends the image to the AI provider via the local engine only if AI consent is on. Visual output is a hypothesis, not exact OHLCV."),
      form,
    );
  }
}
