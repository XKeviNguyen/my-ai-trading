// AI usage and estimated cost (GET /api/v1/usage).

import type { App, View } from "../app.js";
import { h, setChildren, status } from "../dom.js";
import { fmtInt, fmtMs, fmtUsd } from "../format.js";
import type { UsageResponse } from "../types.js";

function pct(used: number, budget: number): string {
  if (!budget) return "no limit set";
  return `${Math.round((used / budget) * 100)}% of budget`;
}

function render(u: UsageResponse): HTMLElement[] {
  const rows = u.by_feature_model ?? [];
  return [
    h(
      "section",
      { class: "card" },
      h("h4", null, `Since ${fmtMs(u.since_ms)}`),
      h(
        "dl",
        { class: "kv" },
        h("dt", null, "Tokens"),
        h("dd", null, `${fmtInt(u.total_tokens)} / ${fmtInt(u.monthly_ai_token_budget)} (${pct(u.total_tokens, u.monthly_ai_token_budget)})`),
        h("dt", null, "Estimated cost"),
        h("dd", null, `${fmtUsd(u.total_cost_usd)} / ${u.monthly_ai_budget_usd ? fmtUsd(u.monthly_ai_budget_usd) : "no USD cap"} (${pct(u.total_cost_usd, u.monthly_ai_budget_usd)})`),
        h("dt", null, "Calls with unknown cost"),
        h("dd", null, fmtInt(u.calls_with_unknown_cost)),
        h("dt", null, "Answered from cache (free)"),
        h("dd", null, u.cache_hit_rate == null ? "no AI calls yet" : `${fmtInt(u.cache_hits ?? 0)} (${Math.round(u.cache_hit_rate * 100)}%)`),
      ),
      h("p", { class: "small muted" }, u.note),
    ),
    rows.length === 0
      ? h("p", { class: "muted" }, "No AI calls this month. Deterministic analysis costs no tokens.")
      : h(
          "div",
          { class: "table-wrap", tabindex: 0, role: "region", "aria-label": "Usage by feature and model" },
          h(
            "table",
            null,
            h("thead", null, h("tr", null, ["Feature", "Model", "Calls", "In", "Out", "Est. cost", "Avg ms"].map((c) => h("th", { scope: "col" }, c)))),
            h(
              "tbody",
              null,
              rows.map((r) =>
                h(
                  "tr",
                  null,
                  h("td", null, r.feature),
                  h("td", null, `${r.provider}/${r.model}`),
                  h("td", null, fmtInt(r.n)),
                  h("td", null, fmtInt(r.i ?? 0)),
                  h("td", null, fmtInt(r.o ?? 0)),
                  h("td", null, r.unknown_cost ? `${fmtUsd(r.c ?? 0)} + ${r.unknown_cost} unknown` : fmtUsd(r.c ?? 0)),
                  h("td", null, fmtInt(r.lat)),
                ),
              ),
            ),
          ),
        ),
  ];
}

export class UsageView implements View {
  private readonly root: HTMLElement;
  private readonly app: App;

  constructor(root: HTMLElement, app: App) {
    this.root = root;
    this.app = app;
  }

  show(): void {
    void this.load();
  }

  async load(): Promise<void> {
    const refresh = h("button", { type: "button", onclick: () => void this.load() }, "Refresh");
    setChildren(this.root, status("loading", "Loading usage…"));
    try {
      setChildren(this.root, h("div", { class: "toolbar" }, refresh), ...render(await this.app.client.usage()));
    } catch (err) {
      setChildren(this.root, h("div", { class: "toolbar" }, refresh), this.app.errorView(err));
    }
  }
}
