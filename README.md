# My AI Trading

**An open research workbench that watches crypto markets, explains what the data shows, and argues with your trade ideas, so you decide with evidence instead of hype.**

Your market data. Your strategy. Your decision.

> [!IMPORTANT]
> **Early development (v0.2), research only.** My AI Trading never places orders, never asks for exchange API keys, and is not financial advice. Nothing here promises profit.

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

See [docs/EXTENSION.md](docs/EXTENSION.md) for permissions and troubleshooting.

## Features

This is the product roadmap with an honest status for each item.

✅ available · 🧪 experimental (works, but limited or unvalidated) · 🗓️ planned

### Market analysis

| Feature | Status | What you get today |
|---|---|---|
| Auto market monitoring | ✅ | Watches your watchlist and runs once per newly closed candle on each timeframe. Duplicate-proof alerts, backoff on outages, stale-data detection. |
| Multi-timeframe analysis | ✅ | Any fixed interval from 1m to 1W (monthly is planned), with a summary of where timeframes agree or conflict. |
| Technical analysis engine | 🧪 | SMA, EMA, RSI, ATR (Wilder and simple), volume ratio, prior highs and lows. MACD and Bollinger Bands are planned. |
| Chart pattern recognition | 🧪 | Darvas boxes and volatility contraction. VCP, cup and handle, head and shoulders, flags, triangles, and wedges are planned. |
| Market structure | 🧪 | Trend state, support and resistance, breakouts, breakdowns, and failed breakouts. Supply and demand zones are planned. |
| Smart alerts | ✅ | Breakouts, failed breakouts, moving-average reclaims and losses, unusual volume, your strategy matches, and method state changes. Quiet hours and pause. |

### Copilot and Strategy Studio

| Feature | Status | What you get today |
|---|---|---|
| Interactive copilot | 🧪 | Ask about the current symbol. Facts always come from the engine. An optional AI model adds clearly labeled interpretation when you supply your own API key. |
| Chart screenshots and deep reasoning | 🧪 | Local screenshot analysis with OpenCV and OCR: candle colors, labels, and price-axis text, all checked against exchange data. Optional AI vision and in-depth analysis. |
| 20 trader-inspired profiles | 🧪 | All 20 are listed with their documented timeframes and data needs. Four have experimental rule checks: Minervini (price criteria), Weinstein, the Turtles, and Darvas. The rest are reference cards for now. |
| Custom Strategy Studio | ✅ | Describe a rule in plain English. Preview it, dry-run it on past candles, and turn it on only after approving that exact version. You can switch off every preset. |
| Challenge My Thesis | ✅ | Lays out the evidence for and against your idea, your hidden assumptions, the unknowns, and the price levels that would prove you wrong. |
| Method and timeframe fidelity | ✅ | Each method runs as Native, Hybrid, Adapted, or Custom, and is labeled that way. A 30-week moving average is never quietly swapped for a 30-hour one. |

### Research and machine learning

| Feature | Status | What you get today |
|---|---|---|
| ML signal research | 🧪 | Leak-free datasets, purged train/test splits, and baseline comparisons. Logistic regression runs if scikit-learn is installed. LightGBM is planned. |
| Backtesting lab | ✅ | Next-bar fills, fees, slippage, perpetual funding, ATR stops, and walk-forward folds. Results are always labeled hypothetical. |
| AI cost control | 🧪 | Engine first, AI only when useful. Monthly token and dollar budgets, a usage dashboard, and consent before any data leaves your machine. Self-hosted or fine-tuned models are planned. |

### Platform

| Feature | Status | What you get today |
|---|---|---|
| Chrome extension | ✅ | Side panel with analysis, copilot, studio, alerts, usage, and settings. Recognizes Binance USD-M futures and spot pages. |
| Desktop app | 🗓️ | Planned. The engine is already separate from the UI. |
| Multi-market workspace | 🧪 | Watchlists and alert history for Binance crypto. Stocks and other markets are planned. There is no portfolio or position tracking, because the app is read-only by design. |

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

- **Read-only.** No order placement, no exchange credentials, no leverage, no auto-trading.
- **Local-first.** The engine listens only on `127.0.0.1` and requires a private token. Screenshots are processed in memory and never stored.
- **Your data stays yours.** Nothing is sent to an AI provider unless you turn on consent and configure a key.
- **No impersonation.** Trader profiles are research summaries of publicly described methods. They aren't endorsed by those traders and don't claim what anyone would personally do.

## Development

```bash
.venv/bin/pip install -e '.[api,test,vision]'
.venv/bin/python -m pytest              # Python tests (offline)
cd extension && npm test                 # extension tests
```

Contributors should read [AGENTS.md](AGENTS.md) and [docs/SECURITY.md](docs/SECURITY.md) first. The secret guard (`python3 tools/secret_guard.py --staged`) must pass before every commit.

## Learn more

- [Product requirements](docs/PRODUCT_REQUIREMENTS.md): the full vision and acceptance scenarios
- [Roadmap](docs/ROADMAP.md) and [current progress](docs/PROGRESS.md)
- [Chrome extension guide](docs/EXTENSION.md)
- [Security policy](docs/SECURITY.md)

## License and disclaimer

No license has been chosen yet, so all rights are reserved for now. This software is provided for research and education. Markets are risky, past results don't predict future returns, and every decision you make is your own.
