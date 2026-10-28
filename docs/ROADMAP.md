# Roadmap — My AI Trading

**Effective:** 2026-10-26. **Status:** product plan, not a promise of completed functionality.
**Authoritative requirements:** [`PRODUCT_REQUIREMENTS.md`](PRODUCT_REQUIREMENTS.md) (PRD v1.1). Where this roadmap and the PRD disagree, the PRD wins; fix the roadmap.

## R0 — Repo preparation (documentation only)
- README/AGENTS/CLAUDE/docs written and reviewed. Owner creates repository privately and imports these docs locally.
- Choose explicit license later before public release; no production service.
- Exit gate: docs coherent; no private credentials; user understands that scaffold is not pushed.

## R1 — Audit and selectively import V0.1
- Inspect existing ZIP generated in a separate ChatGPT session. Make sure no generated caches, secrets or throwaway test artifacts enter Git.
- Run baseline tests and static checks on Ubuntu. Compare docs against actual source; secure dependencies and public Binance API adapter.
- Verify closed-candle semantics and no-lookahead invariants. Confirm CLI/API bindings localhost-only.
- Prove API behavior via mocks/fixtures first, then (with permission) bounded live public API smoke test.
- Python 3.14 is the local runtime (`requires-python >= 3.11`); CI must test 3.11 and 3.14 at minimum. Use a project-local `.venv` only — never modify system Python.
- Isolated audit of the V0.1 ZIP: [`audits/V0.1_AUDIT.md`](audits/V0.1_AUDIT.md) — findings and the staged integration plan.
- Exit gate: reproducible virtualenv setup, passing tests with evidence, no trading permissions, clear limitations.

## R2 — Robust data and real-time Auto Analyst
- Multi-timeframe 1H/4H/1D/1W: source metadata, UTC, incomplete/closed bars, dedup, sync; distinguish USD-M spot vs futures.
- Provider reconnect/backoff, stale data detection, bounded memory and queues; share work across watchlists.
- Test retries, offline/reconnect, out-of-order and duplicate data, DST/local vs UTC, provider throttling.
- Each strategy evaluates on **its own qualifying close cadence**: a 1W context must not be recomputed into a fresh weekly conclusion on every 1H close (PRD F13 step 7, Scenario L).
- Bar alignment by instrument, venue, close finality and calendar; 24/7 crypto vs session-based equities are distinct calendar models.
- Exit gate: stable hour-long mock stream tests plus measured live public endpoint tests (if permitted); no repeated alert for same event.

## R3 — Strategy Registry & Prompt-defined rules
- Implement certified deterministic rules gradually (first Minervini-like trend screen, pivots/volume, Brandt-like structures) with sourced definitions and test fixtures.
- All 20 profiles remain selectable but unimplemented profiles are labeled as such; no fake consensus or bogus "same as trader" claims.
- Typed schema for custom rules, user profiles, enable/disable all presets; prompts → proposal AST → validation → dry run → user approval to activate.
- **Methodology fidelity (PRD F13).** Every profile version carries a `timeframe_policy` and the machine-readable fields `methodology_id`, `strategy_version`, `mode`, `asset_scope`, `documented_timeframes`, `context_timeframes`, `execution_timeframes`, `market_calendar`, `required_data`, `provenance`, `evidence_quality`, `implementation_maturity` (`catalog_only` / `experimental` / `validated`), `user_approved_adaptation`, `unresolved_limitations`. Unknown native timeframes are `UNVERIFIED`, never invented.
- **Timeframe roles (PRD F03):** analysis (what the user views), methodology/native, context (regime) and execution (timing) are separate fields, not one `interval`.
- **Analysis modes:** `NATIVE`, `HYBRID` (native context + separately sourced user timing), `ADAPTED` (forked, versioned, `User-adapted — unvalidated`, opt-in) and `CUSTOM` (no trader attribution). No automatic mapping such as `SMA(30, 1W) → SMA(30, 1H)`; the original profile is never mutated.
- **Applicability gate before evaluation:** a cheap deterministic premise check returns `SUPPORTED_NATIVE`, `SUPPORTED_HYBRID`, `ADAPTED_UNVALIDATED`, `UNSUPPORTED_TIMEFRAME`, `UNSUPPORTED_ASSET`, `INSUFFICIENT_DATA`, `UNVERIFIED_METHOD` or `STALE_DATA`. These describe applicability, not buy/sell.
- Exit gate: negative tests for malicious prompts, unknown metrics and conflicting rules; reproducible rule results and per-rule rationale; acceptance Scenarios **B, C, G, H, I, K, L** pass as automated tests (see below); invalid or falsely attributed rules fail closed.

## R4 — Trading Copilot, decision routing, vision & deep reasoning
- Shared market snapshots; AI output includes evidence refs, timestamps, model info, alternatives, invalidation and uncertainty.
- Deterministic routing first; decision classifier only if benchmark wins; small LLM for ordinary explanations; reasoning/vision on demand/event.
- Versioned providers (no specific price hardcoding); cost logs + spend caps, per-user tenant isolation, cache boundaries.
- Offline/mock LLM evaluations on labeled examples, hallucination checks, cost/latency benchmark; provider license/privacy review.
- **Critical thinking (PRD F12):** every meaningful thesis separates observations, rule-based findings, interpretations, assumptions, unknowns and proposed tests, with falsification/invalidation conditions. User-invoked **Challenge My Thesis** produces independent bull/bear cases and risk-first counterarguments. No majority vote across traders; no invented likelihoods; LLM output never overrides a failed data-validity or applicability gate — disagreement is shown, not hidden.
- Exit gate: abstain on missing data, budget controls working, prompt injection and cross-tenant tests; evals for misapplied timeframes, sycophancy/deference and hallucinated trader rules; Scenarios **J** and **K** pass.

## R5 — Browser/desktop client and alerts
- Compare Chrome Side Panel and PySide6/Tauri: browser-awareness, Windows/Ubuntu/Wayland, maintenance/performance.
- Show chosen presets + custom rules, evidence and contradictions; clear indicator for stale or incomplete market data.
- Alerts only on meaningful state changes; accessible UI with small-screen and zoom testing.
- Exit gate: no unauthorized page scraping, no credential capture, no accidental order execution.

## R6 — Research and evaluation lab
- Backtest events and strategies with fees, slippage, liquidity, spreads; walk-forward/out-of-sample splits and timestamped data availability.
- Monitor false positives, drawdown, calibration and reproducibility; reject strategies overfit to sample history.
- Exit gate: independent review of evidence; do not present backtest results as guaranteed future returns.

## R7 — Optional local/fine-tuned AI
- PyTorch + Transformers + PEFT/LoRA/QLoRA pilots, and a self-host vs API comparison (PRD §8).
- Only with real datasets, evaluation goals, hardware, licenses and an approved budget (PRD §5).
- Exit gate: better validated quality or unit economics, with an acceptable security and operations footprint.
- **Status:** not started. None of these libraries is a project dependency.

## R8 — Exchange accounts + testnet trading
- Normalized adapters for Binance, Bybit and OKX, plus Interactive Brokers paper if feasible. Read-only account sync, portfolio and trade analytics, and the credential lifecycle in the OS vault with expiry.
- Testnet-only Trading Mode: preview → per-order confirmation → journal → send → reconcile. Boundary in `AGENTS.md` §2.
- Exit gate (PRD §8): live keys with trade or withdrawal rights are rejected; expired credentials are never used; no order without a per-order confirmation; unknown order states are reconciled, never blindly retried; OAuth flows stay marked BLOCKED BY PROVIDER APPROVAL until approved.
- **Status:** built and tested offline; not yet run against real exchange testnets. Details and limitations in [`ACCOUNTS_TRADING.md`](ACCOUNTS_TRADING.md); open findings in the latest audit under [`audits/`](audits/).

## Acceptance scenarios G–L → milestone gates

Full text in PRD §9. Each becomes an offline, fixture-based test before the milestone exits.

| Scenario | Requirement under test | First gate |
|---|---|---|
| **G** — Weinstein selected on a 1H chart | Stage Analysis evaluated on 1W; hourly SMA30 never labeled native; hybrid / opt-in adaptation offered | R3 |
| **H** — Lynch on SANDUSDT perpetual 1H | `UNSUPPORTED_ASSET` / `INSUFFICIENT_DATA` for fundamentals; never a Lynch hourly BUY | R3 |
| **I** — user knowingly insists on 1H | Labeled `HYBRID` / `ADAPTED` / `CUSTOM` result, not a refusal; original version unchanged; activation needs preview + approval | R3 |
| **J** — bullish daily breakout in bearish weekly regime | Challenge My Thesis returns both sides, missing data, falsification conditions; no invented probabilities | R4 |
| **K** — three profiles with different horizons/data | Per-framework applicability and evidence; no `2 BUY vs 1 SELL` vote or unsupported consensus | R3 (deterministic), R4 (copilot) |
| **L** — session stocks, 24/7 crypto, open/closed bars | Correct calendar per source; 30 weekly ≠ 30 hourly bars; no new weekly signal per hourly event | R2 (cadence), R3 (labels) |

## Open decisions
- Preferred client (Chrome extension vs native), exact market data rights, commercial licensing, user auth/billing, AI vendors.
- GPU self-host/LoRA only after economics and quality justify; initial MVP local-first with zero-AI-token fast engine.
- Later asset classes (US/JP equities, commodities), deeper macro/fundamental integrations, optional paper trading.
