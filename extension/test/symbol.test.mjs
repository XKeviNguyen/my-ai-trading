import test from "node:test";
import assert from "node:assert/strict";
import { isValidSymbol, normaliseSymbol, parseBinanceUrl } from "../build/src/symbol.js";

test("futures URLs → usdm_perpetual", () => {
  assert.deepEqual(parseBinanceUrl("https://www.binance.com/en/futures/SANDUSDT"), { symbol: "SANDUSDT", market: "usdm_perpetual" });
  assert.deepEqual(parseBinanceUrl("https://www.binance.com/futures/BTCUSDT"), { symbol: "BTCUSDT", market: "usdm_perpetual" });
  assert.deepEqual(parseBinanceUrl("https://www.binance.com/zh-CN/futures/ethusdt"), { symbol: "ETHUSDT", market: "usdm_perpetual" });
  assert.deepEqual(parseBinanceUrl("https://www.binance.com/pt-BR/futures/1000PEPEUSDT/"), { symbol: "1000PEPEUSDT", market: "usdm_perpetual" });
  assert.deepEqual(parseBinanceUrl("https://www.binance.com/vi/futures/SANDUSDT#chart"), { symbol: "SANDUSDT", market: "usdm_perpetual" });
});

test("spot URLs → spot", () => {
  assert.deepEqual(parseBinanceUrl("https://www.binance.com/en/trade/SAND_USDT?type=spot"), { symbol: "SANDUSDT", market: "spot" });
  assert.deepEqual(parseBinanceUrl("https://www.binance.com/en/trade/BTC_USDT"), { symbol: "BTCUSDT", market: "spot" });
  assert.deepEqual(parseBinanceUrl("https://www.binance.com/ja/trade/eth_btc?type=spot&layout=pro"), { symbol: "ETHBTC", market: "spot" });
});

test("unsupported or malicious URLs → null", () => {
  const bad = [
    undefined,
    null,
    "",
    "not a url",
    "http://www.binance.com/en/futures/SANDUSDT", // not https
    "https://binance.com/en/futures/SANDUSDT", // other host
    "https://www.binance.com.evil.example/en/futures/SANDUSDT",
    "https://evil.example/https://www.binance.com/en/futures/SANDUSDT",
    "https://user:pw@www.binance.com/en/futures/SANDUSDT",
    "https://www.binance.com:8443/en/futures/SANDUSDT",
    "https://www.binance.com/en/futures/BTCUSD_PERP", // coin-margined
    "https://www.binance.com/en/delivery/BTCUSD_PERP",
    "https://www.binance.com/en/trade/SAND_USDT?type=cross", // margin page
    "https://www.binance.com/en/trade/SANDUSDT", // no separator
    "https://www.binance.com/en/trade/A_B_C",
    "https://www.binance.com/en/futures/SAND%3Cscript%3EUSDT",
    "https://www.binance.com/en/futures/..%2F..%2Fapi",
    "https://www.binance.com/en/futures/AB", // too short
    "https://www.binance.com/en/futures/" + "A".repeat(21),
    "https://www.binance.com/en/futures/SANDUSDT/extra",
    "https://www.binance.com/en/markets/overview",
    "https://www.binance.com/en/futures/SANDUSDT?type=spot",
    "javascript:alert(1)",
    "chrome://extensions",
    "https://www.binance.com/en/futures/" + "%E0%A4%A",
  ];
  for (const u of bad) assert.equal(parseBinanceUrl(u), null, String(u));
});

test("symbol validation and normalisation", () => {
  assert.equal(isValidSymbol("SANDUSDT"), true);
  assert.equal(isValidSymbol("sandusdt"), false);
  assert.equal(isValidSymbol("SAND USDT"), false);
  assert.equal(isValidSymbol("AB"), false);
  assert.equal(isValidSymbol(42), false);
  assert.equal(normaliseSymbol(" sand/usdt "), "SANDUSDT");
  assert.equal(normaliseSymbol("sand_usdt"), "SANDUSDT");
  assert.equal(normaliseSymbol("<img src=x>"), null);
  assert.equal(normaliseSymbol(""), null);
});
