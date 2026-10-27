# My AI Trading

**An open research workbench that watches crypto markets, explains what the data shows, and argues with your trade ideas, so you decide with evidence instead of hype.**

Your market data. Your strategy. Your decision.

> [!IMPORTANT]
> **Early development, research first.** Market analysis needs no account. Connecting an exchange is optional: live accounts are read-only, and orders can only be sent to exchange testnets after you confirm each one. This is not financial advice, and nothing here promises profit.

---

## What it does

Ask most "AI trading" tools *"should I buy?"* and you get a confident answer with nothing behind it. My AI Trading asks better questions:

- **What is the data actually showing**, on each timeframe, using only fully closed candles?
- **What supports your idea, what contradicts it**, and what would prove it wrong?
- **Does a trading method even apply here?** A weekly method doesn't turn into an hourly signal just because you're looking at a 1H chart.
- **What does a rule really do historically**, once you include fees, slippage, and funding?

Every alert and every answer shows its evidence: the values, the candle it came from, and the condition that would invalidate it.

## Quick start (about 5 minutes)

You need Linux or macOS with **Python 3.11+**. Ubuntu users also need `sudo apt install python3-venv`. Market data comes from Binance's free public API, so no account or API key is needed.

```bash
git clone https://github.com/XKeviNguyen/my-ai-trading.git
cd my-ai-trading
python3 -m venv .venv
.venv/bin/pip install -e '.[api,vision]'

# Analyze SANDUSDT perpetual on 1H / 4H / 1D / 1W from live public data
.venv/bin/my-ai-trading analyze --symbol SANDUSDT
```

You'll get a JSON report for each timeframe: trend, moving averages, RSI, ATR, support and resistance, and any events such as breakouts. Each event comes with what would invalidate it.

Try it offline first if you prefer: `.venv/bin/my-ai-trading demo` runs on clearly labeled synthetic data.

### Run the app with the Chrome side panel

```bash
.venv/bin/my-ai-trading token     # creates a private access token (file mode 0600)
.venv/bin/my-ai-trading serve     # local engine on http://127.0.0.1:8765

cd extension && npm ci && npm run build
```

1. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and choose `extension/dist`.
2. Open the side panel, go to **Settings**, and paste the token from `~/.config/my-ai-trading/api_token`.
3. Open a Binance futures or spot page. The panel picks up the symbol from the page address and shows the analysis next to your chart.

See [docs/EXTENSION.md](docs/EXTENSION.md) for permissions and troubleshooting. Prefer a desktop window? See [docs/DESKTOP.md](docs/DESKTOP.md).

## Features

This is the product roadmap with an honest status for each item.

✅ available · 🧪 experimental (works, but limited or unvalidated) · 🗓️ planned

### Market analysis

| Feature | Status | What you get today |
|---|---|---|
| Auto market monitoring | ✅ | Watches your watchlist and runs once per newly closed candle on each timeframe. Duplicate-proof alerts, backoff on outages, stale-data detection. |
| Multi-timeframe analysis | ✅ | Any interval from 1m to 1W, plus calendar months (1M), with a summary of where timeframes agree or conflict. |
| Technical analysis engine | ✅ | SMA, EMA, RSI, MACD, Bollinger Bands, ADX/DI, ATR (Wilder and simple), volume ratio, prior highs and lows. Each formula and its warm-up period is documented and tested. |
| Chart pattern recognition | 🧪 | VCP, cup and handle, head and shoulders (and inverse), flags, triangles, wedges, and Darvas boxes, all with explicit rules and invalidation levels. These detectors also fire on random data, so treat them as prompts to look. |
| Market structure | 🧪 | Trend state, support and resistance, breakouts, breakdowns, failed breakouts, and supply and demand zones. |
| Smart alerts | ✅ | Breakouts, failed breakouts, moving-average reclaims and losses, unusual volume, confirmed patterns, your strategy matches, and method state changes. Quiet hours and pause. |

### Copilot and Strategy Studio

| Feature | Status | What you get today |
|---|---|---|
| Interactive copilot | 🧪 | Ask about the current symbol. Facts always come from the engine. An optional AI model adds clearly labeled interpretation when you supply your own API key. |
| Chart screenshots and deep reasoning | 🧪 | Local screenshot analysis with OpenCV and OCR: candle colors, labels, and price-axis text, all checked against exchange data. Price-axis OCR reads 99.5% of labels exactly on a held-out synthetic benchmark ([method and limits](docs/OCR_BENCHMARK.md)); real screenshots are not benchmarked yet. Optional AI vision and in-depth analysis. |
| 20 trader-inspired profiles | 🧪 | All 20 are listed with their documented timeframes and data needs. Seven have experimental rule checks: Minervini (price criteria), Weinstein, the Turtles, Darvas, Larry Williams, Linda Raschke, and Peter Brandt (classical patterns). The rest need data the app doesn't have yet (fundamentals, macro, news) or have no published rules. |
| Custom Strategy Studio | ✅ | Describe a rule in plain English. Preview it, dry-run it on past candles, and turn it on only after approving that exact version. You can switch off every preset. |
| Challenge My Thesis | ✅ | Lays out the evidence for and against your idea, your hidden assumptions, the unknowns, and the price levels that would prove you wrong. |
| Method and timeframe fidelity | ✅ | Each method runs as Native, Hybrid, Adapted, or Custom, and is labeled that way. A 30-week moving average is never quietly swapped for a 30-hour one. |

### Research and machine learning

| Feature | Status | What you get today |
|---|---|---|
| ML signal research | 🧪 | Leak-free datasets, walk-forward folds with a purge gap, and logistic regression and LightGBM compared against simple baselines, with calibration and leakage checks. Install with `pip install -e '.[ml]'`. |
| Backtesting lab | ✅ | Next-bar fills, fees, slippage, perpetual funding, ATR stops, and walk-forward folds. Results are always labeled hypothetical. |
| AI cost control | 🧪 | Engine first, AI only when useful. Monthly token and dollar budgets, a usage dashboard, and consent before any data leaves your machine. Self-hosted or fine-tuned models are planned. |

### Exchange accounts (testnet trading)

| Feature | Status | What you get today |
|---|---|---|
| Exchange connections | 🧪 | Binance, Bybit and OKX through API keys, and Interactive Brokers paper accounts through IBKR's own gateway. Live accounts must use read-only keys: keys that can trade, withdraw or transfer are refused. Keys stay in memory or your operating system's credential vault, with an expiry you choose (default: this session only). |
| Portfolio and positions | 🧪 | Balances, positions, open orders, exposure by asset and direction, margin use, distance to liquidation, and risk warnings with your own thresholds. Every number says whether the exchange reported it or the app calculated or estimated it. |
| Testnet Trading Mode | 🧪 | Off by default. Orders go only to testnet or demo accounts, after a preview against the exchange's rules and your explicit confirmation. Orders whose outcome is unknown are looked up, never resent. The AI can never place an order. |
| Performance from fills | 🧪 | Win rate, expectancy, profit factor, drawdown and fees from your executed trades. |
| One-click exchange sign-in (OAuth) | 🗓️ | Needs partner approval from each exchange (**blocked by provider approval**). |

Details and the safety rules: [docs/ACCOUNTS_TRADING.md](docs/ACCOUNTS_TRADING.md).

### Platform

| Feature | Status | What you get today |
|---|---|---|
| Chrome extension | ✅ | Side panel with analysis, copilot, studio, alerts, usage, and settings. Recognizes Binance USD-M futures and spot pages. |
| Desktop app | 🧪 | Native PySide6 app with the same features as the side panel, talking to the same local engine. Tested offscreen; not yet tried on a real display. See [docs/DESKTOP.md](docs/DESKTOP.md). |
| Multi-market workspace | 🧪 | Watchlists, alert history, and saved analysis reports you can export as Markdown, for Binance crypto. Stocks and other markets are planned. There is no portfolio or position tracking, because the app is read-only by design. |

## How it works

```text
Binance public data ──► Validated closed candles ──► Python engine (indicators, events)
                                                          │
            Your rules and selected methods ──────────────┤  checked for applicability first
                                                          ▼
               Alerts · Copilot · Challenge My Thesis · Backtests
                                                          │
                     Optional AI (your key, your budget, your consent)
```

- **Numbers come from code, not from a language model.** Indicators and rules are computed in Python and covered by tests.
- **Closed candles only.** A candle that is still forming never counts as confirmed, and nothing looks into the future.
- **AI is optional and gated.** Without an AI key, everything above still works deterministically, at zero cost.

## Configuration

Copy `.env.example` to `.env` if you want the optional AI features:

| Setting | Purpose |
|---|---|
| `MY_AI_TRADING_MODEL` | The AI model to use for the copilot. Leave it empty to stay fully local. |
| `MY_AI_TRADING_PRICES` | Optional price per million tokens, so the usage dashboard can estimate cost. |

Install the AI extra with `.venv/bin/pip install -e '.[ai]'`. The SDK reads its API key from its standard environment variable. For text recognition in screenshots, also install Tesseract: `sudo apt install tesseract-ocr` on Ubuntu or `brew install tesseract` on macOS.

## Safety and privacy

- **Read-only by default.** Analysis needs no account. Live exchange connections are read-only; testnet orders need Trading Mode plus your confirmation for each order. No auto-trading, withdrawals or transfers.
- **Local-first.** The engine listens only on `127.0.0.1` and requires a private token. Screenshots are processed in memory and never stored.
- **Your data stays yours.** Nothing is sent to an AI provider unless you turn on consent and configure a key.
- **No impersonation.** Trader profiles are research summaries of publicly described methods. They aren't endorsed by those traders and don't claim what anyone would personally do.

## Development

```bash
.venv/bin/pip install -e '.[api,test,vision,ml]'
.venv/bin/python -m pytest              # Python tests (offline)
cd extension && npm test                 # extension tests
```

Contributors should read [AGENTS.md](AGENTS.md) and [docs/SECURITY.md](docs/SECURITY.md) first. The secret guard (`python3 tools/secret_guard.py --staged`) must pass before every commit.

## Learn more

- [Product requirements](docs/PRODUCT_REQUIREMENTS.md): the full vision and acceptance scenarios
- [Roadmap](docs/ROADMAP.md) and [current progress](docs/PROGRESS.md)
- [Chrome extension guide](docs/EXTENSION.md) and [desktop app guide](docs/DESKTOP.md)
- [Security policy](docs/SECURITY.md)

## License and disclaimer

No license has been chosen yet, so all rights are reserved for now. This software is provided for research and education. Markets are risky, past results don't predict future returns, and every decision you make is your own.
