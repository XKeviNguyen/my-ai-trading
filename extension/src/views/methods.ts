// Methodology selection: all 20 profiles, enable/disable any (including none) and choose a mode.

import type { App, View } from "../app.js";
import { badge, h, list, nextId, setChildren, status } from "../dom.js";
import type { Profile, Selection } from "../types.js";

const MODES: Selection["mode"][] = ["NATIVE", "HYBRID", "ADAPTED"];
const MODE_HINT: Record<Selection["mode"], string> = {
  NATIVE: "Methodology-inspired / native scope",
  HYBRID: "Hybrid: native context + user timing",
  ADAPTED: "User-adapted — unvalidated; not the trader's original system",
};

/** Build the new selection list: keep the saved order, append newly enabled in catalog order. */
export function buildSelections(
  previous: Selection[],
  catalogOrder: string[],
  enabled: Map<string, Selection["mode"]>,
): Selection[] {
  const out: Selection[] = [];
  const seen = new Set<string>();
  for (const s of previous) {
    const mode = enabled.get(s.methodology_id);
    if (mode && !seen.has(s.methodology_id)) {
      out.push({ methodology_id: s.methodology_id, mode });
      seen.add(s.methodology_id);
    }
  }
  for (const id of catalogOrder) {
    const mode = enabled.get(id);
    if (mode && !seen.has(id)) {
      out.push({ methodology_id: id, mode });
      seen.add(id);
    }
  }
  return out;
}

interface RowState {
  enabled: HTMLInputElement;
  mode: HTMLSelectElement;
}

export class MethodsView implements View {
  private readonly root: HTMLElement;
  private readonly app: App;
  private rows = new Map<string, RowState>();

  constructor(root: HTMLElement, app: App) {
    this.root = root;
    this.app = app;
  }

  show(): void {
    void this.load();
  }

  async load(): Promise<void> {
    setChildren(this.root, status("loading", "Loading methodologies…"));
    try {
      const [profiles, settings] = await Promise.all([this.app.profiles(), this.app.settings(true)]);
      const selected = new Map(settings.selected_methodologies.map((s) => [s.methodology_id, s.mode]));
      this.rows.clear();
      const msg = h("div", { "aria-live": "polite" });
      const save = h("button", { type: "button", class: "primary" }, "Save selection");
      save.addEventListener("click", () => void this.save(profiles, msg, save));
      const none = h("button", { type: "button" }, "Disable all");
      none.addEventListener("click", () => {
        this.rows.forEach((r) => (r.enabled.checked = false));
        setChildren(msg, status("info", "All disabled — press Save to apply."));
      });
      setChildren(
        this.root,
        h("p", { class: "small" }, `${profiles.length} research profiles. Each selected profile is reported on its own; there is no vote across them. Selecting none is fine.`),
        h("div", { class: "toolbar sticky" }, save, none),
        msg,
        h("ul", { class: "plain cards" }, profiles.map((p) => this.row(p, selected.get(p.methodology_id)))),
      );
    } catch (err) {
      setChildren(this.root, this.app.errorView(err));
    }
  }

  private row(p: Profile, mode: Selection["mode"] | undefined): HTMLElement {
    const id = nextId("m");
    const enabled = h("input", { type: "checkbox", id: `${id}-on`, checked: mode !== undefined });
    const select = h(
      "select",
      { id: `${id}-mode`, "aria-describedby": `${id}-hint` },
      MODES.map((m) => h("option", { value: m }, m)),
    );
    select.value = mode ?? "NATIVE";
    const hint = h("small", { class: "hint", id: `${id}-hint` }, MODE_HINT[select.value as Selection["mode"]]);
    select.addEventListener("change", () => (hint.textContent = MODE_HINT[select.value as Selection["mode"]]));
    this.rows.set(p.methodology_id, { enabled, mode: select });
    const tfs = Array.isArray(p.documented_timeframes) ? p.documented_timeframes.join("/") : p.documented_timeframes;
    return h(
      "li",
      { class: "card" },
      h(
        "div",
        { class: "card-head" },
        enabled,
        h("label", { for: enabled.id, class: "strong" }, p.name),
        badge(p.implementation_maturity, p.implementation_maturity === "experimental" ? "info" : "muted"),
        badge(`evidence: ${p.evidence_quality}`, "muted"),
      ),
      h("p", { class: "small" }, `${p.focus} · native ${tfs} · ${p.price_only ? "price-only" : `needs ${p.required_data.join(", ")}`}`),
      h("div", { class: "field inline-field" }, h("label", { for: select.id }, "Mode"), select, hint),
      h("details", null, h("summary", null, "Provenance & limitations"), h("p", { class: "small" }, p.provenance), list(p.limitations, "No limitations listed.")),
    );
  }

  private async save(profiles: Profile[], msg: HTMLElement, btn: HTMLButtonElement): Promise<void> {
    const enabled = new Map<string, Selection["mode"]>();
    this.rows.forEach((r, mid) => {
      if (r.enabled.checked) enabled.set(mid, r.mode.value as Selection["mode"]);
    });
    btn.disabled = true;
    setChildren(msg, status("loading", "Saving…"));
    try {
      const s = await this.app.updateSettings((cur) => {
        cur.selected_methodologies = buildSelections(cur.selected_methodologies, profiles.map((p) => p.methodology_id), enabled);
      });
      setChildren(msg, status("ok", `Saved: ${s.selected_methodologies.length} methodolog${s.selected_methodologies.length === 1 ? "y" : "ies"} selected.`));
    } catch (err) {
      setChildren(msg, this.app.errorView(err));
    } finally {
      btn.disabled = false;
    }
  }
}
