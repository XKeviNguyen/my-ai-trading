// Exchange connections, portfolio, positions, testnet orders and performance (R8; AGENTS.md §2).
// Keys are sent once to the local engine and cleared from the form; nothing here stores them.
// Live connections are read-only; orders exist only for testnet connections with Trading Mode on.

import type { App, View } from "../app.js";
import { badge, field, h, list, setChildren, status } from "../dom.js";
import { fmtMs } from "../format.js";
import type {
  AccountSnapshot, ConnectionMeta, ExchangesResponse, OrderDraft, TradePreview, TradeResult,
} from "../types.js";

type Section = "connections" | "portfolio" | "positions" | "orders" | "performance";
const SECTIONS: [Section, string][] = [
  ["connections", "Connections"], ["portfolio", "Portfolio"], ["positions", "Positions"],
  ["orders", "Orders"], ["performance", "Performance"],
];
const LIFETIME_LABELS: Record<string, string> = {
  session: "This session only (default)", "1h": "1 hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days",
  until_removed: "Until I remove it",
};
const PROVENANCE: Record<string, string> = { exchange_reported: "reported by exchange", derived: "calculated", estimate: "estimate" };

function yesNo(v: boolean | null): string {
  return v === null ? "unknown" : v ? "yes" : "no";
}

function expiry(c: ConnectionMeta): string {
  if (c.lifetime === "session") return "ends when the engine stops";
  return c.expires_ms ? `expires ${fmtMs(c.expires_ms)}` : "until you remove it";
}

function table(caption: string, head: string[], rows: (string | HTMLElement)[][]): HTMLElement {
  return h(
    "div",
    { class: "table-wrap", tabindex: "0", role: "region", "aria-label": caption },
    h(
      "table",
      { class: "matrix" },
      h("caption", { class: "sr-only" }, caption),
      h("thead", null, h("tr", null, head.map((c) => h("th", { scope: "col" }, c)))),
      h("tbody", null, rows.map((r) => h("tr", null, r.map((c, i) => (i === 0 ? h("th", { scope: "row" }, c) : h("td", null, c)))))),
    ),
  );
}

export class AccountsView implements View {
  private readonly root: HTMLElement;
  private readonly app: App;
  private section: Section = "connections";
  private readonly body = h("div", { class: "accounts-body" });
  private catalog: ExchangesResponse | null = null;
  private connections: ConnectionMeta[] = [];
  private countdown: ReturnType<typeof setInterval> | null = null;
  private env: "live" | "testnet" | undefined;  // portfolio view: live and testnet are never added together

  constructor(root: HTMLElement, app: App) {
    this.root = root;
    this.app = app;
    const nav = h(
      "div",
      { class: "subnav", role: "group", "aria-label": "Account sections" },
      SECTIONS.map(([id, label]) =>
        h("button", { type: "button", "data-section": id, "aria-pressed": String(id === this.section), onclick: () => this.go(id) }, label),
      ),
    );
    setChildren(this.root, nav, this.body);
  }

  show(): void {
    void this.render();
  }

  private go(id: Section): void {
    this.section = id;
    for (const b of this.root.querySelectorAll<HTMLButtonElement>(".subnav button")) {
      b.setAttribute("aria-pressed", String(b.dataset["section"] === id));
    }
    void this.render();
  }

  private stopCountdown(): void {
    if (this.countdown !== null) clearInterval(this.countdown);
    this.countdown = null;
  }

  private async render(): Promise<void> {
    this.stopCountdown();
    setChildren(this.body, status("loading", "Loading…"));
    try {
      this.connections = (await this.app.client.connections()).connections;
      if (this.section === "connections") await this.renderConnections();
      else if (this.section === "portfolio") await this.renderPortfolio();
      else if (this.section === "positions") await this.renderPositions();
      else if (this.section === "orders") await this.renderOrders();
      else this.renderPerformance();
    } catch (err) {
      setChildren(this.body, this.app.errorView(err));
    }
  }

  // ---------- connections ----------
  private async renderConnections(): Promise<void> {
    this.catalog ??= await this.app.client.exchanges();
    const cards = this.connections.map((c) => this.connectionCard(c));
    setChildren(
      this.body,
      h("p", { class: "small muted" }, "Market analysis never needs an account. Connect an exchange only to see your own balances, positions and orders."),
      h("h3", null, `Connected (${this.connections.length})`),
      cards.length ? h("div", { class: "cards" }, cards) : h("p", { class: "muted" }, "No exchange connected."),
      this.connectForm(this.catalog),
      h(
        "p",
        { class: "small muted" },
        "One-click sign-in with Binance, Bybit or OKX (OAuth) needs partner approval from each exchange and is not available yet. Use an API key instead.",
      ),
    );
  }

  private connectionCard(c: ConnectionMeta): HTMLElement {
    const p = c.permissions;
    const msg = h("div", { "aria-live": "polite" });
    const canTrade = c.environment === "testnet" && (p.spot_trade || p.derivatives_trade);
    return h(
      "article",
      { class: "card" },
      h(
        "header",
        { class: "card-head" },
        h("h4", null, c.label || c.exchange),
        c.environment === "live" ? badge("Live · read-only", "ok") : badge("Testnet", "info"),
        c.trading_enabled ? badge("Trading Mode on", "warn") : null,
      ),
      h(
        "dl",
        { class: "kv" },
        h("dt", null, "Key"), h("dd", null, `••••${c.key_hint}`),
        h("dt", null, "Stored"), h("dd", null, `${LIFETIME_LABELS[c.lifetime] ?? c.lifetime}; ${expiry(c)}`),
        h("dt", null, "Permissions"),
        h("dd", null, `read ${yesNo(p.read)} · trade ${yesNo(p.spot_trade || p.derivatives_trade)} · withdraw ${yesNo(p.withdraw)} · transfer ${yesNo(p.transfer)}`),
        h("dt", null, "IP restricted"), h("dd", null, yesNo(p.ip_restricted)),
      ),
      h(
        "div",
        { class: "toolbar" },
        canTrade
          ? h("button", { type: "button", onclick: () => void this.toggleTrading(c, msg) }, c.trading_enabled ? "Turn Trading Mode off" : "Turn Trading Mode on…")
          : null,
        h("button", { type: "button", class: "danger", onclick: () => void this.disconnect(c, msg) }, "Disconnect"),
      ),
      msg,
    );
  }

  private async toggleTrading(c: ConnectionMeta, msg: HTMLElement): Promise<void> {
    try {
      if (c.trading_enabled) {
        await this.app.client.setTradingMode(c.connection_id, false, null);
        return void this.render();
      }
      const consent = (await this.app.client.tradingConsent()).consent_text;
      setChildren(
        msg,
        h(
          "div",
          { class: "status status-info confirm-box" },
          h("p", null, consent),
          h("div", { class: "toolbar" },
            h("button", { type: "button", class: "primary", onclick: async () => {
              try {
                await this.app.client.setTradingMode(c.connection_id, true, consent);
                void this.render();
              } catch (err) {
                setChildren(msg, this.app.errorView(err));
              }
            } }, "I agree, turn Trading Mode on"),
            h("button", { type: "button", onclick: () => setChildren(msg) }, "Cancel")),
        ),
      );
    } catch (err) {
      setChildren(msg, this.app.errorView(err));
    }
  }

  private async disconnect(c: ConnectionMeta, msg: HTMLElement): Promise<void> {
    if (!confirm(`Disconnect ${c.label || c.exchange}? The key is deleted from this computer. It keeps working at the exchange until you delete it there.`)) return;
    try {
      const r = await this.app.client.disconnect(c.connection_id);
      setChildren(msg, status("ok", r.note));
      void this.render();
    } catch (err) {
      setChildren(msg, this.app.errorView(err));
    }
  }

  private connectForm(catalog: ExchangesResponse): HTMLElement {
    const ex = h("select", null, catalog.exchanges.map((e) => h("option", { value: e.id }, e.name)));
    const env = h("select", null, h("option", { value: "testnet" }, "Testnet / demo"), h("option", { value: "live" }, "Live (read-only key)"));
    const life = h("select", null, catalog.lifetimes.map((l) => h("option", { value: l }, LIFETIME_LABELS[l] ?? l)));
    life.value = catalog.default_lifetime;
    const label = h("input", { type: "text", maxlength: "60", autocomplete: "off" });
    const key = h("input", { type: "password", autocomplete: "off", spellcheck: "false" });
    const secret = h("input", { type: "password", autocomplete: "off", spellcheck: "false" });
    const keyField = field("API key", key);
    const secretField = field("API secret", secret);
    const pass = h("input", { type: "password", autocomplete: "off", spellcheck: "false" });
    const passField = field("Passphrase", pass, "Required for this exchange.");
    const notes = h("div", { class: "small muted" });
    const msg = h("div", { "aria-live": "polite" });
    const sync = () => {
      const info = catalog.exchanges.find((e) => e.id === ex.value);
      passField.hidden = !info?.needs_passphrase;
      keyField.querySelector("label")!.textContent = info?.key_label ?? "API key";
      secretField.querySelector("label")!.textContent = info?.secret_label ?? "API secret";
      for (const o of env.options) o.disabled = !!info && !info.environments.includes(o.value as "live" | "testnet");
      if (env.selectedOptions[0]?.disabled) env.value = info?.environments[0] ?? "testnet";
      setChildren(notes, info ? list(info.notes, "") : null, info ? h("p", null, `Create keys at: ${info.key_setup_url}`) : null);
    };
    ex.addEventListener("change", sync);
    sync();
    const submit = async (e: Event) => {
      e.preventDefault();
      const creds = { api_key: key.value.trim(), api_secret: secret.value.trim(), ...(passField.hidden ? {} : { passphrase: pass.value }) };
      const body = { exchange: ex.value, environment: env.value as "live" | "testnet", lifetime: life.value, label: label.value.trim(), ...creds };
      key.value = secret.value = pass.value = ""; // never keep secrets in the page longer than needed
      setChildren(msg, status("loading", "Checking the key with the exchange…"));
      try {
        const r = await this.app.client.connect(body);
        setChildren(msg, status("ok", `Connected. ${r.note}`));
        void this.render();
      } catch (err) {
        setChildren(msg, this.app.errorView(err));
      }
    };
    return h(
      "form",
      { class: "card stack", onsubmit: submit },
      h("h3", { class: "inline" }, "Connect an exchange"),
      h("p", { class: "small" }, "Live keys must be read-only: keys that can trade, withdraw or transfer are refused. Orders are only ever sent to testnet/demo accounts."),
      h("div", { class: "row" }, field("Exchange", ex), field("Account", env)),
      field("Keep the key", life, "Session only: the key is forgotten when the engine stops. Longer options use your operating system's credential vault."),
      field("Label (optional)", label),
      keyField,
      secretField,
      passField,
      notes,
      h("button", { type: "submit", class: "primary" }, "Check and connect"),
      msg,
    );
  }

  // ---------- portfolio & positions ----------
  private envSwitch(p: { environment: string; available_environments: string[] }, rerender: () => void): HTMLElement | null {
    if (p.available_environments.length < 2) return null;
    const sel = h("select", null, p.available_environments.map((e) => h("option", { value: e }, e === "live" ? "Live accounts" : "Testnet accounts")));
    sel.value = p.environment;
    sel.addEventListener("change", () => { this.env = sel.value as "live" | "testnet"; rerender(); });
    return field("Accounts shown", sel, "Live and testnet funds are never added together.");
  }

  private async renderPortfolio(): Promise<void> {
    if (!this.connections.length) return this.needConnection();
    const p = await this.app.client.portfolio(this.env);
    setChildren(
      this.body,
      this.envSwitch(p, () => void this.render()),
      h(
        "section",
        { class: "headline", "aria-label": "Portfolio value" },
        h("div", { class: "headline-top" },
          h("h2", { class: "headline-symbol" }, "Portfolio"),
          p.environment === "live" ? badge("Live · read-only", "ok") : badge("Testnet", "info"),
          h("p", { class: "headline-close" }, h("span", { class: "headline-close-label" }, `Total (${PROVENANCE[p.total_value_usd.provenance]})`), `$${p.total_value_usd.value}`)),
        h("p", { class: "headline-meta" }, p.total_value_usd.note),
      ),
      p.warnings.length ? h("section", { class: "card contra", "aria-label": "Risk warnings" }, h("h4", null, `Risk warnings (${p.warnings.length})`), list(p.warnings)) : null,
      p.errors.length ? h("section", { class: "card lane lane-fault" }, h("h4", null, "Connections with errors"), list(p.errors.map((e) => `${e.exchange}: ${e.error}`))) : null,
      h("h3", null, "Allocation"),
      p.allocation.length
        ? table("Exposure by asset", ["Asset", "Exposure (USD)", "Share"], p.allocation.map((a) => [a.asset, a.exposure_usd, a.share_pct ? `${a.share_pct}%` : "—"]))
        : h("p", { class: "muted" }, "No priced holdings or positions."),
      h("h3", null, "Exposure"),
      table("Exposure by direction", ["Direction", "USD"], [["Long", p.exposure_by_direction.long], ["Short", p.exposure_by_direction.short], ["Net", p.net_exposure_usd]]),
      Object.keys(p.by_exchange).length
        ? table("Value by exchange", ["Exchange", "USD"], Object.entries(p.by_exchange).map(([k, v]) => [k, v])) : null,
      p.margin.length
        ? table("Margin use", ["Exchange", "Wallet", "Maint. margin / equity"], p.margin.map((m) => [m.exchange, m.wallet, `${m.maintenance_margin_pct_of_equity}%`])) : null,
      p.unpriced_assets.length ? h("p", { class: "small muted" }, `Not valued (no public price): ${p.unpriced_assets.join(", ")}`) : null,
    );
  }

  private async renderPositions(): Promise<void> {
    if (!this.connections.length) return this.needConnection();
    const p = await this.app.client.portfolio(this.env);
    setChildren(
      this.body,
      this.envSwitch(p, () => void this.render()),
      h("h3", null, `Open positions (${p.positions.length}, ${p.environment})`),
      p.positions.length
        ? table("Open positions", ["Symbol", "Side", "Size", "Notional", "Unreal. P&L", "Leverage", "To liq."],
            p.positions.map((x) => [`${x.symbol} · ${x.exchange}`, x.side, x.quantity, x.notional_usd ?? "—", x.unrealized_pnl ?? "—",
                                    x.leverage ? `${x.leverage}×` : "—", x.liquidation_distance_pct ? `${x.liquidation_distance_pct}%` : "—"]))
        : h("p", { class: "muted" }, "No open positions."),
      h("p", { class: "small muted" }, "Values are reported by the exchange; distance to liquidation is calculated from its mark and liquidation prices."),
    );
  }

  private needConnection(): void {
    setChildren(this.body, h("div", { class: "empty" }, h("p", null, "Connect an exchange first."),
      h("button", { type: "button", onclick: () => this.go("connections") }, "Open Connections")));
  }

  private connectionPicker(filter: (c: ConnectionMeta) => boolean = () => true): HTMLSelectElement {
    return h("select", null, this.connections.filter(filter).map((c) =>
      h("option", { value: c.connection_id }, `${c.label || c.exchange} (${c.environment})`)));
  }

  // ---------- orders ----------
  private async renderOrders(): Promise<void> {
    if (!this.connections.length) return this.needConnection();
    this.catalog ??= await this.app.client.exchanges();
    const pick = this.connectionPicker();
    const area = h("div");
    const load = async () => {
      this.stopCountdown();
      setChildren(area, status("loading", "Loading orders…"));
      try {
        const conn = this.connections.find((c) => c.connection_id === pick.value)!;
        const [snap, journal] = await Promise.all([this.app.client.snapshot(conn.connection_id), this.app.client.journal()]);
        setChildren(area, ...this.ordersFor(conn, snap, journal.orders.filter((o) => o.connection_id === conn.connection_id), journal.needs_reconcile));
      } catch (err) {
        setChildren(area, this.app.errorView(err));
      }
    };
    pick.addEventListener("change", () => void load());
    setChildren(this.body, field("Connection", pick), area);
    await load();
  }

  private ordersFor(conn: ConnectionMeta, snap: AccountSnapshot, journal: { client_order_id: string; state: string; message: string | null; request_json: string }[], needsReconcile: string[]): HTMLElement[] {
    const open = snap.open_orders;
    const out: HTMLElement[] = [
      h("h3", null, `Open orders (${open.length})`),
      open.length
        ? table("Open orders", ["Symbol", "Side", "Type", "Price", "Qty", "Filled", "Status"],
            open.map((o) => [o.symbol, o.side, o.type, o.price ?? "market", o.quantity ?? "—", o.filled_quantity ?? "—", o.status]))
        : h("p", { class: "muted" }, "No open orders."),
    ];
    if (snap.unavailable.length) out.push(h("p", { class: "small muted" }, `Not available: ${snap.unavailable.join("; ")}`));
    if (conn.environment !== "testnet") {
      out.push(h("p", { class: "small muted" }, "Live connections are read-only in this app. Orders can be placed only on testnet/demo connections."));
      return out;
    }
    if (!conn.trading_enabled) {
      out.push(h("p", { class: "small" }, "Trading Mode is off for this testnet connection. Turn it on in Connections to place testnet orders."));
    } else {
      out.push(this.ticket(conn));
    }
    if (journal.length) {
      out.push(h("h3", null, "Order journal (this app)"));
      out.push(h("ul", { class: "plain" }, journal.map((j) => this.journalRow(j, needsReconcile.includes(j.client_order_id)))));
    }
    return out;
  }

  private journalRow(j: { client_order_id: string; state: string; message: string | null; request_json: string }, stuck: boolean): HTMLElement {
    let summary = j.client_order_id;
    try {
      const r = JSON.parse(j.request_json) as Record<string, string>;
      summary = `${r["side"] ?? ""} ${r["quantity"] ?? ""} ${r["symbol"] ?? ""} ${r["type"] ?? ""}${r["price"] && r["price"] !== "None" ? ` @ ${r["price"]}` : ""}`;
    } catch { /* keep the id */ }
    const msg = h("div", { "aria-live": "polite" });
    const act = async (fn: () => Promise<TradeResult>) => {
      try {
        const r = await fn();
        setChildren(msg, status(r.state === "UNKNOWN" ? "info" : "ok", `${r.state}${r.note ? ` — ${r.note}` : ""}`));
      } catch (err) {
        setChildren(msg, this.app.errorView(err));
      }
    };
    const live = ["NEW", "PARTIALLY_FILLED"].includes(j.state);
    return h(
      "li",
      null,
      h("div", { class: "event-head" }, h("strong", null, summary), badge(j.state, stuck ? "warn" : j.state === "REJECTED" ? "err" : "muted")),
      j.message ? h("p", { class: "small muted" }, j.message) : null,
      h("div", { class: "toolbar" },
        h("button", { type: "button", onclick: () => void act(() => this.app.client.reconcile(j.client_order_id)) }, "Check status"),
        live ? h("button", { type: "button", class: "danger", onclick: () => {
          if (confirm("Cancel this testnet order?")) void act(() => this.app.client.cancelOrder(j.client_order_id));
        } }, "Cancel order") : null),
      msg,
    );
  }

  private ticket(conn: ConnectionMeta): HTMLElement {
    const symbol = h("input", { type: "text", value: this.app.instrument?.symbol ?? "BTCUSDT", maxlength: "30", autocomplete: "off", spellcheck: "false" });
    const MARKET_NAMES: Record<string, string> = { spot: "Spot", usdm_perpetual: "USD-M perpetual", stock: "Stock" };
    const offered = this.catalog?.exchanges.find((e) => e.id === conn.exchange)?.markets ?? ["spot", "usdm_perpetual"];
    const market = h("select", null, offered.filter((m) => m in MARKET_NAMES).map((m) => h("option", { value: m }, MARKET_NAMES[m]!)));
    const side = h("select", null, h("option", { value: "buy" }, "Buy"), h("option", { value: "sell" }, "Sell"));
    const type = h("select", null, h("option", { value: "limit" }, "Limit"), h("option", { value: "market" }, "Market"));
    const qty = h("input", { type: "text", inputmode: "decimal", autocomplete: "off" });
    const price = h("input", { type: "text", inputmode: "decimal", autocomplete: "off" });
    const reduce = h("input", { type: "checkbox" });
    const priceField = field("Limit price", price);
    const out = h("div", { "aria-live": "polite" });
    type.addEventListener("change", () => { priceField.hidden = type.value === "market"; });
    const preview = async (e: Event) => {
      e.preventDefault();
      this.stopCountdown();
      const draft: OrderDraft = { symbol: symbol.value.trim().toUpperCase(), market: market.value as OrderDraft["market"],
        side: side.value as OrderDraft["side"], type: type.value as OrderDraft["type"], quantity: qty.value.trim(),
        ...(type.value === "limit" ? { price: price.value.trim() } : {}), ...(reduce.checked ? { reduce_only: true } : {}) };
      setChildren(out, status("loading", "Checking the order against exchange rules…"));
      try {
        setChildren(out, this.previewCard(await this.app.client.previewOrder(conn.connection_id, draft), out));
      } catch (err) {
        setChildren(out, this.app.errorView(err));
      }
    };
    return h(
      "form",
      { class: "card stack", onsubmit: preview },
      h("h3", { class: "inline" }, "Testnet order"),
      h("p", { class: "small" }, "Sent only to this testnet/demo account, and only after you review the preview and confirm."),
      h("div", { class: "row" }, field("Symbol", symbol), field("Market", market)),
      h("div", { class: "row" }, field("Side", side), field("Type", type)),
      h("div", { class: "row" }, field("Quantity", qty), priceField),
      h("label", { class: "check" }, reduce, "Reduce only (futures)"),
      h("button", { type: "submit" }, "Preview order"),
      out,
    );
  }

  private previewCard(p: TradePreview, out: HTMLElement): HTMLElement {
    const o = p.order;
    const left = h("span", null, `${Math.max(0, Math.floor(p.expires_in_s))} s`);
    const place = h("button", { type: "button", class: "primary" }, "Place testnet order");
    let remaining = Math.floor(p.expires_in_s);
    this.countdown = setInterval(() => {
      remaining -= 1;
      left.textContent = `${Math.max(0, remaining)} s`;
      if (remaining <= 0) {
        this.stopCountdown();
        place.disabled = true;
        left.textContent = "expired — preview again";
      }
    }, 1000);
    place.addEventListener("click", async () => {
      this.stopCountdown();
      place.disabled = true;
      try {
        const r = await this.app.client.submitOrder(p.preview_id, p.confirm_token);
        setChildren(out, status(r.state === "UNKNOWN" || r.state === "NOT_FOUND" ? "info" : "ok",
          `${r.state}: ${r.note ?? ""}`), h("button", { type: "button", onclick: () => void this.render() }, "Refresh orders"));
      } catch (err) {
        setChildren(out, this.app.errorView(err));
      }
    });
    const estimates = Object.entries(p.estimates).map(([k, v]) => {
      const e = v as { value?: string | null; asset?: string; provenance?: string; note?: string; side?: string; quantity?: string };
      const value = e.value !== undefined ? (e.value ?? "not estimated") : `${e.side ?? ""} ${e.quantity ?? ""}`;
      return `${k.replaceAll("_", " ")}: ${value}${e.asset ? ` ${e.asset}` : ""} (${PROVENANCE[e.provenance ?? ""] ?? e.provenance ?? ""})${e.note ? ` — ${e.note}` : ""}`;
    });
    return h(
      "section",
      { class: "card confirm-box", "aria-label": "Order preview" },
      h("h4", null, `${String(o["side"]).toUpperCase()} ${o["quantity"]} ${o["symbol"]} · ${o["type"]}${o["price"] ? ` @ ${o["price"]}` : ""}`),
      h("p", { class: "small muted" }, `Testnet · ${p.exchange} · client order id ${o["client_order_id"]}`),
      h("h5", null, "Exchange rule checks"),
      list(p.checks.map((c) => `${c.ok ? "OK" : "FAILED"} — ${c.detail}`)),
      h("h5", null, "Estimates"),
      list(estimates),
      p.warnings.length ? h("div", { class: "contra card" }, h("h5", null, "Warnings"), list(p.warnings)) : null,
      h("div", { class: "toolbar" }, place, h("span", { class: "small muted" }, "Confirm within ", left)),
    );
  }

  // ---------- performance ----------
  private renderPerformance(): void {
    if (!this.connections.length) return this.needConnection();
    const pick = this.connectionPicker();
    const symbol = h("input", { type: "text", value: this.app.instrument?.symbol ?? "BTCUSDT", maxlength: "30", autocomplete: "off" });
    const market = h("select", null, h("option", { value: "spot" }, "Spot"), h("option", { value: "usdm_perpetual" }, "USD-M perpetual"));
    const out = h("div", { "aria-live": "polite" });
    const run = async (e: Event) => {
      e.preventDefault();
      setChildren(out, status("loading", "Reading your fills…"));
      try {
        const r = await this.app.client.performance(pick.value, symbol.value.trim().toUpperCase(), market.value);
        if (!r.trades) return void setChildren(out, h("p", { class: "muted" }, r.note ?? "No closed trades."));
        const rows: [string, string][] = [
          ["Closed trades", String(r.trades)], ["Win rate", `${r.win_rate_pct}%`], ["Net P&L", r.net_pnl ?? "—"],
          ["Expectancy / trade", r.expectancy_per_trade ?? "—"], ["Average win", r.average_win ?? "—"],
          ["Average loss", r.average_loss ?? "—"], ["Profit factor", r.profit_factor ?? "—"],
          ["Max drawdown", r.max_drawdown ?? "—"], ["Fees paid", r.fees_paid ?? "—"],
        ];
        setChildren(out, h("section", { class: "card" },
          h("dl", { class: "kv" }, rows.map(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
          r.warning ? h("p", { class: "invalidation" }, r.warning) : null,
          r.fills_with_fees_in_other_assets ? h("p", { class: "small muted" }, `${r.fills_with_fees_in_other_assets} fills paid fees in another asset (not subtracted).`) : null,
          r.unmatched_sells ? h("p", { class: "small muted" }, `${r.unmatched_sells} sells had no earlier buy in the loaded history and were not counted as trades.`) : null,
          h("p", { class: "small muted" }, `${(r.provenance ?? []).map((x) => PROVENANCE[x] ?? x).join(", ")}. ${r.note ?? ""}`)));
      } catch (err) {
        setChildren(out, this.app.errorView(err));
      }
    };
    setChildren(this.body, h("form", { class: "card stack", onsubmit: run },
      h("h3", { class: "inline" }, "Performance from your fills"),
      field("Connection", pick), h("div", { class: "row" }, field("Symbol", symbol), field("Market", market)),
      h("button", { type: "submit" }, "Calculate")), out);
  }
}
