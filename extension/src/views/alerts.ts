// Alerts feed. The service worker polls GET /api/v1/alerts every minute and caches results in
// chrome.storage.local; while this tab is visible we ask it to poll every 30 s.

import type { App, View } from "../app.js";
import { badge, h, setChildren, status } from "../dom.js";
import { alertSummary, fmtAge, fmtMs, isQuiet, symbolFromKey } from "../format.js";
import { getAlerts, getNotifyEnabled, getPollStatus, KEYS, setNotifyEnabled, type PollStatus } from "../storage.js";
import type { Alert } from "../types.js";

const PANEL_POLL_MS = 30_000;

function alertItem(a: Alert, now: number): HTMLElement {
  const quiet = isQuiet(a);
  return h(
    "li",
    { class: quiet ? "alert quiet" : "alert" },
    h(
      "div",
      { class: "card-head" },
      h("strong", null, `${symbolFromKey(a.instrument_key)} ${a.timeframe}`),
      badge(a.event_type, a.priority >= 3 ? "warn" : "muted"),
      quiet ? badge("quiet hours", "muted") : null,
    ),
    h("div", { class: "small" }, alertSummary(a)),
    h(
      "div",
      { class: "small muted" },
      `source ${a.source} · bar opened ${fmtMs(a.bar_open_time_ms)} · recorded ${fmtAge(a.created_at, now)} · #${a.seq}`,
    ),
  );
}

export class AlertsView implements View {
  private readonly app: App;
  private readonly listBox: HTMLElement;
  private readonly statusBox: HTMLElement;
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly root: HTMLElement;

  constructor(root: HTMLElement, app: App) {
    this.root = root;
    this.app = app;
    this.listBox = h("div");
    this.statusBox = h("div", { "aria-live": "polite" });
    const notify = h("input", { type: "checkbox" });
    void getNotifyEnabled().then((on) => (notify.checked = on));
    notify.addEventListener("change", () => void setNotifyEnabled(notify.checked));
    setChildren(
      root,
      h(
        "div",
        { class: "toolbar" },
        h("button", { type: "button", onclick: () => void this.poll() }, "Check now"),
        h("button", { type: "button", onclick: () => void this.reset() }, "Clear cache"),
      ),
      h("label", { class: "check" }, notify, " Desktop notifications for non-quiet alerts"),
      this.statusBox,
      this.listBox,
    );
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "local") return;
      if (KEYS.alerts in changes || KEYS.lastPoll in changes) void this.render();
    });
    document.addEventListener("visibilitychange", () => this.syncTimer());
    void this.render();
  }

  show(): void {
    void this.poll();
    this.syncTimer();
  }

  /** Poll every 30 s only while the panel and this tab are visible. */
  syncTimer(): void {
    const visible = !this.root.hidden && document.visibilityState === "visible";
    if (visible && !this.timer) this.timer = setInterval(() => void this.poll(), PANEL_POLL_MS);
    if (!visible && this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  private async poll(): Promise<void> {
    try {
      await chrome.runtime.sendMessage<PollStatus>({ type: "poll-alerts" });
    } catch {
      setChildren(this.statusBox, status("error", "Background worker not reachable; reload the extension."));
    }
  }

  private async reset(): Promise<void> {
    await chrome.runtime.sendMessage({ type: "reset-alerts" }).catch(() => undefined);
    await this.render();
    void this.poll();
  }

  private async render(): Promise<void> {
    const [alerts, st] = await Promise.all([getAlerts(), getPollStatus()]);
    const now = Date.now();
    if (st) {
      setChildren(
        this.statusBox,
        st.ok
          ? status("ok", `Last checked ${fmtAge(st.at, now)}${st.newCount ? ` · ${st.newCount} new` : ""}.`)
          : h("div", null, status("error", `Last check ${fmtAge(st.at, now)} failed: ${st.error ?? "unknown error"}`), st.error?.includes("token") ? h("button", { type: "button", onclick: () => this.app.selectTab("settings") }, "Open Settings") : null),
      );
    } else {
      setChildren(this.statusBox, status("info", "Not checked yet."));
    }
    if (alerts.length === 0) {
      setChildren(
        this.listBox,
        h("p", { class: "muted" }, "No alerts yet. Alerts come from the local monitor (`my-ai-trading serve`) for your watchlist, approved strategies and methodology state changes."),
      );
      return;
    }
    setChildren(this.listBox, h("ul", { class: "plain alerts", "aria-label": "Alerts, newest first" }, alerts.map((a) => alertItem(a, now))));
  }
}
