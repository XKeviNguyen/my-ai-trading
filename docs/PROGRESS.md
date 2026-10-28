# Progress & Handoff — My AI Trading

**Last updated:** 2026-10-27 · **Branch:** work lands on `main` through pull requests. The owner commits, pushes and merges.
States are *implemented + offline-tested* unless marked otherwise. One read-only public `analyze` run against live Binance is the only live check so far (see below). Nothing has been tested against a live AI provider or a real exchange testnet.

## Implemented (offline tests pass)

| Area | Module(s) | PRD | Evidence |
|---|---|---|---|
| Market data types, closed-bar semantics, gaps, interval identity | `domain.py` | F01, F03 | `tests/test_domain.py` |
| Indicators with stated conventions | `indicators.py` | F02 | `tests/test_indicators.py` (property tests) |
| Fast Engine events with invalidation, stale suppression | `engine.py` | F02, F06 | `tests/test_engine.py` (no-look-ahead property test) |
| Binance public adapter (perp + spot), rate-limit handling | `providers/binance.py` | F01 | `tests/test_provider_market.py` |
| Shared closed-bar cache, server clock, MTF snapshot | `market.py` | F01, F03 | `tests/test_provider_market.py` |
| 20-profile catalog with F13 metadata + applicability gate | `methodology.py` | F04, F13 | `tests/test_methodology.py` (Scenarios G, H, I, K) |
| Rule-check evaluators for 7 profiles: Minervini trend template (partial), Weinstein stages, Turtle, Darvas, Williams volatility breakout, Raschke Holy Grail, Brandt classical patterns | `strategies.py` | F04 | `tests/test_strategies.py` |
| Perspectives (gate → native evaluation, hybrid tactical layer, no vote) | `perspectives.py` | F07, F13 | `tests/test_methodology.py` |
| Typed rule DSL + NL compiler + LLM proposal validation (see "Rule compiler" below) | `rules.py`, `compiler.py` | F05 | `tests/test_rules_compiler.py` (Scenarios B, C) |
| Persistence: settings (with ETag / If-Match), versioned strategies, hash-bound approval, alerts, usage ledger, order journal | `store.py` | F05, F06, F09 | `tests/test_monitor_store.py`, `tests/test_settings_concurrency.py` |
| Always-on monitor (per-timeframe cadence, idempotent alerts, backoff) | `monitor.py` | F06 | `tests/test_monitor_store.py` (Scenarios D, L) |
| Binance kline WebSocket streams as a monitor wake-up signal (data still comes from REST closed bars) | `streams.py` | F06 | `tests/test_streams.py` (needs the `accounts` extra for `websockets`) |
| Challenge My Thesis (deterministic) | `thesis.py` | F12 | `tests/test_thesis_copilot.py` (Scenario J) |
| Copilot router, consent/budget gates, vision input checks | `copilot.py` | F07–F09 | `tests/test_thesis_copilot.py` (Scenario E) |
| Backtester (next-open fills, costs, funding, stops, gaps, walk-forward) | `backtest.py` | F10 | `tests/test_backtest.py` |
| ML: leak-free dataset, purged split, baselines, walk-forward logistic regression and LightGBM, calibration, leakage check (library only; not reachable from the CLI or API) | `ml.py` | §5, F10 | `tests/test_ml.py` |
| Screenshot analysis: OpenCV candle geometry, Tesseract OCR (symbol, timeframe, MA labels, price axis), cross-checks against exchange data, decompression-bomb guard | `chartvision.py` | F08 | `tests/test_chartvision.py`, `tests/test_ocr_benchmark.py` |
| MACD, Bollinger Bands, ADX; chart patterns (generic VCP approximation, cup and handle, head and shoulders, flags, triangles, wedges, supply/demand zones); pattern alerts on the newest closed bar | `indicators.py`, `patterns.py`, `monitor.py` | F02, F04, F06 | `tests/test_indicators.py`, `tests/test_patterns.py` (no-look-ahead property test; 11,370-prefix stress run: 0 mismatches) |
| Calendar-month (1M) timeframe with exact calendar arithmetic | `domain.py` | F03 | `tests/test_domain.py` |
| Saved and exportable analysis reports; AI response cache with hit rate | `reports.py`, `store.py`, `copilot.py`, `api.py` | F09, F11 | `tests/test_api.py`, `tests/test_thesis_copilot.py` |
| Local API (Host allowlist, token, CORS, rate limit) + CLI; committed OpenAPI spec checked for drift | `api.py`, `cli.py`, `docs/api/openapi.json` | F11 | `tests/test_api.py`, `tests/test_openapi_drift.py` |
| Chrome extension (side panel, all tabs incl. Accounts and Reports) | `extension/` | F11 | `extension/test/*.test.mjs` |
| Desktop app (PySide6, client of the local API; side-panel features incl. Accounts, plus Reports) | `desktop/` | F11 | `desktop/tests` (offscreen, real engine in-process) |
| **R8** CredentialStore: session or OS vault, expiry, startup purge, no secrets in SQLite or responses | `accounts/credentials.py` | §7 | `tests/test_accounts_credentials.py`, `tests/test_accounts_api.py` |
| **R8** Adapters: Binance, Bybit, OKX (live read-only, testnet orders), IBKR paper | `accounts/adapters/*` | §7 | `tests/test_adapter_*.py` (official doc payloads, offline) |
| **R8** Portfolio and trade analytics (Decimal, provenance labels) | `accounts/analytics.py` | §7 | `tests/test_accounts_analytics.py` |
| **R8** Testnet Trading Mode: preview → confirm → journal → send → reconcile | `accounts/trading.py`, `accounts/trading_api.py` | §7 | `tests/test_trading.py`, `tests/test_trading_reconcile.py` |

Status details for R8 are in [ACCOUNTS_TRADING.md](ACCOUNTS_TRADING.md).

### Rule compiler: what stays UNRESOLVED

The compiler maps a clause to a typed rule only when it can consume the whole clause. Otherwise the clause stays **UNRESOLVED** and is shown to you; it is never guessed. A clause stays unresolved when it contains:

- a negation ("not", "never", "without", "doesn't", …);
- a word the compiler does not understand (only a few filler words such as "the" or "current" are ignored);
- a timeframe that is unsupported, or two timeframes that conflict (an explicit timeframe is never replaced);
- "N-day" on a timeframe other than daily (a "20-day high" on 4h bars is not 20 bars).

Multi-timeframe crossings compare values as of the **previous close of the trigger timeframe**, so every timeframe in a crossing uses the same instant.

## Test suites

CI (`.github/workflows/ci.yml`) runs four jobs on every pull request and on pushes to `main`: `python` (Python 3.11 and 3.14), `extension` (Node 22: type-check, build, `node --test`), `desktop` (Python 3.14, Qt offscreen) and `secret-scan` (full history). **The CI run is the source of truth for test counts.** Last local counts, 2026-10-27 (six-breaker remediation): Python 563 passed, extension 55/55, desktop 114 passed, 0 skipped.

Local commands:

```bash
.venv/bin/python -m pytest                                        # Python engine
(cd extension && npm ci && npm test)                              # extension
(cd desktop && QT_QPA_PLATFORM=offscreen .venv-desktop/bin/python -m pytest)   # desktop
python3 tools/secret_guard.py --history                           # full-history secret scan
python3 tools/export_openapi.py                                   # regenerate docs/api/openapi.json
```

## Live verification

- **2026-10-26 15:06 UTC, run by the owner:** `my-ai-trading analyze --symbol SANDUSDT` against public Binance USD-M.
  - Forming bars were excluded on every timeframe: last closes were 1h 15:00, 4h 12:00, 1d 00:00.
  - 1w bars were Monday-aligned (closed Mon 26 Oct 00:00 UTC).
  - 400 bars per timeframe (1w: 300, the full listing history), no gaps, nothing stale; server-time sync worked.
  - This run exposed float noise in event evidence values, now fixed by rounding.

## Independent reviews

- **2026-10-27 six-breaker audit of `main` (0586cd1):** [audits/2026-10-27-six-breaker-audit.md](audits/2026-10-27-six-breaker-audit.md). Release readiness: NOT READY at the audited commit; findings and the remediation plan are listed there.
- **2026-10-26 read-only security Breaker** (below). 12 findings, 9 of them reproduced. Every reproduced finding is fixed, with a regression test in `tests/test_breaker_regressions.py`:

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
| 11 | Low | Turtle "stop distance" read as trade guidance | Relabeled as a volatility reference, for study only |

Known and documented, not changed: after an outage the monitor evaluates only the latest closed bar; bars missed during the outage are not replayed. A rule's `cooldown_bars` means "minimum bars between alerts" (1 = every bar).

## Not implemented / not verified

- **Desktop app** on a real X11/Wayland display, tray notifications, dark theme and screen readers: tested offscreen only (also in CI).
- **Live data:** only the single read-only `analyze` run above. The monitor, API server and dry-run have not been run live.
- **Live AI:** the Anthropic adapter is untested against the real API; it needs `pip install '.[ai]'`, credentials, `MY_AI_TRADING_MODEL`, consent in Settings, and optionally `MY_AI_TRADING_PRICES` for dollar estimates.
- **Real exchange testnets:** no adapter has been run against a real testnet from this repository. One-click OAuth is blocked by provider approval.
- **Chrome extension:** unit tests run in CI. It was exercised end to end in headless Chrome against the real API code with synthetic data. Not yet tried: opening the side panel from the toolbar, real desktop notifications, symbol detection from a real Binance tab, live data, screen readers, and real 200% browser zoom.
- **Profiles:** 13 of the 20 profiles are catalog-only (no rule checks). Equity session calendars, licensed fundamentals/macro data and RS universe ranking are not implemented. The VCP detector is a generic geometric approximation, not a Minervini VCP or SEPA evaluation.
- **OCR** runs against the real Tesseract 5.5 on synthetic charts only. It has not been tested on real exchange screenshots.
- **R7** (PyTorch, Hugging Face, PEFT/LoRA): not started and not installed. It needs real datasets, evaluation goals, hardware and licenses first (PRD §5).
- **Python 3.11** runs in CI only; locally only 3.14 has been run.
- **Repository controls (owner):** branch protection on `main` (currently **unprotected**; the repository is **public**), GitHub secret scanning settings, and whether to add Gitleaks next to `tools/secret_guard.py --history`.

## Run locally

```bash
# one-time: project-local venv (Ubuntu's python3.14-venv/ensurepip is not installed; see audit doc)
python3 -m venv .venv            # or: python3 -m venv --without-pip .venv + bootstrap pip
.venv/bin/python -m pip install -e '.[api,test]'      # add ,ai for the Claude adapter; ,accounts for exchange accounts
.venv/bin/python -m pytest                             # offline tests
.venv/bin/python -m my_ai_trading demo                 # synthetic, no network
.venv/bin/python -m my_ai_trading compile "4h close above SMA25 and volume at least 2x prior 20 bars"
.venv/bin/python -m my_ai_trading token                # creates ~/.config/my-ai-trading/api_token (0600)
.venv/bin/python -m my_ai_trading serve                # 127.0.0.1:8765, PUBLIC Binance data; add --docs for /docs
(cd extension && npm ci && npm run build && npm test)   # then chrome://extensions → Load unpacked → extension/dist
```

## Next executable tasks

1. Owner: protect `main` (required PR and passing CI checks, no force-push), and decide on Gitleaks and a Python lock file (see `SECURITY.md` §6).
2. Finish the remediation plan in the 2026-10-27 audit, then rerun the affected breakers.
3. Owner: run the testnet walkthrough in `ACCOUNTS_TRADING.md` with your own testnet keys.
4. Implement additional profiles only with sourced, testable criteria.
