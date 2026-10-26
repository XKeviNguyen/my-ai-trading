// Side panel entry: instrument bar (tab URL detection or manual entry), accessible tabs, views.

import { App, type TabId, type View } from "./app.js";
import { h, setChildren } from "./dom.js";
import { makeClient } from "./storage.js";
import { isMarket, marketLabel, normaliseSymbol, parseBinanceUrl, type Instrument } from "./symbol.js";
import { AlertsView } from "./views/alerts.js";
import { AnalysisView } from "./views/analysis.js";
import { CopilotView } from "./views/copilot.js";
import { MethodsView } from "./views/methods.js";
import { SettingsView } from "./views/settings.js";
import { StudioView } from "./views/studio.js";
import { UsageView } from "./views/usage.js";

const app = new App(makeClient());
const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

// ---------- tabs (WAI-ARIA tabs pattern with roving tabindex) ----------
const tabButtons = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
const views = new Map<TabId, View>();

function selectTab(id: TabId, focus = false): void {
  for (const btn of tabButtons) {
    const on = btn.dataset["tab"] === id;
    btn.setAttribute("aria-selected", String(on));
    btn.tabIndex = on ? 0 : -1;
    const panel = document.getElementById(btn.getAttribute("aria-controls") ?? "");
    if (panel) panel.hidden = !on;
    if (on && focus) btn.focus();
  }
  views.get(id)?.show?.();
  (views.get("alerts") as AlertsView | undefined)?.syncTimer();
}
app.selectTab = (id) => selectTab(id, true);

tabButtons.forEach((btn, i) => {
  btn.addEventListener("click", () => selectTab(btn.dataset["tab"] as TabId));
  btn.addEventListener("keydown", (e) => {
    let next = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % tabButtons.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + tabButtons.length) % tabButtons.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabButtons.length - 1;
    if (next >= 0) {
      e.preventDefault();
      selectTab(tabButtons[next]!.dataset["tab"] as TabId, true);
    }
  });
});

views.set("analysis", new AnalysisView($("panel-analysis"), app));
views.set("copilot", new CopilotView($("panel-copilot"), app));
views.set("studio", new StudioView($("panel-studio"), app));
views.set("alerts", new AlertsView($("panel-alerts"), app));
views.set("usage", new UsageView($("panel-usage"), app));
views.set("methods", new MethodsView($("panel-methods"), app));
views.set("settings", new SettingsView($("panel-settings"), app));
app.onInstrumentChange(() => views.forEach((v) => v.onInstrument?.()));

// ---------- instrument bar ----------
const summary = $("inst-summary");
const form = $<HTMLFormElement>("inst-form");
const symInput = $<HTMLInputElement>("inst-symbol");
const marketSel = $<HTMLSelectElement>("inst-market");
const follow = $<HTMLInputElement>("inst-follow");
const tfSel = $<HTMLSelectElement>("inst-tf");
const formMsg = $("inst-msg");
const watchSel = $<HTMLSelectElement>("inst-watch");

function renderSummary(note = ""): void {
  const i = app.instrument;
  if (!i) {
    setChildren(summary, h("span", { class: "muted" }, note || "No instrument selected."));
    return;
  }
  setChildren(
    summary,
    h("strong", null, i.symbol),
    ` · Binance · ${marketLabel(i.market)} · `,
    h("span", { class: "muted" }, app.instrumentSource === "tab" ? "from active tab URL" : "entered manually"),
    note ? h("span", { class: "muted" }, ` · ${note}`) : null,
  );
}

function useInstrument(inst: Instrument | null, source: "tab" | "manual" | null, note = ""): void {
  app.setInstrument(inst, source);
  if (inst) {
    symInput.value = inst.symbol;
    marketSel.value = inst.market;
    void chrome.storage.local.set({ lastInstrument: inst }).catch(() => undefined);
  }
  renderSummary(note);
}

async function detectFromTab(): Promise<void> {
  if (!follow.checked) return;
  let url: string | undefined;
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    url = tab?.url; // only populated for hosts we have permission for (www.binance.com)
  } catch {
    url = undefined;
  }
  const inst = parseBinanceUrl(url);
  if (inst) useInstrument(inst, "tab");
  else if (app.instrumentSource !== "manual") renderSummary(app.instrument ? "active tab is not a recognised Binance pair" : "Open a Binance futures/spot pair or type a symbol.");
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const sym = normaliseSymbol(symInput.value);
  if (!sym) {
    formMsg.textContent = "Symbol must be 3–20 letters/digits, e.g. SANDUSDT.";
    symInput.setAttribute("aria-invalid", "true");
    return;
  }
  symInput.removeAttribute("aria-invalid");
  formMsg.textContent = "";
  follow.checked = false;
  useInstrument({ symbol: sym, market: isMarket(marketSel.value) ? marketSel.value : "usdm_perpetual" }, "manual");
});

follow.addEventListener("change", () => void detectFromTab());
tfSel.addEventListener("change", () => app.setAnalysisTf(tfSel.value));
watchSel.addEventListener("change", () => {
  const [sym, market] = watchSel.value.split("|");
  if (sym && isMarket(market) && normaliseSymbol(sym)) {
    follow.checked = false;
    useInstrument({ symbol: sym, market }, "manual");
  }
  watchSel.value = "";
});

function renderWatchlist(): void {
  const wl = app.cachedSettings?.watchlist ?? [];
  setChildren(
    watchSel,
    h("option", { value: "" }, wl.length ? "Watchlist…" : "Watchlist empty"),
    wl.map((w) => h("option", { value: `${w.symbol}|${w.market}` }, `${w.symbol} · ${marketLabel(w.market)}`)),
  );
  watchSel.disabled = wl.length === 0;
}
app.onSettingsChange(() => {
  renderWatchlist();
  if (app.cachedSettings) tfSel.value = app.cachedSettings.analysis_timeframe;
});

chrome.tabs.onActivated.addListener(() => void detectFromTab());
chrome.tabs.onUpdated.addListener((_id, info, tab) => {
  if (tab.active && info.url) void detectFromTab();
});

async function init(): Promise<void> {
  renderWatchlist();
  try {
    const s = await app.settings();
    if (["1h", "4h", "1d", "1w"].includes(s.analysis_timeframe)) tfSel.value = s.analysis_timeframe;
    app.analysisTf = tfSel.value;
    renderWatchlist();
  } catch {
    // Not connected yet: views show their own errors; Settings explains how to connect.
  }
  await detectFromTab();
  if (!app.instrument) {
    const last = (await chrome.storage.local.get("lastInstrument"))["lastInstrument"] as Partial<Instrument> | undefined;
    if (last && typeof last.symbol === "string" && normaliseSymbol(last.symbol) === last.symbol && isMarket(last.market)) {
      useInstrument({ symbol: last.symbol, market: last.market }, "manual", "last used");
    }
  }
  selectTab("analysis");
}

void init();
