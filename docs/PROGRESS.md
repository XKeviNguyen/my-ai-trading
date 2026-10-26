# Progress & Handoff — My AI Trading

**Last updated:** 2026-10-26 · **Branch:** `feature/fast-engine` (all work staged, **not committed** — the owner commits and pushes).
States are *implemented + offline-tested* unless marked otherwise. Nothing has been tested against live Binance or a live AI provider yet.

## Implemented (offline tests pass)

| Area | Module(s) | PRD | Evidence |
|---|---|---|---|
| Market data types, closed-bar semantics, gaps, interval identity | `domain.py` | F01, F03 | `tests/test_domain.py` |
| Indicators with stated conventions | `indicators.py` | F02 | `tests/test_indicators.py` (property tests) |
| Fast Engine events with invalidation, stale suppression | `engine.py` | F02, F06 | `tests/test_engine.py` (no-look-ahead property test) |
| Binance public adapter (perp + spot), rate-limit handling | `providers/binance.py` | F01 | `tests/test_provider_market.py` |
| Shared closed-bar cache, server clock, MTF snapshot | `market.py` | F01, F03 | `tests/test_provider_market.py` |
| 20-profile catalog with F13 metadata + applicability gate | `methodology.py` | F04, F13 | `tests/test_methodology.py` (Scenarios G, H, I, K) |
| Experimental evaluators: Minervini trend template (partial), Weinstein stages, Turtle, Darvas | `strategies.py` | F04 | `tests/test_strategies.py` |
| Perspectives (gate → native evaluation, hybrid tactical layer, no vote) | `perspectives.py` | F07, F13 | `tests/test_methodology.py` |
| Typed rule DSL + NL compiler + LLM proposal validation | `rules.py`, `compiler.py` | F05 | `tests/test_rules_compiler.py` (Scenarios B, C) |
| Persistence: settings, versioned strategies, hash-bound approval, alerts, usage ledger | `store.py` | F05, F06, F09 | `tests/test_monitor_store.py` |
| Always-on monitor (per-timeframe cadence, idempotent alerts, backoff) | `monitor.py` | F06 | `tests/test_monitor_store.py` (Scenarios D, L) |
| Challenge My Thesis (deterministic) | `thesis.py` | F12 | `tests/test_thesis_copilot.py` (Scenario J) |
| Copilot router, consent/budget gates, vision input checks | `copilot.py` | F07–F09 | `tests/test_thesis_copilot.py` (Scenario E) |
| Backtester (next-open fills, costs, funding, stops, gaps, walk-forward) | `backtest.py` | F10 | `tests/test_backtest.py` |
| ML baseline (leak-free dataset, purged split, baselines) | `ml.py` | §5, R6 | `tests/test_ml.py` (sklearn test skipped: not installed) |
| Local API (Host allowlist, token, CORS, rate limit) + CLI | `api.py`, `cli.py` | F11 | `tests/test_api.py` |

Test command: `.venv/bin/python -m pytest` — see the latest result in the session report.

## Live verification

- **2026-10-26 15:06 UTC, run by the owner:** `my-ai-trading analyze --symbol SANDUSDT` against public Binance USD-M.
  - Forming bars were excluded on every timeframe: last closes were 1h 15:00, 4h 12:00, 1d 00:00.
  - 1w bars were Monday-aligned (closed Mon 26 Oct 00:00 UTC).
  - 400 bars per timeframe (1w: 300, the full listing history), no gaps, nothing stale; server-time sync worked.
  - This run exposed float noise in event evidence values, now fixed by rounding.

## Independent security review (read-only Breaker, 2026-10-26)

12 findings, 9 of them reproduced. Every reproduced finding is fixed, with a regression test in `tests/test_breaker_regressions.py`:

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | High | Compiler regexes backtracked badly: a 2000-char prompt took ~12 s and blocked other work | Collapse whitespace, 200-char clause cap, endpoint rate-limited |
| 2 | Medium | Request bodies parsed before auth, with no size limit | ASGI guard checks Host + token + Content-Length (64 KB JSON, ~5 MB upload) before any read |
| 3 | High | One unresolvable watchlist symbol stopped all monitoring | Per-symbol resolution with a 10-minute retry and UNRESOLVED status |
| 4 | High | A failure on a secondary timeframe skipped the strategy check for that bar | Per-rule/per-methodology isolation; the bar is marked processed only when complete; alerts delivered per subscription |
| 5 | High | One malformed provider row poisoned the cache permanently | Merged data validated before caching; bad data raises MarketDataError (502) |
| 6 | Medium | Stale subscriptions re-polled every second | Jittered backoff on stale data |
| 7 | Medium | Alert paging lost alerts | Ascending order after a cursor, plus `next_after_seq` / `has_more` |
| 8 | Low-Med | A long-idle cache reported a fake gap | Refetch when the hole exceeds one fetch window |
| 9 | Low | Snapshot `as_of` could predate its data | `series(now_ms=...)` pinned to the snapshot instant |
| 10 | Low | Docs/CDN scripts on the local origin, `testserver` host, redirects followed, clock re-sync hammering, budget race, status iteration race | `/docs` is opt-in (`serve --docs`); `testserver` removed; no-redirect opener; 60 s re-sync retry; budget lock; status uses a snapshot copy |
| 11 | Low | Turtle "stop distance" read as trade guidance | Relabelled as a volatility reference, for study only |

Known and documented, not changed: after an outage the monitor evaluates only the latest closed bar; bars missed during the outage are not replayed. A rule's `cooldown_bars` means "minimum bars between alerts" (1 = every bar).

## Not implemented / not verified

- **Live data:** only the single read-only `analyze` run above. The monitor, API server and dry-run have not been run live.
- **Live AI:** the Anthropic adapter is untested against the real API; it needs `pip install '.[ai]'`, credentials, `MY_AI_TRADING_MODEL`, consent in Settings, and optionally `MY_AI_TRADING_PRICES` for dollar estimates.
- **Chrome extension:** integrated (`extension/`, guide in `docs/EXTENSION.md`). 33/33 unit tests pass, and it was exercised end to end in headless Chrome against the real API code with synthetic data. Not yet tried: opening the side panel from the toolbar, real desktop notifications, symbol detection from a real Binance tab, live data, screen readers, and real 200% browser zoom.
- WebSocket streaming. The monitor polls REST once per bar close, which is enough for ≥1m bars and needs no extra dependency.
- Equity session calendars, licensed fundamentals/macro data, RS universe ranking, VCP detection, Brandt pattern recognition, and the other 16 profiles (catalog-only).
- OpenCV/OCR preprocessing, LightGBM, PyTorch/PEFT (none installed; not needed by current features).
- Python 3.11 compatibility is targeted (`requires-python >= 3.11`, CI matrix) but only 3.14.4 has been run locally.
- Full-history secret scanning (Gitleaks), remote branch protection on `main` (currently **unprotected**; repo is **public**).

## Run locally

```bash
# one-time: project-local venv (Ubuntu's python3.14-venv/ensurepip is not installed; see audit doc)
python3 -m venv .venv            # or: python3 -m venv --without-pip .venv + bootstrap pip
.venv/bin/python -m pip install -e '.[api,test]'      # add ,ai for the Claude adapter
.venv/bin/python -m pytest                             # offline tests
.venv/bin/python -m my_ai_trading demo                 # synthetic, no network
.venv/bin/python -m my_ai_trading compile "4h close above SMA25 and volume at least 2x prior 20 bars"
.venv/bin/python -m my_ai_trading token                # creates ~/.config/my-ai-trading/api_token (0600)
.venv/bin/python -m my_ai_trading serve                # 127.0.0.1:8765, PUBLIC Binance data; add --docs for /docs
(cd extension && npm ci && npm run build && npm test)   # then chrome://extensions → Load unpacked → extension/dist
```

## Next executable tasks

1. Owner: approve a bounded live smoke test (`my-ai-trading analyze --symbol SANDUSDT`), protect `main`, and commit/push.
2. Load the extension in your own Chrome and try it on a Binance futures tab (see docs/EXTENSION.md).
3. Optional: surface strategy revision and monitor status in the extension (API exists).
4. Implement additional profiles only with sourced, testable criteria.
