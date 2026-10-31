// Position size from a risk budget (POST /api/v1/risk/size). Pure arithmetic on the local engine: no market data,
// no orders. The stop is an input and is never widened to fit; a stop too far away gives a smaller size.

import type { App, View } from "../app.js";
import { badge, field, h, setChildren, status } from "../dom.js";
import type { RiskSizeRequest, RiskSizeResponse } from "../types.js";

export const MAX_RISK_PCT = 2;
export const MIN_RISK_PCT = 0.01;
const NUM_RE = /^\d+(\.\d+)?$/;

export interface RiskInputs {
  capital: string;
  riskPct: string;
  entry: string;
  stop: string;
  target: string;
  step: string;
}

/** Field-level problems, keyed by input name; empty when the inputs can be sent. */
export function validateRisk(v: RiskInputs): Record<string, string> {
  const errors: Record<string, string> = {};
  const positive = (key: keyof RiskInputs, label: string, optional = false): number | null => {
    const raw = v[key].trim();
    if (!raw) {
      if (!optional) errors[key] = `${label} is required.`;
      return null;
    }
    if (!NUM_RE.test(raw) || Number(raw) <= 0) {
      errors[key] = `${label} must be a positive number, like 25000 or 0.8.`;
      return null;
    }
    return Number(raw);
  };
  positive("capital", "Capital");
  const r = positive("riskPct", "Risk per trade");
  const e = positive("entry", "Entry");
  const s = positive("stop", "Stop");
  const t = positive("target", "Target", true);
  positive("step", "Quantity step", true);
  if (r !== null && r > MAX_RISK_PCT) errors["riskPct"] = `Risk per trade is capped at ${MAX_RISK_PCT}%.`;
  if (r !== null && r < MIN_RISK_PCT) errors["riskPct"] = `Risk per trade must be at least ${MIN_RISK_PCT}%.`;
  if (e !== null && s !== null && e === s) errors["stop"] = "The stop must differ from the entry.";
  if (e !== null && s !== null && t !== null && e !== s) {
    const long = s < e;
    if ((long && t <= e) || (!long && t >= e)) {
      errors["target"] = long ? "For a long (stop below entry) the target must be above the entry."
        : "For a short (stop above entry) the target must be below the entry.";
    }
  }
  return errors;
}

/** Percent text ("0.8") to a fraction string ("0.008") by moving the decimal point, without float rounding. */
export function percentToFraction(pct: string): string {
  const [int = "0", frac = ""] = pct.trim().split(".");
  const digits = int + frac;
  const point = int.length - 2;
  const raw = point <= 0 ? `0.${"0".repeat(-point)}${digits}` : `${digits.slice(0, point)}.${digits.slice(point)}`;
  const [i = "0", f = ""] = raw.split(".");
  const intPart = i.replace(/^0+(?=\d)/, "");
  const fracPart = f.replace(/0+$/, "");
  return fracPart ? `${intPart}.${fracPart}` : intPart;
}

export function toRequest(v: RiskInputs): RiskSizeRequest {
  const body: RiskSizeRequest = {
    capital: v.capital.trim(), entry: v.entry.trim(), stop: v.stop.trim(), risk_fraction: percentToFraction(v.riskPct),
  };
  if (v.target.trim()) body.target = v.target.trim();
  if (v.step.trim()) body.quantity_step = v.step.trim();
  return body;
}

function pctText(fraction: string): string {
  return `${(Number(fraction) * 100).toFixed(3).replace(/\.?0+$/, "")}%`;
}

function result(r: RiskSizeResponse): HTMLElement {
  const zero = Number(r.quantity) === 0;
  return h(
    "section",
    { class: "card", "aria-labelledby": "risk-result-h" },
    h("header", { class: "card-head" }, h("h4", { id: "risk-result-h" }, "Position size"),
      badge(r.side === "long" ? "Long (stop below entry)" : "Short (stop above entry)", "info")),
    h(
      "dl",
      { class: "kv" },
      h("dt", null, "Quantity"),
      h("dd", null, h("strong", null, r.quantity)),
      h("dt", null, "Risk if stopped"),
      h("dd", null, `${r.risk_amount} (${pctText(r.risk_fraction_used)} of capital)`),
      h("dt", null, "Risk budget"),
      h("dd", null, r.risk_budget),
      h("dt", null, "Risk per unit"),
      h("dd", null, r.risk_per_unit),
      h("dt", null, "Stop distance"),
      h("dd", null, pctText(r.stop_distance_fraction)),
      h("dt", null, "Notional"),
      h("dd", null, r.notional),
      h("dt", null, "Reward : risk"),
      h("dd", null, r.reward_risk === null ? "add a target to see it" : `${r.reward_risk} : 1`),
    ),
    zero ? h("p", { class: "warn-text" }, r.reason ?? "The stop is too far for this budget.") : null,
    r.reward_risk !== null && Number(r.reward_risk) < 2
      ? h("p", { class: "small warn-text" }, "Reward is less than twice the risk; both playbooks look for about 2–3 : 1.")
      : null,
    h("p", { class: "small muted" }, r.note, " Fees and slippage are not included."),
  );
}

export class RiskView implements View {
  private readonly root: HTMLElement;
  private readonly app: App;
  private readonly out = h("div", { "aria-live": "polite" });
  private readonly inputs: Record<keyof RiskInputs, HTMLInputElement>;
  private readonly errors: Record<string, HTMLElement> = {};

  constructor(root: HTMLElement, app: App) {
    this.root = root;
    this.app = app;
    const input = (name: keyof RiskInputs, value = "", placeholder = ""): HTMLInputElement =>
      h("input", { type: "text", inputmode: "decimal", autocomplete: "off", name, value, placeholder, spellcheck: false });
    this.inputs = {
      capital: input("capital", "", "e.g. 25000"),
      riskPct: input("riskPct", "0.8"),
      entry: input("entry", "", "e.g. 64250"),
      stop: input("stop", "", "e.g. 61900"),
      target: input("target", "", "optional"),
      step: input("step", "", "optional, e.g. 0.001"),
    };
    this.render();
  }

  // No autofocus here: show() also runs when arrow keys move between tabs, and stealing focus would trap them.
  show(): void {}

  private wrap(name: keyof RiskInputs, label: string, hint: string): HTMLElement {
    const f = field(label, this.inputs[name], hint);
    const err = h("p", { class: "field-error", id: `risk-${name}-err`, hidden: true });
    this.errors[name] = err;
    f.append(err);
    return f;
  }

  private render(): void {
    const form = h(
      "form",
      { class: "stack", novalidate: true, onsubmit: (ev: Event) => { ev.preventDefault(); void this.submit(); } },
      h("p", { class: "small muted" },
        "Size a position so that hitting the stop loses a fixed share of capital. Nothing is sent to an exchange."),
      h("div", { class: "row" },
        this.wrap("capital", "Capital", "Trading capital in quote currency."),
        this.wrap("riskPct", "Risk per trade (%)", `0.5–1% is the playbook default; hard cap ${MAX_RISK_PCT}%.`)),
      h("div", { class: "row" },
        this.wrap("entry", "Entry", "Planned entry price."),
        this.wrap("stop", "Stop", "Below entry for a long, above for a short.")),
      h("div", { class: "row" },
        this.wrap("target", "Target", "For the reward : risk ratio."),
        this.wrap("step", "Quantity step", "The exchange lot step; sizes round down to it.")),
      h("div", { class: "toolbar" }, h("button", { type: "submit", class: "primary" }, "Calculate")),
    );
    setChildren(this.root, form, this.out);
  }

  private values(): RiskInputs {
    const v = {} as RiskInputs;
    for (const [k, el] of Object.entries(this.inputs)) v[k as keyof RiskInputs] = el.value;
    return v;
  }

  private showErrors(errors: Record<string, string>): void {
    for (const [name, el] of Object.entries(this.inputs)) {
      const err = this.errors[name]!;
      const msg = errors[name];
      err.hidden = !msg;
      err.textContent = msg ?? "";
      el.setAttribute("aria-invalid", msg ? "true" : "false");
      const described = (el.getAttribute("aria-describedby") ?? "").split(" ").filter((x) => x && x !== err.id);
      if (msg) described.push(err.id);
      el.setAttribute("aria-describedby", described.join(" "));
    }
    const first = Object.keys(this.inputs).find((k) => errors[k]);
    if (first) this.inputs[first as keyof RiskInputs].focus();
  }

  async submit(): Promise<void> {
    const v = this.values();
    const errors = validateRisk(v);
    this.showErrors(errors);
    if (Object.keys(errors).length) {
      setChildren(this.out);
      return;
    }
    setChildren(this.out, status("loading", "Calculating…"));
    try {
      setChildren(this.out, result(await this.app.client.riskSize(toRequest(v))));
    } catch (err) {
      setChildren(this.out, this.app.errorView(err));
    }
  }
}
