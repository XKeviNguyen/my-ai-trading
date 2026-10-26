# My AI Trading — Product Vision, Requirements & Technical Blueprint

**Document type:** Product Requirements Document (PRD) and technical direction  
**Working product name:** My AI Trading  
**Repository slug:** `my-ai-trading`  
**Version:** 1.1 — 26 October 2026 (critical thinking and timeframe integrity)  
**Status:** Proposed product requirements, **not** a claim of implemented functionality  
**Development workflow:** Local-first on Ubuntu + VS Code, using Codex and Claude Code; owner-controlled review, push, merge, and deployment

## 1. Executive Summary

**My AI Trading is a Python-first, AI-assisted financial market intelligence platform that continuously monitors charts and market data, explains trading opportunities and risks, and provides a conversational Trading Copilot.** It combines deterministic technical analysis, user-selectable methodologies inspired by renowned traders, custom natural-language strategy rules, machine learning, multimodal vision, and selectively invoked deep reasoning.

The platform must help users answer five questions with evidence: **What is happening? Why does it matter? Which trading frameworks agree or disagree? What would confirm or invalidate the setup? What is the risk?** It must not make unsupported claims of predictive accuracy, guaranteed profit, or endorsement by named traders.

The differentiator is not an AI chatbot that merely describes screenshots. It is a **reproducible, multi-timeframe, event-driven research system** that uses structured market data as the source of truth, calculates objective signals in Python, and uses AI only when its interpretation or interaction adds measurable value.

**Product promise:** *Your market data. Your strategy. Your decision.*

## 2. Product Goals and Success Criteria

1. **Continuous analysis:** Monitor selected markets, instruments, and watchlists automatically, including while the user is studying a chart, with relevant and non-duplicated alerts.
2. **Interactive copilot:** Support natural-language questions, follow-ups, scenario comparisons, and explanations grounded in the same market snapshot used by automated analysis.
3. **Strategy freedom:** Allow users to select zero, one, or multiple trader-inspired profiles; combine or disable them; or define one or several entirely original strategies through prompts.
4. **Timeframe-aware research:** Support configurable 1H/4H/1D/1W (and permitted additional intervals) as market context, but evaluate each selected methodology only on timeframes and asset classes justified by its evidence and declared capabilities. Do not impose hourly decisions on long-horizon strategies.
5. **Evidence and reproducibility:** Attach observable data, timestamps, timeframe, price levels, pattern criteria, missing data, uncertainty, and invalidation conditions to each insight.
6. **Cost discipline:** Make most frequent computations zero-LLM-token operations; measure actual per-user/per-feature/provider inference spend; enforce budgets, fallbacks, and deduplication.
7. **Security first:** Read-only market access in initial releases; no exchange trade permissions, credential leaks, arbitrary prompt-to-code execution, unauthorized deployment, or automated order execution.
8. **Measured quality:** Evaluate rules and models through reproducible fixtures, historical replay, and out-of-sample analysis; quantify false alarms, latency, and cost rather than promising wins.

**North-star quality indicators:** useful validated insights per active user; false-alert rate; analysis correctness against labeled cases; median alert delay after a qualified event; copilot response usefulness; cost per valid insight; operational uptime and data freshness. These require measured baselines before setting production SLOs.

## 3. Intended Users and Core User Journeys

- **Discretionary trader:** Opens Binance SANDUSDT perpetual; receives automatic 1H/4H/1D/1W analysis, alerts, chart levels, and a concise bullish/bearish/neutral explanation.
- **Methodology learner:** Selects Mark Minervini and Peter Brandt and asks why a large-volume breakout is not necessarily a confirmed VCP or major trend reversal.
- **Timeframe-specific trader:** Requests a 1H execution chart while selecting a weekly-based methodology; receives an honest compatibility warning and a choice of native, hybrid, or explicitly adapted analysis, not fake attribution.
- **Skeptical trader:** Asks the AI to challenge a bullish thesis, find contradicting evidence, identify invalidation, and say what cannot be determined.
- **Independent strategy designer:** Disables every preset and writes a prompt defining a rule; previews the generated structured rule, runs tests, approves it, and monitors its triggers.
- **Quantitative researcher:** Compares deterministic rules and scikit-learn/LightGBM models using walk-forward validation, net-of-cost backtests, and recorded market snapshots.
- **Cost-conscious advanced user:** Chooses inexpensive default analysis, optional pay-per-use deep vision/reasoning, and explicit monthly AI budgets.

## 4. Required Product Capabilities

### F01 — Market Data, Instrument Identity, and Chart Context (MUST)

- Begin with public **Binance USD-M Futures** OHLCV, then support additional market providers through independent adapters.
- Resolve venue, `symbol`, instrument type (spot vs perpetual/futures), quote currency, contract metadata, requested timeframe, and data-source timestamps. Never assume that identical ticker text means identical instruments on different venues.
- Retrieve bounded historical candles and stream new updates through REST/WebSocket, where provider permissions allow.
- Represent **closed versus still-forming candles** explicitly; use only information available at the relevant decision timestamp; avoid look-ahead leakage.
- Detect gaps, duplicates, missing fields, invalid OHLC prices, out-of-order events, stale feeds, timezone differences, API rate limits, and reconnect/retry issues. Fail visibly and safely.
- Share normalized public market-data calculations among users without mixing private strategies, user data, or tenant caches.
- Detect the chart currently viewed in supported browser integrations using narrowly scoped, permission-respecting methods. **Do not depend on scraping proprietary chart data or reading private exchange account state.**

### F02 — Deterministic Python Fast Engine (MUST)

- Calculate SMA/EMA and configurable periods including 7, 25, 50, 100, 150, and 200; ATR, relative volume/volume averages, pivot levels, highs/lows, breakout/breakdown conditions, volatility changes, and trend alignment.
- Add RSI, momentum and other indicators only when needed by implemented rules; every formula must have tests, known warm-up requirements, and stated adjustment conventions.
- Evaluate structural patterns such as VCP candidates, bases, flags, ranges, trendline events, and false breakouts with **explicit deterministic criteria where possible**, not unverified declarations of pattern certainty.
- Provide evidence fields such as `indicator_values`, `bar_close_time`, `pattern_conditions_met`, `rule_version`, `support/resistance`, `invalidation`, and `missing_inputs`.
- Ensure bounded memory, incremental updates where useful, deduplication of computations, and CPU-first operation. **No LLM call is required for routine numerical calculations.**

### F03 — Timeframe-Aware, Multi-Timeframe Analysis (MUST)

- Support user-requested 1H/4H/1D/1W and additional intervals when a provider supplies valid data. **These are available data views, not universal strategy timeframes.** Do not force every preset to evaluate every interval.
- Distinguish **analysis timeframe** (what the user is viewing), **methodology/native timeframe** (where the published framework is normally evaluated), **context timeframe** (broader market regime), and **execution timeframe** (optional entry/exit timing). One trade thesis may legitimately use all four roles.
- Each strategy version must declare a `timeframe_policy`: supported/natively documented intervals or horizons, session/calendar semantics, higher-timeframe prerequisites, required data, optional execution intervals, permissible adaptation methods and evidence quality. When the exact native interval is unknown, mark it `UNVERIFIED`, rather than inventing one.
- Correlate relevant timeframes for context only when meaningful. A 1H scalp can be reported as countertrend to 1D/1W without falsely converting a weekly investing philosophy into an hourly signal.
- Never silently transform `SMA(30, 1W)` into `SMA(30, 1H)`: 30 weekly observations and 30 hourly observations are fundamentally different. A 50-day MA is not a 50-hour MA; nor can trading sessions be equated to 24/7 crypto hours without an explicit calendar model.
- Align bars by instrument, venue, timestamp, close finality, session calendar, exchange timezone and data availability; avoid mixed-session, daylight-saving and partial-bar leakage.
- Expose explicit statuses: `SUPPORTED_NATIVE`, `SUPPORTED_HYBRID`, `ADAPTED_UNVALIDATED`, `UNSUPPORTED_TIMEFRAME`, `UNSUPPORTED_ASSET`, `INSUFFICIENT_DATA`, `UNVERIFIED_METHOD` and `STALE_DATA` where appropriate. These statuses describe *applicability*, not a buy/sell signal.
- If a user requests an unsupported or non-native timeframe, explain the mismatch concisely and offer three meaningful routes: native-methodology analysis on its relevant timeframe; a **hybrid** using native higher-timeframe context and requested intraday execution; or a user-approved **adaptation** with separate labels, versions and backtests.

### F04 — Trader Methodology Library (MUST AS CATALOG; STRATEGIES IMPLEMENTED INCREMENTALLY)

Provide **20 selectable methodology profiles**, initially with accurate descriptions, applicability metadata, implementation status, and references to public concepts. Do **not** present a metadata-only profile as an executable, validated strategy.

| # | Methodology / Trader | Primary Focus |
|---|---|---|
| 1 | Mark Minervini | SEPA, VCP, trend templates, breakout and volume |
| 2 | Peter Brandt | Classical price patterns, market structure, invalidation |
| 3 | Jesse Livermore | Pivotal points, trend behavior, price leadership |
| 4 | William J. O'Neil | CAN SLIM, growth and institutional accumulation |
| 5 | Stan Weinstein | Stage analysis, weekly trends and moving averages |
| 6 | Nicolas Darvas | Darvas Box breakout frameworks |
| 7 | Kristjan Kullamägi | Momentum bursts, episodic pivots, setups |
| 8 | Richard Dennis | Turtle trend-following principles |
| 9 | Ed Seykota | Systematic trends, risk and position sizing |
| 10 | Linda Bradford Raschke | Short-term/swing setups and market behavior |
| 11 | Takashi Kotegawa (B.N.F.) | Short-term reactions, momentum and mean reversion |
| 12 | cis (Japan) | Tactical trend/momentum and position management |
| 13 | Larry Williams | Short-term price behavior and volatility |
| 14 | Lance Breitstein | Intraday catalysts, execution and risk |
| 15 | Tom Hougaard | Price action and trading psychology |
| 16 | Paul Tudor Jones | Global macro themes, asymmetric trading risk |
| 17 | George Soros | Macro analysis and reflexivity |
| 18 | Stanley Druckenmiller | Macro trends and concentrated asymmetry |
| 19 | Kelvin Chiu | Commodity and relative-value/asymmetric opportunities |
| 20 | Peter Lynch | Fundamental growth investing and valuation |

**Critical transparency rule:** These are *research profiles inspired by publicly described methodologies*, not reproductions of any person's private system, real-time opinions, or trades. Fundamental, macro, commodity-spread, and equity-specific methods require suitable licensed data; if unavailable, return `INSUFFICIENT_DATA` rather than pretending an OHLCV crypto chart can satisfy every methodology.

- Users can turn **all** profiles off, toggle any subset, reorder perspectives, or save combinations as reusable templates.
- Do not create 20 permanent LLM agents. Share indicators and selectively invoke relevant interpretations.
- Each profile declares eligible assets, **native and optional execution timeframes**, market/session calendars, typical decision/holding horizon when supported by sources, required inputs, evaluation method, implementation maturity (`catalog_only`, `experimental`, `validated`), **documentation evidence level**, and limitations.
- Profiles that require company fundamentals, macro data, market spreads, or weekly structures must not be represented as native 1H candle-only strategies merely because the user requests 1H.
- Preserve source fidelity: distinguish published criteria from informed interpretation, common community adaptations, and newly designed user rules. Mark disputed or unknown trader preferences `UNVERIFIED`; never assert what a living or historical trader personally would do with a specific chart.
- Define an explicit **Native / Hybrid / Adapted** evaluation mode per selected profile (see F13). A profile whose required evidence is unavailable can still provide educational context but must not emit a fabricated executable signal.

### F05 — Custom Strategy Studio and Natural-Language Rules (MUST)

- Accept a single prompt or a sequence of prompts defining a user-owned trading strategy, entry filters, exit/invalidation rules, timeframes, risk limits, alert conditions, and exclusions.
- Compile unambiguous criteria into a **versioned, typed, allowlisted rule schema/AST or JSON DSL**, with explicit units and bar-finality semantics.
- Example input: *“Alert me only if a 4H candle closes above SMA25, current volume is at least 2x its prior 20-bar average, and the daily trend is bullish; otherwise show WAIT.”*
- Show a human-readable preview of compiled conditions, unresolved ambiguity, unsupported inputs, and a dry-run on recorded data; **require user confirmation before activating a new or materially changed rule**.
- Support composition (`AND`, `OR`, groups), multiple named strategies, versions, priority/override policies, alert thresholds, and conflicts between rules.
- Users may choose any supported timeframe for an original custom strategy. Preserve user intent as an **independent** strategy rather than incorrectly attributing it to a named trader.
- If a user asks to modify a trader-inspired profile for 1H/4H, create a separately named/versioned `ADAPTED_UNVALIDATED` rule set after explicit informed consent; never mutate the original profile silently.
- Never `eval`/`exec` generated Python, arbitrary SQL or shell commands from prompts; never grant tool permissions based on untrusted LLM output.
- Default to a safe `UNRESOLVED` or `INSUFFICIENT_DATA` result when a rule cannot be compiled or evaluated faithfully.

### F06 — Always-On Auto Analyst and Alerts (MUST)

- Observe selected symbols/watchlists and evaluate strategies on qualified closed-bar events or other explicitly configured events.
- Detect breakout, failed breakout, trend changes, MA reclaim, contraction, unusual volume, support/resistance tests, and strategy-specific triggers.
- Maintain per-symbol/timeframe/strategy state; use idempotent event IDs, deduplication, cooldowns, alert priorities, and a transparent alert history.
- Reconnect with bounded backoff and jitter; handle partial API failure and market-data outages without inventing live signals.
- Provide concise alerts with rationale, relevant prices, missing information, and an option to open a deeper copilot analysis.
- Support pause/resume, quiet hours, watchlist management, and budget-aware alert policies. Notifications should not execute trades.

### F07 — Interactive Trading Copilot (MUST)

- Answer questions about the active instrument, multiple timeframes, selected methodologies, user-defined rules, alternative scenarios, and risk.
- Reuse trusted Fast Engine outputs and time-stamped market snapshots; clearly separate **observed facts, deterministic signals, model interpretation, and unknowns**.
- Explain why traders' approaches disagree **without assuming each framework applies to the user's chosen timeframe or asset**; offer scenario conditions (e.g., *bullish only if price reclaims a pivot with volume*) instead of unsupported BUY/SELL certainty.
- Act as a **critical-thinking partner**, not a compliant bullish signal generator: test the user's thesis, identify evidence against it, distinguish assumptions from facts, challenge misapplied indicators/timeframes, and propose specific observations that would falsify the conclusion.
- If a user insists on 1H analysis for a primarily weekly/fundamental methodology, provide a suitable hourly research path while clearly labeling what is historical methodology, what is a hybrid overlay, and what is an unvalidated adaptation. Never quietly call it the named trader's authentic 1H strategy.
- Do not use majority voting among famous traders as evidence: methods may be correlated, incompatible, or lack valid inputs. Surface `NOT_APPLICABLE`/unknown cases and reason from applicability and data quality first.
- Support follow-up context, user-controlled depth, and optional creation/editing of strategy rules through a validated approval step.
- Display model/provider usage and timestamp; abstain if inputs are missing, stale, invalid or not licensed for a claimed conclusion.
- Do not imply actual endorsement or current personal opinions of named traders.

### F08 — Multimodal Vision and Deep Reasoning (MUST AS CAPABILITY, OPTIONAL PER REQUEST)

- Accept screenshots or permitted screen captures of chart views, with explicit user permissions and no hidden collection of unrelated windows.
- Use **OpenCV** for optional image preprocessing/cropping/geometry; **OCR** (e.g. Tesseract/pytesseract or an equivalent evaluated provider) only when chart text cannot be obtained from a reliable structured source.
- Use multimodal vision models to hypothesize chart annotations, trendlines, candlestick structure, and visually represented patterns; verify quantitative claims against exchange data whenever available.
- Route difficult, conflicting, or user-requested deep analysis to reasoning-capable LLMs; use smaller models for ordinary explanation and deterministic code for facts.
- Record the source of visual evidence, known ambiguity, confidence calibration, and the cost; never treat image recognition as proof of exact OHLCV values.

### F09 — AI Routing, Budgets, and Cost Observability (MUST)

- Route in order of least justified cost: **Python rules → event filter → optional decision/classification API → optional small LLM → optional multimodal/deep reasoning**.
- Evaluate providers such as **Jev and OpenAI Decisions** for constrained-choice or classification tasks, subject to current API availability, licensing, documented behavior, empirical quality, and pricing. Never use them when deterministic logic already provides the answer.
- Maintain provider adapters with typed contracts, timeouts, retries, circuit breakers, model versioning, explicit consent for data transmission, and fallbacks.
- Track **input/output tokens, reasoning/image charges where reported, cache hit rate, cost per user, cost per feature, cost per model/provider, and cost per validated insight**.
- Enforce per-user and platform-wide hard spend ceilings, rate limits, bounded concurrency, and explicit deep-analysis opt-in for expensive paths.
- Avoid unnecessary repeated images and redundant analysis; share only non-sensitive cached computations. Isolate user prompts, positions and account-related data.
- Consider batch processing for offline evaluations when it is cheaper and compatible with task latency.

### F10 — Research, Backtesting, and Performance Lab (MUST BEFORE PERFORMANCE CLAIMS)

- Record immutable or versioned rule definitions, market snapshots, timestamps of data availability, generated signals, and subsequent outcomes.
- Support deterministic replay, labeled setup examples, backtesting, walk-forward / chronological out-of-sample evaluation, and baselines.
- Model trading fees, spread, slippage, liquidity/market impact assumptions, funding costs for perpetual futures, leverage/liquidation implications when relevant, and survivorship bias for equities.
- Prevent look-ahead, target leakage, train/test contamination, and repeated tuning on held-out data; use purging/embargo where overlapping labels warrant it.
- Report precision/recall or suitable setup-classification metrics, false positives, drawdown, expectancy, calibration, stability by regime, and uncertainty. Do **not** present classification confidence as trade win probability.
- **Backtest adaptations separately** from original/native strategies. Do not transfer known research results, win-rate claims, thresholds, or parameter interpretations from a weekly/daily system to 1H/4H without new evidence; avoid parameter-mining on a tiny intraday sample.
- Distinguish hypothetical research, paper signal tracking, and audited live results. Initial releases do not place real orders.

### F11 — User Experience and Client Integration (MUST FOR USER-FACING RELEASE)

- Provide a clear instrument header (`SANDUSDT`, exchange, perpetual/spot, timeframe, freshness), multi-timeframe summary, selected strategies, evidence-backed scenarios, and alert feed.
- Display **methodology compatibility and provenance** next to each perspective: `Original methodology`, `Hybrid with native context`, `User-adapted — unvalidated`, or `Not applicable / insufficient evidence`. Distinguish an analysis timeframe from a methodology timeframe.
- When a user requests an unsupported hourly interpretation, show a concise compatibility explanation with options to continue via native, hybrid or adapted mode; avoid repeatedly prompting after the user saves an informed mode choice. The user can always create a fully independent custom strategy.
- Offer a chat panel, Strategy Studio, watchlist, signal history, model usage/cost dashboard, settings and exportable analysis reports.
- **Preferred first client:** Chrome/Chromium extension using TypeScript and Manifest V3 Side Panel, backed by the Python engine through an explicitly authorized local or authenticated API. The extension must not rely on DOM hacks to extract protected chart data.
- **Later native client:** Evaluate **PySide6/Qt** (Python) versus **Tauri** (TypeScript/Rust shell) using usability, memory, maintenance, and OS permissions. Ubuntu Wayland screen-capture limitations must be handled through supported OS permission flows.
- Keep business logic and strategy evaluation in reusable services rather than embedding it inside browser or native UI code.
- Support accessible navigation, readable states, small windows, 200% zoom and meaningful error/reconnect feedback.

### F12 — Critical Thinking, Adversarial Review, and Evidence-Based Disagreement (MUST)

**Core principle: The assistant must serve the user's decision quality, not flatter the user or simulate certainty.** User preferences determine analysis goals and acceptable custom rules; they do not alter historical facts, market data, mathematical definitions or the scope of a published methodology.

- For each meaningful thesis, distinguish **observations**, **rule-based findings**, **interpretations**, **assumptions**, **unknowns**, and **proposed tests**. For high-impact claims, give both supporting and contradicting evidence.
- Ask: **What evidence would prove this interpretation wrong?** Provide invalidation levels or conditions where justified, along with failure scenarios, regime changes, market liquidity and conflicting higher-timeframe data.
- Explicitly challenge unsupported premises: for example, a weekly-based system projected onto 1H, a fundamental investor applied to a futures token with no corporate financial statements, unclosed candles treated as confirmed, or a non-native indicator period silently changed.
- Respect user agency: when a requested analysis is unconventional but safe, explain tradeoffs, **offer an adapted or custom alternative**, and proceed under clearly labeled assumptions where the user has opted in. Do not block solely because it differs from a famous person's style.
- Do not invent evidence, historical trades, performance, exact private trading rules, or confidence values. Give `INCONCLUSIVE`, `INSUFFICIENT_DATA` or `NOT_APPLICABLE` when appropriate; not every perspective must output `BUY`, `SELL` or `WAIT`.
- Use a cheap deterministic **premise/applicability gate first**; reserve optional decision models or reasoning LLMs for genuinely ambiguous conflicts or user-selected deep debate. A costly mandatory multi-agent debate for every candle is prohibited without measured value.
- Permit a user-invoked **Challenge My Thesis** feature with independent bull/bear hypotheses, risk-first counterarguments, and observations that would change the conclusion. Summarize disagreement faithfully, not through majority vote or made-up accuracy scores.
- Prevent blind model deference: LLM judgments must not override failed market-data validity gates or invented strategy eligibility; show disagreement between deterministic and model conclusions rather than silently replacing verified results.

### F13 — Methodology Fidelity and User-Requested Timeframe Adaptation (MUST)

**Every profile must support an explicit analysis mode:**

| Mode | Meaning | Permitted outcome | Mandatory user-visible labeling |
|---|---|---|---|
| `NATIVE` | Evaluate documented rules on documented/supported horizons with required evidence | Eligible for rule evaluation when implemented and validated | `Methodology-inspired / native scope` |
| `HYBRID` | Keep trader-style higher-timeframe/fundamental/macro context; add user-requested lower-timeframe execution rules as **separately sourced/custom** analysis | Context verdict + separate tactical signal | `Hybrid: native context + user timing` |
| `ADAPTED` | Modify thresholds, bar resolution, rules or horizon for a different timeframe/asset | Experimental research only until independently validated | `User-adapted — unvalidated; not the trader's original system` |
| `CUSTOM` | Original user-defined strategy with no claimed trader authorship | Rule evaluation on a supported timeframe after rule validation | `Custom strategy` |

**Compatibility decision sequence:**

1. Resolve the instrument, asset class, venue, trading sessions, user-selected interval and purpose: **screening, structural context, execution timing or backtesting**.
2. Load the selected methodology's source-backed eligibility metadata, required inputs, native timeframe/horizon, and implementation maturity. Mark unknown fields `UNVERIFIED` instead of using fabricated historical rules.
3. When native and eligible, evaluate the documented strategy at its intended timeframe; optionally analyze other intervals as non-authoritative context.
4. When not native, first offer **native analysis**, **hybrid context + requested interval**, or **user-adapted experimental rules**. An existing saved preference may resolve this without interrupting monitoring; a *new or materially changed adapted executable rule* still requires user preview and approval.
5. If a valid interpretation cannot be made or required inputs are missing, explain precisely why, and offer a relevant independent custom strategy without assuming the trader would have endorsed it.
6. When adapting, preserve the original unchanged, fork a versioned strategy ID, document time units, source differences and validation limits, and require a fresh out-of-sample evaluation before showing performance claims. No automatic mapping `30-week MA → 30-hour MA` or `50-day MA → 50-hour MA`.
7. On active monitoring, evaluate on **each strategy's own qualifying close/event cadence**: a weekly strategy should not trigger fresh weekly conclusions every 1H candle. Hourly alerts may report separately defined tactical conditions without resetting weekly context.

**Illustrative acceptance cases:**

- **Stan Weinstein + SANDUSDT 1H:** Preserve weekly Stage Analysis using weekly candles and its relevant moving average; optionally analyze 1H tactical timing under `HYBRID`. A bespoke `30-period SMA on 1H` is a separately adapted rule, **not** Weinstein's 30-week indicator.
- **Mark Minervini + 1H:** Keep daily/weekly trend-template context; optionally test intraday price/volume timing. A pure hourly VCP-like detector is explicitly a **Minervini-inspired adaptation**, not a verified SEPA setup by the trader.
- **Peter Lynch + crypto perpetual 1H:** Explain that hourly token candles are insufficient for Lynch-style equity fundamental analysis; do not score a Lynch fundamental thesis from OHLCV alone. Offer a custom hourly technical strategy or, for a suitable stock with licensed fundamentals, Lynch-inspired long-horizon context plus separate 1H tactical timing.
- **User says “I know the difference—still analyze 1H”:** Continue with `HYBRID`, `ADAPTED`, or `CUSTOM` as appropriate, clearly label it, and preserve consent/version information; avoid a repetitive refusal or misleading trader attribution.

**Machine-readable minimum fields:** `methodology_id`, `strategy_version`, `mode`, `asset_scope`, `documented_timeframes`, `context_timeframes`, `execution_timeframes`, `market_calendar`, `required_data`, `provenance`, `evidence_quality`, `implementation_maturity`, `user_approved_adaptation`, `unresolved_limitations`.

## 5. AI & Machine Learning Technology Stack

The following technologies are explicitly part of the research and implementation plan. **They are not all required dependencies for the first commit.** Choose them when measured use cases justify inclusion.

| Technology | Planned Role | Adoption Gate |
|---|---|---|
| **Python** | Core programming language; market data, rules, services, evaluation | Day-one foundation |
| **NumPy / pandas** | Numerical arrays, indicators, time-series analysis, research | Use when simpler implementations become costly or error-prone |
| **scikit-learn** | Preprocessing, supervised classification baselines, model selection, validation, calibration, metrics, pipelines | Introduce with labeled research tasks |
| **LightGBM** | Efficient gradient-boosted decision trees on OHLCV, volatility, volume, funding/OI and engineered features; ranking/classification | Benchmark against simple rules and scikit-learn baselines |
| **PyTorch** | Deep learning research and optional fine-tuning of local text/vision models | Only after sufficient data, evals, hardware and economics |
| **Hugging Face Transformers** | Load and evaluate commercially compatible open-weight text/multimodal models | License/security review; compare against hosted APIs |
| **PEFT / LoRA / QLoRA** | Parameter-efficient domain adaptation for setup interpretation and structured output | Only with high-quality licensed, leakage-free examples and measured gains |
| **OpenCV** | Optional screenshot crop, geometry/image processing and preprocessing | When a visual input is necessary; not for numeric OHLCV |
| **OCR** | Extract visible chart text (symbols, MA labels, timeframe) with confidence and validation | Fallback only when structured data is unavailable |
| **Multimodal Vision LLMs** | Semantic understanding of chart images and annotations | User permission, relevance and cost budget |
| **Reasoning-capable LLMs** | Hard multi-timeframe synthesis, disagreement analysis, scenario reasoning | Event-triggered or explicit user request |
| **Small LLMs / Decision APIs** | Cheap classification, structured prompt-to-rule proposals, ordinary summaries | Win on cost/accuracy/latency benchmark over deterministic baselines |
| **TensorFlow** | **Not selected by default**; potential future experiment | Only if a specific model/tool requires it |

### ML Research Principles

- Treat ML as an **experimental signal-ranking/research component**, not as an oracle. A strategy does not become profitable because it uses LightGBM or a neural network.
- Start with robust feature definitions and simple baselines. Compare `rule_only`, scikit-learn, LightGBM, and LLM-assisted systems against the same held-out dataset and assumptions.
- Define labels carefully (*breakout held over N bars*, *invalidated within M bars*, etc.) while accounting for fees and overlapping windows; record label-generation versions.
- Optimize for **out-of-sample usefulness**, false-alarm reduction, latency, inference cost, model size, interpretability and regime drift.
- If self-hosting small models, measure actual CPU/GPU utilization, memory, throughput and cost per correct evaluation. Open-weight licenses, data licenses and redistribution rights must be checked before commercial use.

## 6. Proposed Technical Architecture

```text
Browser Side Panel / Native Client / CLI
              |
              v
    Authenticated or Local API (FastAPI + Pydantic)
              |
  +-----------+-----------+
  |                       |
Market/Chart Context   Trading Copilot / Strategy Studio
  |                       |
  v                       v
Provider Adapters      Typed Rule Compiler + Approval
(REST, WebSocket)             |
  |                           |
  +----------+----------------+
             v
  Validated Instrument + Timestamped Closed-Bar OHLCV
             |
             v
      Python Fast Engine  ---> Shared indicator cache
             |
             v
   Strategy Registry + Custom Rule Evaluator
             |
             v
  Event Detection / Dedup / Alert State
             |
             v
      Cost-Aware AI Router
   /         |          |            \
 Rules   Decisions   Small LLM   Vision/Deep LLM
   \         |          |            /
             v
     Evidence / Risk / Uncertainty Layer
             |
             v
 Notifications / Copilot / Audit & Research History
```

**Initial technologies:** Python 3.11+ (or newer supported runtime), `asyncio`, FastAPI, Pydantic, pytest, typed models, simple local persistence such as SQLite if required, Git/GitHub CI, and an independently testable CLI. Upgrade to PostgreSQL, Redis, distributed queues or other infrastructure **only after measured needs** (multi-user tenancy, durability, scale, throughput) justify their complexity.

**Provider-neutral interfaces:** MarketDataProvider, InstrumentResolver, IndicatorService, StrategyEvaluator, RuleCompiler, VisionProvider, DecisionProvider, ReasoningProvider, NotificationService, CostLedger. Implement only interfaces actually needed; do not build a large framework in anticipation of imagined providers.

**Suggested core records:** InstrumentIdentity; Candle (open/close timestamps + `is_closed`); MarketSnapshot; IndicatorResult; StrategyProfile; StrategyVersion; RuleEvaluation; TradingInsight; AlertEvent; AIInvocationCost; ModelEvaluation. All cross-boundary inputs need parsing/validation and schema versions where persistence or provider evolution requires it.

## 7. Security, Privacy, and Engineering Doctrine (NON-NEGOTIABLE)

- **Read-only first:** Public market data only. Do not request trading API keys, private account endpoints, order permissions or automated execution features in the initial releases.
- **No secrets in Git:** `.env`, `.env.*`, private keys, access tokens, provider keys, production logs or user positions never belong in commits, prompts, screenshots or CI output. Commit sanitized `.env.example` only. Use `.gitignore` **and** reviewed Git hooks, staging/history secret checks, independent secret scanning and available GitHub push protection. These controls complement rather than replace each other.
- **No arbitrary code execution:** Prompts and LLM outputs are untrusted data; the custom-rule compiler outputs validated typed AST/DSL only. No `eval`, unrestricted Python, shell, SQL, or model-issued execution permissions.
- **Least privilege:** Restrict extension permissions, local API binding and cross-origin access; verify authentication/authorization and strict tenant isolation when accounts are introduced.
- **Data privacy:** Encrypt credentials at rest where supported; never expose secrets or private strategy content across tenants; do not send private chart/account context to external LLM providers without informed user consent.
- **Resilience and correctness:** Validate data source/closure, rate limits, stale states, reconnect/replay, alert idempotency, bounded queues and backpressure. Clearly represent `INSUFFICIENT_DATA`, `STALE_DATA` and provider errors.
- **Human control:** Coding agents may inspect/test and perform bounded local reversible edits in approved branches; repository publishing, pushes, merges, releases, deployments, billing changes, production-data operations and external high-risk actions require explicit owner authorization.
- **Multi-agent safety:** Separate concurrent code-writing agents into non-overlapping Git worktrees/branches with explicit file ownership, contract dependencies, test gates and budgets. Independent reviewers and six Breakers are read-only.
- **Six engineering Breakers:** **Security; Data; Flow; Environment; Performance; Efficiency & Simplicity.** Prioritize security/correctness before speed and cost. Use evidence-based pass/fail reports; never claim an unrun test passed.
- **Trading disclaimers:** No guaranteed profit, no financial advice impersonation, no hidden automatic leverage, and no claim of an endorsed or exact trader-specific proprietary system.

## 8. Release Phases and Boundaries

| Milestone | Deliverable | Exit Condition |
|---|---|---|
| **R0 — Documentation-first** | Product vision, English PRD, README, `AGENTS.md`, `CLAUDE.md`, security/orchestration policies | No secrets, understandable scope; **not a finished app** |
| **R1 — Audit and import V0.1** | Secure Python package + fixture tests, read-only Binance adapter, Fast Engine and validated custom JSON rules | Offline tests pass locally; known shortcomings documented; bounded public API smoke test only with approval |
| **R2 — Monitoring foundation** | Reliable 1H/4H/1D/1W synchronization and event-based watcher | Closure/reconnect/stale/gap/idempotency and low-resource tests |
| **R3 — Strategy Studio** | Accurate 20-profile catalog, **timeframe applicability + native/hybrid/adapted labeling**, gradually implemented rules, natural-language-to-DSL compilation | Profiles declare horizon/data provenance; user can disable all; invalid or falsely attributed rules fail closed |
| **R4 — Copilot + AI routing** | Grounded, skeptical chat, **Challenge My Thesis**, decisions/small LLM/reasoning/vision adapters, spend limits | Evals for misapplied timeframes, deference/sycophancy, hallucinations, prompt attacks, cost, privacy and missing data |
| **R5 — User-facing client** | Browser extension (or approved native alternative), watchlists, alerts and cost dashboard | Usability/accessibility, safe browser permissions, no unapproved trading operations |
| **R6 — ML & research lab** | scikit-learn/LightGBM research, reproducible backtesting, calibration and walk-forward metrics | Verified no-leakage tests, realistic cost assumptions, no unearned profitability claims |
| **R7 — Optional local/fine-tuned AI** | PyTorch + Transformers + PEFT/LoRA/QLoRA pilots; self-host vs API comparison | Better validated quality or unit economics and acceptable security/ops footprint |

**Out of scope for initial releases:** Automated live trading, copy-trading, portfolio management via private exchange credentials, guaranteed profit prediction, unlicensed market-data redistribution, proprietary-strategy replication, always-on GPU inference, 20 simultaneous reasoning agents, and premature cloud microservices.

## 9. Acceptance Scenarios (Agent-Readable)

**Scenario A — SANDUSDT, two timeframes.** Given valid 1D and 1W Binance USD-M candles, when a large daily breakout occurs but the weekly chart remains weak, the app shows both sets of evidence and explains disagreement. It must not fabricate a Minervini VCP, claim a confirmed long-term reversal without criteria, or treat a still-forming candle as closed.

**Scenario B — No trader presets.** Given that a user disables all 20 methodology profiles and adds a custom natural-language rule, the app compiles only supported criteria into a versioned rule with a readable preview. It evaluates only after approval. No preset is silently re-enabled.

**Scenario C — Malicious or ambiguous custom prompt.** Given an instruction to execute arbitrary Python, request trading keys, or override system controls, the compiler rejects it and reports a non-executable reason. Ambiguous rules remain inactive pending clarification.

**Scenario D — Network loss and duplicated events.** Given interrupted WebSocket connectivity, replays and out-of-order candles, the watcher reconnects with bounded retries, identifies stale data, and avoids duplicate alerts for the same strategy/instrument/bar event.

**Scenario E — Security and cost.** Given a forced staged `.env` or a hard-coded credential, local checks block the normal commit/push path and the workflow stops for human remediation; real leaked keys require rotation. Given an exhausted user AI budget, analysis falls back to deterministic results instead of making unauthorized paid API calls.

**Scenario F — ML claims.** Given an ML model reporting high historical accuracy, the app displays labeled data provenance, holdout/walk-forward results, leakage checks, fees/slippage assumptions and limitations. It never advertises a win rate without valid out-of-sample evidence.

**Scenario G — Native weekly system requested on 1H.** Given an active 1H chart and Stan Weinstein selected, the app evaluates weekly Stage Analysis on 1W (when valid), not SMA30 of hourly bars under a misleading native label. It offers a hybrid hourly timing view or a separate unvalidated adaptation with opt-in.

**Scenario H — Fundamental investor selected for hourly crypto.** Given Peter Lynch selected on SANDUSDT perpetual 1H without company financial statements, the app returns `UNSUPPORTED_ASSET` or `INSUFFICIENT_DATA` for Lynch-style fundamentals. It may separately analyze custom 1H technical criteria but never fabricates a Lynch hourly BUY.

**Scenario I — User knowingly insists on 1H.** Given an informed user's preference for 1H analysis, the app provides appropriately labeled `HYBRID`, `ADAPTED` or `CUSTOM` results rather than refusing the analysis. Original methodology versions remain unchanged; custom/adapted activation requires a preview and approval.

**Scenario J — Skeptical counter-analysis.** Given a bullish daily breakout during a bearish weekly regime, `Challenge My Thesis` reports bullish and bearish evidence, missing data, falsification conditions and uncertainty. It must not invent precise likelihoods or accept the user's optimistic premise by default.

**Scenario K — Methodologies disagree or do not apply.** Given three selected profiles with differing horizons and required data, the app does not vote `2 BUY vs 1 SELL`; it reports each framework's compatibility and evidence independently and abstains from unsupported consensus.

**Scenario L — Timeframe integrity.** Given session-based stocks, 24/7 crypto and a mix of open/closed bars, indicators use the appropriate source/calendar, clearly distinguish 30 weekly bars from 30 hourly bars and do not generate new weekly signals on every hourly event.

## 10. Current Implementation Status (As of 26 October 2026)

- A **separate local V0.1 scaffold ZIP** was generated previously in ChatGPT. It reportedly includes a read-only Binance USD-M public-candle adapter, closed-bar data validation, basic MA/ATR/volume/pivot/breakout signals, one partial Minervini-inspired heuristic, 20 **mostly catalog-only** trader profiles, a safe limited JSON rule evaluator, local CLI/API and 16 mocked/offline tests.
- That prototype is **not yet audited on the user's Ubuntu environment**, not verified against Binance live data, not installed into the intended `my-ai-trading` Git repository, not pushed or deployed, and not a complete implementation of this document.
- The repository setup kit V3 contains documentation, multi-agent/secret policies and guard scripts. Its presence in ChatGPT does **not** prove an actual GitHub repository was created or local Git hooks were enabled.
- The next concrete task is to inspect and selectively import V0.1 on the owner's Ubuntu machine, establish a passing tested baseline, and then implement R2 in focused increments.

## 11. Development Instructions for Codex / Claude Code

**Before writing code:** inspect actual repository state, `README.md`, `AGENTS.md`, `CLAUDE.md`, `docs/ROADMAP.md`, `docs/SECURITY.md` and `docs/AGENT_ORCHESTRATION.md`; establish what is implemented versus planned. Treat this document as product direction and user requirements, not permission to execute outside operations.

**For each task:** report the requirement, small design, exact files to change, dependency/secret implications, test plan, risk level, bounded token/API budget, test results and limitations. Prefer a single implementer with read-only reviewers at R1. Permit parallel implementers only when independent worktree/file ownership, stable interfaces and integration gates make it safe and beneficial. Apply the six Breakers selectively, with Security always rigorous, and keep the owner in control of pushes, merges and deployments.

**Special development gate:** All strategies must pass timeframe/asset applicability checks before evaluation. QA must test explicit Native/Hybrid/Adapted/Custom results and adversarial user requests to misuse a trader's methodology. Do not silently invent compatibility.

**Definition of success:** an explainable, affordable, secure AI trading research companion that helps humans make better-informed decisions, with traceable observations and independently verified engineering quality—not a black-box buy/sell oracle.
