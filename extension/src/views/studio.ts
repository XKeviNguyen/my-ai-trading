// Strategy Studio: compile prompts → preview → dry-run / save draft → explicit rule_hash approval.

import { TIMEFRAMES } from "../api.js";

import type { App, View } from "../app.js";
import { badge, field, h, list, setChildren, status } from "../dom.js";
import { fmtJson, fmtMs } from "../format.js";
import type { CompileResponse, DryRunResponse, RulePreview, Strategy, StrategyRule } from "../types.js";

export const NAME_RE = /^[\w .,:()+/-]{1,80}$/;

/** Split the textarea into prompts: one per non-empty line, at most 10. */
export function splitPrompts(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

function previewBlock(p: RulePreview | null | undefined): HTMLElement {
  if (!p) return h("p", { class: "muted" }, "No rule preview.");
  return h(
    "dl",
    { class: "kv" },
    [
      ["Name", p.name],
      ["When", p.when],
      ["Condition", p.condition],
      ["Then", p.then],
      ["Timeframes used", (p.timeframes_used ?? []).join(", ")],
      ["Cooldown bars", p.cooldown_bars === undefined ? undefined : String(p.cooldown_bars)],
      ["Bar finality", p.bar_finality],
      ["Attribution", p.attribution],
      ["rule_hash", p.rule_hash],
    ].map(([k, v]) => [h("dt", null, k ?? ""), h("dd", { class: k === "rule_hash" ? "mono" : "" }, v ?? "—")]),
  );
}

function statusTone(s: string): "ok" | "warn" | "err" | "muted" | "info" {
  if (s === "COMPILED" || s === "ACTIVE") return "ok";
  if (s === "NEEDS_CONFIRMATION" || s === "DRAFT") return "warn";
  if (s === "REJECTED" || s === "UNRESOLVED" || s === "UNAVAILABLE") return "err";
  return "muted";
}

function dryRunBlock(r: DryRunResponse): HTMLElement {
  const c = r.current ?? {};
  const counts = Object.entries(r.replay?.counts ?? {}).map(([k, v]) => `${k}: ${v}`);
  const matches = (r.replay?.recent_matches ?? []).filter((m): m is string => typeof m === "string");
  return h(
    "section",
    { class: "card" },
    h("h4", null, "Dry-run"),
    h("p", null, "Current: ", badge(c.result ?? "—", c.result === "MATCH" ? "info" : "muted"), ` action ${c.action ?? "—"} · as of ${c.as_of_utc ?? "—"}`),
    h(
      "p",
      { class: "small" },
      "Bar close times: ",
      Object.entries(c.bar_close_times ?? {})
        .map(([tf, t]) => `${tf} ${t ?? "—"}`)
        .join(" · ") || "—",
    ),
    c.leaves && c.leaves.length
      ? h(
          "details",
          null,
          h("summary", null, `Conditions (${c.leaves.length})`),
          h(
            "ul",
            { class: "plain" },
            c.leaves.map((l) =>
              h("li", null, `${l.condition ?? ""} → ${fmtJson(l.result)}${l.left !== undefined ? ` (left ${fmtJson(l.left)}, right ${fmtJson(l.right)})` : ""}`),
            ),
          ),
        )
      : null,
    c.note ? h("p", { class: "small muted" }, c.note) : null,
    h("h5", null, `Replay over ${r.replay?.bars ?? 0} closed bars`),
    h("p", { class: "small" }, counts.join(" · ") || "—"),
    h("details", null, h("summary", null, `Recent matches (${matches.length})`), list(matches, "No matches in the replay window.")),
    r.replay?.note ? h("p", { class: "small muted" }, r.replay.note) : null,
  );
}

export class StudioView implements View {
  private readonly app: App;
  private readonly listBox: HTMLElement;
  private loaded = false;

  constructor(root: HTMLElement, app: App) {
    this.app = app;
    this.listBox = h("div", { "aria-live": "polite" });
    setChildren(
      root,
      this.composer(),
      h(
        "section",
        { "aria-labelledby": "strat-h" },
        h("div", { class: "row-between" }, h("h3", { id: "strat-h" }, "My strategies"), h("button", { type: "button", onclick: () => void this.loadList() }, "Reload")),
        this.listBox,
      ),
    );
  }

  show(): void {
    if (!this.loaded) void this.loadList();
  }

  private composer(): HTMLElement {
    const name = h("input", { type: "text", value: "Custom strategy", maxlength: 80 });
    const prompts = h("textarea", {
      rows: 4,
      placeholder: "One prompt per line, e.g.\nalert me when the 1h close is above SMA50 and RSI is below 70\nalso require the 1d trend is up",
    });
    const tf = h(
      "select",
      null,
      h("option", { value: "" }, "None (prompts must state it)"),
      TIMEFRAMES.map((t) => h("option", { value: t }, t)),
    );
    const llm = h("input", { type: "checkbox" });
    const out = h("div", { "aria-live": "polite" });
    const btn = h("button", { type: "submit", class: "primary" }, "Compile preview");
    const form = h(
      "form",
      { class: "stack" },
      field("Strategy name", name, "Letters, digits, spaces and . , : ( ) + / - only."),
      field("Prompts", prompts, "Up to 10 lines. Rules are data only; nothing runs until you approve."),
      field("Default timeframe for prompts without one", tf),
      h("label", { class: "check" }, llm, " If unresolved, ask the AI to propose a rule (uses tokens; needs consent)"),
      btn,
      out,
    );
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const ps = splitPrompts(prompts.value);
      const nm = name.value.trim() || "Custom strategy";
      if (ps.length === 0 || ps.length > 10) return void setChildren(out, status("error", "Enter 1 to 10 prompts (one per line)."));
      if (!NAME_RE.test(nm)) return void setChildren(out, status("error", "Name contains unsupported characters."));
      btn.disabled = true;
      setChildren(out, status("loading", "Compiling…"));
      this.app.client
        .compile(ps, nm, tf.value || null, llm.checked)
        .then((r) => setChildren(out, this.proposal(r, ps, "Proposal")))
        .catch((err) => setChildren(out, this.app.errorView(err)))
        .finally(() => (btn.disabled = false));
    });
    return h("section", { "aria-labelledby": "studio-h" }, h("h3", { id: "studio-h" }, "Strategy Studio"), form);
  }

  private proposal(r: CompileResponse | (Partial<CompileResponse> & { reason?: string; model?: string }), prompts: string[], title: string): HTMLElement {
    const rule = r.rule ?? null;
    const actions = h("div", { class: "toolbar" });
    const out = h("div", { "aria-live": "polite" });
    if (rule) {
      actions.append(
        h("button", { type: "button", onclick: () => void this.dryRun(rule, out) }, "Dry-run on current symbol"),
        h("button", { type: "button", class: "primary", onclick: (e: Event) => void this.saveDraft(rule, prompts, out, e.currentTarget as HTMLButtonElement) }, "Save draft"),
      );
    }
    const extra = r as { reason?: string; model?: string };
    return h(
      "section",
      { class: "card" },
      h("header", { class: "card-head" }, h("h4", null, title), badge(r.status ?? "—", statusTone(r.status ?? ""))),
      extra.model ? h("p", { class: "mode-label" }, `Proposed by model ${extra.model} — review every condition`) : null,
      extra.reason ? h("p", { class: "small" }, extra.reason) : null,
      r.preview ? previewBlock(r.preview) : null,
      r.clauses && r.clauses.length ? h("details", null, h("summary", null, "Recognized clauses"), list(r.clauses.map((c) => `${c.kind}: ${c.text}`))) : null,
      h("h5", null, "Assumptions"),
      list(r.assumptions),
      r.unresolved && r.unresolved.length ? [h("h5", null, "Unresolved"), list(r.unresolved)] : null,
      r.rejected_reasons && r.rejected_reasons.length ? [h("h5", null, "Rejected"), list(r.rejected_reasons)] : null,
      r.activation ? h("p", { class: "small muted" }, `Activation: ${r.activation}`) : null,
      actions,
      out,
      "llm_proposal" in r && r.llm_proposal ? this.proposal(r.llm_proposal, prompts, "AI proposal (validated)") : null,
    );
  }

  private async dryRun(rule: StrategyRule, out: HTMLElement): Promise<void> {
    const inst = this.app.instrument;
    if (!inst) return setChildren(out, status("error", "Select an instrument above first."));
    setChildren(out, status("loading", `Dry-running on ${inst.symbol}…`));
    try {
      setChildren(out, dryRunBlock(await this.app.client.dryRun(rule, inst.symbol, inst.market)));
    } catch (err) {
      setChildren(out, this.app.errorView(err));
    }
  }

  private async saveDraft(rule: StrategyRule, prompts: string[], out: HTMLElement, btn: HTMLButtonElement): Promise<void> {
    btn.disabled = true;
    setChildren(out, status("loading", "Saving draft…"));
    try {
      const r = await this.app.client.createStrategy(rule, prompts);
      setChildren(
        out,
        status("ok", `Saved strategy ${r.strategy_id} v${r.version} as ${r.status}. ${r.next ?? ""}`),
        r.preview?.rule_hash ? this.approveControl(r.strategy_id, r.version, r.preview.rule_hash, r.preview.name ?? rule["name"]?.toString() ?? "") : null,
      );
      void this.loadList();
    } catch (err) {
      btn.disabled = false;
      setChildren(out, this.app.errorView(err));
    }
  }

  /** Approve requires a second explicit click on a button that shows the exact rule_hash. */
  private approveControl(sid: string, version: number, ruleHash: string, name: string): HTMLElement {
    const box = h("div", { class: "confirm", "aria-live": "polite" });
    const start = h("button", { type: "button" }, `Approve v${version}…`);
    start.addEventListener("click", () => {
      const confirmBtn = h("button", { type: "button", class: "danger" }, `Confirm approve rule_hash ${ruleHash}`);
      const cancel = h("button", { type: "button" }, "Cancel");
      cancel.addEventListener("click", () => setChildren(box, start));
      confirmBtn.addEventListener("click", async () => {
        confirmBtn.disabled = true;
        try {
          await this.app.client.approve(sid, version, ruleHash);
          setChildren(box, status("ok", `${name} v${version} is ACTIVE; the local monitor evaluates it on closed bars.`));
          void this.loadList();
        } catch (err) {
          setChildren(box, this.app.errorView(err), start);
        }
      });
      setChildren(
        box,
        h("p", { class: "small" }, `Activate monitoring for "${name}" v${version}? Only confirm if this rule_hash matches the preview you reviewed:`),
        h("p", { class: "mono" }, ruleHash),
        h("div", { class: "toolbar" }, confirmBtn, cancel),
      );
      confirmBtn.focus();
    });
    box.append(start);
    return box;
  }

  async loadList(): Promise<void> {
    setChildren(this.listBox, status("loading", "Loading strategies…"));
    try {
      const { strategies } = await this.app.client.strategies();
      this.loaded = true;
      if (strategies.length === 0) return setChildren(this.listBox, h("p", { class: "muted" }, "No saved strategies yet."));
      setChildren(this.listBox, h("div", { class: "cards" }, strategies.map((s) => this.strategyCard(s))));
    } catch (err) {
      setChildren(this.listBox, this.app.errorView(err));
    }
  }

  private strategyCard(s: Strategy): HTMLElement {
    const active = s.versions.some((v) => v.status === "ACTIVE");
    const msg = h("div", { "aria-live": "polite" });
    const deactivate = active
      ? h("button", {
          type: "button",
          onclick: async (e: Event) => {
            const b = e.currentTarget as HTMLButtonElement;
            b.disabled = true;
            try {
              await this.app.client.deactivate(s.id);
              await this.loadList();
            } catch (err) {
              b.disabled = false;
              setChildren(msg, this.app.errorView(err));
            }
          },
        }, "Deactivate")
      : null;
    return h(
      "article",
      { class: "card" },
      h("header", { class: "card-head" }, h("h4", null, s.name), badge(active ? "ACTIVE" : "INACTIVE", active ? "ok" : "muted")),
      h("p", { class: "mode-label" }, s.label),
      h("p", { class: "small muted" }, `id ${s.id} · ${s.kind}${s.parent_methodology ? ` · parent ${s.parent_methodology}` : ""}`),
      h(
        "ul",
        { class: "plain versions" },
        s.versions.map((v) =>
          h(
            "li",
            null,
            h("div", null, `v${v.version} `, badge(v.status, statusTone(v.status)), " ", h("span", { class: "mono small" }, v.rule_hash)),
            h("div", { class: "small" }, `trigger ${fmtJson(v.rule["trigger_timeframe"])} · ${fmtJson(v.rule["description"] ?? "", 160)}`),
            v.approved_at ? h("div", { class: "small muted" }, `approved ${fmtMs(v.approved_at)}`) : null,
            v.status === "DRAFT" ? this.approveControl(s.id, v.version, v.rule_hash, s.name) : null,
          ),
        ),
      ),
      deactivate,
      msg,
    );
  }
}
