// Settings: API token + connection test, watchlist, analysis timeframe, quiet hours, alerts pause,
// AI consent / deep-analysis opt-in, monthly budgets. Saved with PUT /api/v1/settings (full object).


import { ApiError, TIMEFRAMES } from "../api.js";
import type { App, View } from "../app.js";
import { field, h, setChildren, status } from "../dom.js";
import { clearToken, getToken, setToken } from "../storage.js";
import { normalizeSymbol, type Market } from "../symbol.js";
import type { UserSettings, WatchItem } from "../types.js";

// The engine writes secrets.token_urlsafe(32) (43 chars) and accepts any file content of 32+ chars.
export const TOKEN_RE = /^[\x21-\x7e]{32,512}$/;

/** Validate watchlist rows: normalize, drop blanks, reject invalid, dedupe. */
export function cleanWatchlist(rows: { symbol: string; market: string }[]): { items: WatchItem[]; errors: string[] } {
  const items: WatchItem[] = [];
  const errors: string[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    if (!r.symbol.trim()) continue;
    const sym = normalizeSymbol(r.symbol);
    if (!sym) {
      errors.push(`"${r.symbol.slice(0, 30)}" is not a valid symbol`);
      continue;
    }
    const market: Market = r.market === "spot" ? "spot" : "usdm_perpetual";
    const key = `${sym}|${market}`;
    if (!seen.has(key)) {
      seen.add(key);
      items.push({ symbol: sym, market });
    }
  }
  if (items.length > 50) errors.push("At most 50 watchlist entries.");
  return { items, errors };
}

function numberInput(value: number, min: number, max: number, step = "1"): HTMLInputElement {
  return h("input", { type: "number", min, max, step, value: String(value), inputmode: "decimal" });
}

export class SettingsView implements View {
  private readonly root: HTMLElement;
  private readonly app: App;
  private readonly prefsBox: HTMLElement;
  /** Version tag of the settings the form shows; a save sends it so stale form values never overwrite. */
  private formEtag: string | null = null;

  constructor(root: HTMLElement, app: App) {
    this.root = root;
    this.app = app;
    this.prefsBox = h("div");
    setChildren(this.root, this.connection(), this.prefsBox);
  }

  show(): void {
    void this.loadPrefs();
  }

  private connection(): HTMLElement {
    const tokenInput = h("input", { type: "password", autocomplete: "off", spellcheck: "false", placeholder: "Paste token" });
    const stateLine = h("p", { class: "small", "aria-live": "polite" });
    const out = h("div", { "aria-live": "polite" });
    const refreshState = async () => {
      stateLine.textContent = (await getToken()) ? "A token is saved on this device (hidden)." : "No token saved.";
    };
    void refreshState();
    const save = h("button", { type: "submit", class: "primary" }, "Save token");
    const form = h(
      "form",
      { class: "stack" },
      field(
        "Local API token",
        tokenInput,
        "Run `my-ai-trading token` to print the token file path (~/.config/my-ai-trading/api_token), open it and paste its contents. Stored only in this browser profile (chrome.storage.local), sent only to http://127.0.0.1:8765.",
      ),
      stateLine,
      h(
        "div",
        { class: "toolbar" },
        save,
        h("button", { type: "button", onclick: () => void this.test(out) }, "Test connection"),
        h(
          "button",
          {
            type: "button",
            onclick: async () => {
              await clearToken();
              await refreshState();
              setChildren(out, status("info", "Token removed."));
            },
          },
          "Remove token",
        ),
      ),
      out,
    );
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const t = tokenInput.value.trim();
      if (!TOKEN_RE.test(t)) return void setChildren(out, status("error", "That does not look like an API token (expected 32+ printable characters, no spaces)."));
      await setToken(t);
      tokenInput.value = "";
      await refreshState();
      await this.test(out);
      void this.loadPrefs();
    });
    return h("section", { "aria-labelledby": "conn-h" }, h("h3", { id: "conn-h" }, "Connection"), form);
  }

  private async test(out: HTMLElement): Promise<void> {
    setChildren(out, status("loading", "Checking /health…"));
    try {
      const health = await this.app.client.health();
      setChildren(out, status("loading", `Engine ${health.version} is up (read-only: ${health.read_only}). Checking token…`));
      await this.app.client.getSettings();
      setChildren(out, status("ok", `Connected to engine ${health.version}; token accepted.`));
    } catch (err) {
      setChildren(out, this.app.errorView(err));
    }
  }

  async loadPrefs(): Promise<void> {
    setChildren(this.prefsBox, status("loading", "Loading settings…"));
    try {
      const s = await this.app.settings(true);
      this.formEtag = this.app.settingsEtag;
      this.renderPrefs(s);
    } catch (err) {
      if (err instanceof ApiError && err.kind === "unauthorized") {
        setChildren(this.prefsBox, status("info", "Save a valid token to load your settings."));
      } else setChildren(this.prefsBox, this.app.errorView(err));
    }
  }

  private renderPrefs(s: UserSettings, notice: HTMLElement | null = null): void {
    const atf = h("select", null, TIMEFRAMES.map((t) => h("option", { value: t }, t)));
    atf.value = s.analysis_timeframe;

    // watchlist
    const wlBody = h("div", { class: "stack" });
    const addRow = (item?: WatchItem) => {
      const sym = h("input", { type: "text", value: item?.symbol ?? "", maxlength: 24, "aria-label": "Watchlist symbol", placeholder: "SANDUSDT", class: "sym" });
      const mk = h("select", { "aria-label": "Watchlist market" }, h("option", { value: "usdm_perpetual" }, "USD-M perp"), h("option", { value: "spot" }, "Spot"));
      mk.value = item?.market ?? "usdm_perpetual";
      const row = h("div", { class: "wl-row" }, sym, mk);
      row.append(h("button", { type: "button", "aria-label": "Remove watchlist entry", onclick: () => row.remove() }, "Remove"));
      wlBody.append(row);
    };
    s.watchlist.forEach((w) => addRow(w));

    const quietOn = h("input", { type: "checkbox", checked: s.quiet_hours_utc !== null });
    const qStart = numberInput(s.quiet_hours_utc?.[0] ?? 22, 0, 23);
    const qEnd = numberInput(s.quiet_hours_utc?.[1] ?? 7, 0, 23);
    const paused = h("input", { type: "checkbox", checked: s.alerts_paused });
    const consent = h("input", { type: "checkbox", checked: s.llm_data_consent });
    const deep = h("input", { type: "checkbox", checked: s.deep_analysis_opt_in });
    const tokBudget = numberInput(s.monthly_ai_token_budget, 0, 100_000_000);
    const usdBudget = numberInput(s.monthly_ai_budget_usd, 0, 10_000, "0.01");
    const out = h("div", { "aria-live": "polite" }, notice);
    const save = h("button", { type: "submit", class: "primary" }, "Save settings");

    const form = h(
      "form",
      { class: "stack" },
      h("h3", null, "Analysis"),
      field("Default analysis timeframe", atf, "The timeframe you are viewing; methodologies still evaluate on their own native timeframe."),
      h("fieldset", null, h("legend", null, "Watchlist (monitored by the local engine)"), wlBody, h("button", { type: "button", onclick: () => addRow() }, "Add symbol")),
      h("h3", null, "Alerts"),
      h("label", { class: "check" }, paused, " Pause all alerts"),
      h(
        "fieldset",
        null,
        h("legend", null, "Quiet hours (UTC)"),
        h("label", { class: "check" }, quietOn, " Mark alerts as quiet during these hours"),
        h("div", { class: "row" }, field("From hour", qStart), field("To hour (exclusive)", qEnd)),
      ),
      h("h3", null, "AI"),
      h(
        "label",
        { class: "check" },
        consent,
        " I consent to sending market context (snapshot, perspectives, my question/thesis, screenshots I choose) to the configured AI provider.",
      ),
      h("p", { class: "small muted" }, "Without consent every answer is deterministic and costs nothing. Your API token and keys are never sent."),
      h("label", { class: "check" }, deep, " Allow deep analysis (higher effort; up to ~16k output tokens per request)"),
      h("p", { class: "small muted" }, "Deep analysis runs only when you tick it on a request, and only within the budgets below."),
      h("div", { class: "row" }, field("Monthly token budget", tokBudget), field("Monthly USD budget (0 = no USD cap)", usdBudget)),
      save,
      out,
    );
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const rows = [...wlBody.querySelectorAll(".wl-row")].map((r) => ({
        symbol: (r.querySelector("input") as HTMLInputElement).value,
        market: (r.querySelector("select") as HTMLSelectElement).value,
      }));
      const wl = cleanWatchlist(rows);
      const qs = Number(qStart.value);
      const qe = Number(qEnd.value);
      const tb = Number(tokBudget.value);
      const ub = Number(usdBudget.value);
      const errors = [...wl.errors];
      if (quietOn.checked && !(Number.isInteger(qs) && Number.isInteger(qe) && qs >= 0 && qs <= 23 && qe >= 0 && qe <= 23)) errors.push("Quiet hours must be whole UTC hours 0–23.");
      if (!(Number.isInteger(tb) && tb >= 0 && tb <= 100_000_000)) errors.push("Token budget must be a whole number 0–100,000,000.");
      if (!(Number.isFinite(ub) && ub >= 0 && ub <= 10_000)) errors.push("USD budget must be between 0 and 10,000.");
      if (errors.length) return void setChildren(out, status("error", errors.join(" ")));
      save.disabled = true;
      setChildren(out, status("loading", "Saving…"));
      try {
        await this.app.updateSettings((cur) => {
          cur.analysis_timeframe = atf.value;
          cur.watchlist = wl.items;
          cur.quiet_hours_utc = quietOn.checked ? [qs, qe] : null;
          cur.alerts_paused = paused.checked;
          cur.llm_data_consent = consent.checked;
          cur.deep_analysis_opt_in = deep.checked;
          cur.monthly_ai_token_budget = tb;
          cur.monthly_ai_budget_usd = ub;
        }, this.formEtag);
        this.formEtag = this.app.settingsEtag;
        setChildren(out, status("ok", "Settings saved."));
        this.app.setAnalysisTf(atf.value);
      } catch (err) {
        if (err instanceof ApiError && err.kind === "changed_elsewhere" && this.app.cachedSettings) {
          // Show the newer settings instead of the stale form; the user reviews and saves again.
          this.formEtag = this.app.settingsEtag;
          return void this.renderPrefs(this.app.cachedSettings, this.app.errorView(err));
        }
        setChildren(out, this.app.errorView(err));
      } finally {
        save.disabled = false;
      }
    });
    setChildren(this.prefsBox, form);
  }
}
