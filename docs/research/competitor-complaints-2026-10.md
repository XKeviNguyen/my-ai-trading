# What users complain about in trading-analysis apps (October 2026)

- **Date:** 2026-10-31
- **Purpose:** decide which features help a local, read-only research tool avoid the problems people report with other apps.
- **Method:** about 20 web searches plus two fetched pages. Sources were Trustpilot, regulator and news coverage, review sites and Reddit mirrors. Most review sites are affiliates or competitors, so we gave more weight to Trustpilot text and the press.
- **Limits:**
  - Every point below paraphrases individual, unverified user reviews. None of them measure how often a problem happens.
  - Trustpilot itself flags fake reviews on some of these pages.
  - We found little material on Autochartist, TabTrader, Altrady and Pionex.

## Complaint themes

| # | Theme | Apps named | Example source |
|---|---|---|---|
| 1 | Auto-renewal, trial-to-paid charges, refused refunds | TradingView, Finviz Elite, Trade Ideas, TrendSpider | trustpilot.com/review/tradingview.com?page=9 |
| 2 | Support is a chatbot loop; tickets closed unresolved | TradingView, 3Commas, Coinigy | same page; thegodfunded.com/en/tools/3commas/ |
| 3 | Alerts that do not fire, fire wrongly, or expire | TradingView, Tickeron | trustpilot.com/review/tradingview.com |
| 4 | Repainting: signals and strategy results change afterwards | TradingView community scripts | blog.crosstrade.io/pine-script-repainting/ |
| 5 | Backtests that ignore costs or overfit | TradingView, 3Commas, bots in general | hackernoon.com/your-backtest-left-out-the-two-costs-that-kill-it |
| 6 | Opaque or overhyped AI signals | Tickeron, Trade Ideas (Holly) | daytradingtoolkit.com/reviews/tickeron-review |
| 7 | AI-signal scams | Telegram/WhatsApp groups | SEC case reported by investmentnews.com; CFTC warning via theblock.co |
| 8 | API-key leaks | 3Commas, Cryptohopper | cointelegraph.com (3Commas API key leak) |
| 9 | Bots that lose to simply holding | 3Commas, Pionex, Cryptohopper | uk.trustpilot.com/review/3commas.io?page=3 |
| 10 | Forced migrations that break workflows | 3Commas V2, TradingView paywall changes | thegodfunded.com/en/tools/3commas/ |
| 11 | Undisclosed data delay or wrong data | TradingView, Finviz | TradingView Trustpilot, 2026 |
| 12 | Lag, freezes, slow backtests | TradingView, Trade Ideas, Cryptohopper, Coinigy | various Trustpilot pages |
| 14 | Pattern detectors with false positives that change with settings | Autochartist, ZigZag-based scripts | tradingbrokers.com/autochartist-review/ |
| 15 | General chatbots that make up price levels | ChatGPT-style chart readers | vendor warnings only; we found no direct user complaints |

Theme 13 was about exchange account freezes. It doesn't apply to a tool that holds no money, so it's left out of the table.

## What we built or already had

| Theme | Our answer | Where |
|---|---|---|
| 4 Repainting | Signals only on closed bars. A playbook signal never comes before the bar that confirms the pattern's last swing. Prefix-invariance tests check this. | `playbook.pattern_signals`, `tests/test_playbook.py` |
| 5 Costs and overfitting | Every backtest is replayed with zero costs and with double costs, shown next to a hold-the-window baseline. It also gives a 95% interval on the win rate, a warning under 30 trades, and a warning when the edge is smaller than the costs. | `backtest.robustness` |
| 6 Opaque signals | Each pattern verdict lists the measured facts behind it and the rule that wasn't met. A track record counts how earlier signals did on the same chart, with sample size and a 95% interval. | `playbook.breakout_quality`, `playbook.track_record` |
| 9 Losing to holding | The hold baseline is in every backtest, and fixed-risk sizing caps each loss. | `backtest.robustness`, `BacktestConfig.sizing` |
| 14 False patterns | A pivot-sensitivity check shows whether a pattern survives 2-, 3- and 4-bar swing settings. | `playbook.pivot_sensitivity` |
| 7 and 8 Scams, key leaks | No signals for sale, no live orders. Read-only live keys stay in the OS vault. | existing R8 design |
| 11 Data delay | Freshness and staleness are shown on every timeframe. | existing engine |
| 15 Made-up numbers | The copilot answers from engine facts only. | existing copilot |
