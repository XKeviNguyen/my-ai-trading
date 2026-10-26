// Instrument detection from the active tab URL ONLY. The page DOM and chart data are never read.

export type Market = "usdm_perpetual" | "spot";

export interface Instrument {
  symbol: string;
  market: Market;
}

export const SYMBOL_RE = /^[A-Z0-9]{3,20}$/;
export const MARKETS: readonly Market[] = ["usdm_perpetual", "spot"];

const BINANCE_ORIGIN = "https://www.binance.com";
// Locale path segment such as "en", "vi", "zh-CN", "pt-BR".
const LOCALE_RE = /^[a-z]{2}(?:-[A-Za-z]{2,4})?$/;
const ASSET_RE = /^[A-Z0-9]{1,15}$/;

export function isValidSymbol(value: unknown): value is string {
  return typeof value === "string" && SYMBOL_RE.test(value);
}

export function isMarket(value: unknown): value is Market {
  return value === "usdm_perpetual" || value === "spot";
}

/** Normalise user-typed text into a symbol, or null if it is not a valid symbol. */
export function normaliseSymbol(raw: string): string | null {
  const s = raw.trim().toUpperCase().replace(/[\s/_-]/g, "");
  return isValidSymbol(s) ? s : null;
}

/**
 * Parse a Binance trading page URL:
 *   https://www.binance.com/en/futures/SANDUSDT          -> SANDUSDT, usdm_perpetual
 *   https://www.binance.com/en/trade/SAND_USDT?type=spot -> SANDUSDT, spot
 * Anything else (other hosts, coin-margined contracts, margin pages, odd paths) returns null.
 */
export function parseBinanceUrl(raw: string | undefined | null): Instrument | null {
  if (typeof raw !== "string" || raw.length > 2048) return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.origin !== BINANCE_ORIGIN || url.username || url.password) return null;

  let segments = url.pathname.split("/").filter((s) => s.length > 0);
  if (segments.length > 0 && LOCALE_RE.test(segments[0]!)) segments = segments.slice(1);
  if (segments.length !== 2) return null;
  const [section, rawPair] = segments as [string, string];
  let pair: string;
  try {
    pair = decodeURIComponent(rawPair).toUpperCase();
  } catch {
    return null;
  }

  if (section === "futures") {
    // USD-M perpetuals only; coin-margined (BTCUSD_PERP) and dated contracts contain "_".
    if (url.searchParams.has("type")) return null;
    return isValidSymbol(pair) ? { symbol: pair, market: "usdm_perpetual" } : null;
  }
  if (section === "trade") {
    const type = url.searchParams.get("type");
    if (type !== null && type !== "spot") return null; // margin/grid pages: let the user choose
    const parts = pair.split("_");
    if (parts.length !== 2 || !parts.every((p) => ASSET_RE.test(p))) return null;
    const symbol = parts.join("");
    return isValidSymbol(symbol) ? { symbol, market: "spot" } : null;
  }
  return null;
}

export function marketLabel(market: Market): string {
  return market === "usdm_perpetual" ? "USD-M perpetual" : "Spot";
}
