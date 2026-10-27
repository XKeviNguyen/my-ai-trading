# Six-Breaker audit — 2026-10-27

**Audited commit:** `0586cd157e2bd3f94b5d08d3eb4677343b955cdb` (`origin/main`, the merge of PR #6; its file tree is identical to `4527840`).
**Method:** audit-first. Six independent, read-only breakers inspected an isolated worktree; no product code was changed. The lead re-ran the highest-risk reproductions independently (marked **lead-verified**).
**Environment:** Ubuntu (Linux 7.0.0-38), 16 CPUs, 14 GB RAM, Python 3.14.4 (project venv), PySide6 6.12 (offscreen), Node 22.22.1, Chrome 156. Python 3.11 is not installed locally; it was judged from CI and static analysis only.

## 1. Executive summary

**Release readiness: NOT READY.** No path to a live order and no secret leak was found: those core boundaries held under direct attack. But four **High** findings are confirmed. Two of them can lead a user to place a **duplicate testnet order**, one can make a user **approve a strategy rule that means something different from what they wrote**, and one **misreports realized P&L**. All six breakers returned FAIL.

| | Count |
|---|---:|
| Critical | 0 |
| High | 4 |
| Medium | 19 |
| Low | 21 |
| Informational | 16 |

What held under attack: no live order is possible at any layer (service and all four adapters, zero network calls); live keys with trade, withdraw or transfer rights are refused (except one OKX edge case, SEC-2); secrets never reached SQLite, logs, responses, `repr`, AI prompts or git history; preview tokens are single-use under 16 concurrent submits; the Host allowlist blocks DNS rebinding; web origins cannot reach private routes; every indicator matched an independent numpy/pandas oracle (max relative error 3e-14); the backtester, crossings, pattern no-look-ahead, ML labels and purges, FIFO on complete histories and the `rule_hash` all matched independent oracles.

## 2. Scorecard

| Breaker | Verdict | Why |
|---|---|---|
| Security | **FAIL** | Request-size cap can be bypassed (SEC-1, concern D); OKX empty permission list accepted on live (SEC-2); `main` unprotected on a public repo; no CI secret scanning |
| Data | **FAIL** | Rule compiler silently changes explicit timeframes and drops negations (DATA-1); truncated fill history invents trades (DATA-2) |
| Flow & concurrency | **FAIL** | NOT_FOUND after lookups can encourage a duplicate order (FLOW-1, FLOW-2) |
| Environment | **FAIL** | A crash during a send leaves an unrecoverable order (ENV-1 = FLOW-4); one input causes HTTP 500; real Chrome, fresh install and real-display checks BLOCKED |
| Performance & reliability | **FAIL** | Quadratic backtests and a lock held across LLM calls can stall the whole local API (PERF-1, PERF-2); alerts grow without bound (PERF-3) |
| Efficiency & simplicity | **FAIL** | Security-relevant documentation contradicts the code; committed OpenAPI spec covers 20 of 41 operations |

## 3. Test baseline (lead-run, isolated worktree)

| Suite | Command | Exit | Result | Skips |
|---|---|:-:|---|:-:|
| Python | `PYTHONPATH=<worktree>/src .venv/bin/python -m pytest -p no:cacheprovider -rs` | 0 | **383 passed** in 20.8 s | 0 |
| Extension | `cd extension && npm test` (tsc + build + node:test) | 0 | **41/41** | 0 |
| Desktop | `QT_QPA_PLATFORM=offscreen … desktop/.venv-desktop/bin/python -m pytest tests` | 0 | **95 passed** | 0 |
| CI on `0586cd1` | GitHub Actions `tests` | — | Python 3.11 and 3.14 passed | — |

| Check | Status | Reason |
|---|---|---|
| `pip-audit`, `bandit`, `gitleaks`, `trufflehog`, `semgrep`, `ruff`, `mypy` | **BLOCKED** | Not installed; installing needs owner approval |
| Dependency vulnerabilities | Partial | OSV batch query of the local venv (58 packages): 0 known; npm registry advisory lookup: none. Not a substitute for `pip-audit`/`npm audit` |
| Git history secrets | Partial | Regex scan of all 17 commits: 3 hits, all known fixtures. Not Gitleaks |
| Real-browser extension E2E | **BLOCKED** | Breaker sessions could not create a Chrome profile; earlier screenshot runs are not a test |
| Real-display GUI (Wayland/X11) | **BLOCKED** | No isolated display server; the live user session was deliberately not used |
| Fresh install / offline pip venvs | **BLOCKED** | Needed file creation; static review only |
| Real exchange testnets | **BLOCKED** | No authorized testnet credentials |
| GitHub secret scanning / push protection settings | **INCONCLUSIVE** | Not visible without an authenticated token |

## 4. Findings

Status: **CONFIRMED** (reproduced), SUSPECTED, INCONCLUSIVE, DISPROVED. "Lead-verified" means the lead re-ran it independently; the scripts are kept with the audit notes (`scratchpad/audit/lead/repro_*.py`). Breaker reproductions ran as inline scripts because their read-only mode forbade creating files; each finding lists the recipe.

### High

**FLOW-1 · Flow · CONFIRMED (lead-verified) — An acknowledged order can be declared "never arrived".**
- **Where:** `accounts/trading.py:296-307`; `adapters/bybit.py:369-374`, `adapters/okx.py:466-470`.
- **Repro:** fake adapter whose `place_order` returns an acknowledgement with an exchange order id and status UNKNOWN (Bybit/OKX async ack); `query_order` returns `None`; advance the injected wall clock 61 s; reconcile three times.
- **Actual:** `UNKNOWN, UNKNOWN, NOT_FOUND` with the note "it most likely never arrived. You may place a new order."; `needs_reconcile` becomes empty although `order_json` holds the exchange id.
- **Expected:** an order the exchange acknowledged is never NOT_FOUND.
- **Root cause:** the NOT_FOUND rule counts lookups and age but ignores the stored acknowledgement.
- **Impact:** duplicate testnet order; contradicts the project rule "a 'not found' order means we do not know yet".
- **Fix:** allow NOT_FOUND only when no exchange id was ever stored; remove the "You may place a new order" wording; keep such rows in `needs_reconcile`.
- **Regression test:** acknowledged order + `None` lookups over > 60 s never becomes NOT_FOUND and stays listed.

**FLOW-2 · Flow · CONFIRMED (lead-verified) — An order the exchange reported as NEW is downgraded to NOT_FOUND.**
- **Where:** `accounts/trading.py:297-309`, `store.py:370-375`.
- **Repro:** submit returns NEW; the order later drops out of the listing (IBKR new gateway session, exchange purge); three reconciles after 61 s give `UNKNOWN, UNKNOWN, NOT_FOUND`. Also: the miss counter never resets when the order is seen; "3 lookups over 60 s" can be one lookup at t=0 plus two in the same millisecond at t=60 s; two concurrent clicks both count.
- **Expected:** an exchange-reported state is never moved to UNKNOWN/NOT_FOUND by a later absence.
- **Exchange evidence:** IBKR `query_order` searches only the current gateway session's orders (`ibkr.py:235`), and the gateway login repeats daily; Bybit order history has a delayed, time-windowed view; OKX keeps unfilled canceled orders only briefly; Binance USD-M purges canceled/expired unfilled orders after 3 days.
- **Fix:** never downgrade a state the exchange reported; reset `lookups` when seen; count a miss only after a minimum spacing (e.g. 20 s).
- **Regression test:** NEW → unlisted for > 60 s → state stays NEW (or "unverified"), never NOT_FOUND.

**DATA-1 · Data · CONFIRMED (lead-verified) — The rule compiler silently changes timeframes and inverts negations.**
- **Where:** `compiler.py:38, 48, 98-99, 189-197`.
- **Repro:** `compile_prompts([p])`:

| Prompt | Compiled to (status COMPILED) | Expected |
|---|---|---|
| "close is above SMA 10 on the 1 month chart" | `CLOSE on 1m > SMA(10) on 1m` (1-**minute**) | 1M, or UNRESOLVED |
| "today closes above SMA 50 on the 4h" | `… on 1d` (explicit 4h dropped) | 4h |
| "candle does not close above SMA 50 on 1h" | `CLOSE on 1h is above SMA(50)` (negation lost) | UNRESOLVED or "below / not above" |
| "closes above the prior 20-day high on the 1h" | `PRIOR_HIGH(20) on 1h` (20 hours) | UNRESOLVED (PRD F03: 50-day ≠ 50-hour) |

- **Root cause:** `_TF` has no word boundaries (`\d{1,2}\s?[mhdw]` swallows "1 month"); `re.search` matches part of a clause, so leftover words ("not", "today", "20-day") are never flagged.
- **Impact:** a user can approve a rule whose meaning differs from what they wrote — the core promise of Strategy Studio.
- **Fix:** word-bounded timeframe tokens; map or reject month/week words; require the whole clause to be consumed (`fullmatch`) and send any remainder to UNRESOLVED; negation lexicon → UNRESOLVED; "N-day" with a non-1d timeframe → UNRESOLVED.
- **Regression test:** a parametrized table of the prompts above.

**DATA-2 · Data · CONFIRMED (lead-verified) — Truncated futures history invents trades and loses realized P&L.**
- **Where:** `accounts/analytics.py:384-403`.
- **Repro:** `closed_trades([sell 1@110 realized=+10, buy 1@100 realized=0])` on `usdm_perpetual` (entry fill older than the loaded history).
- **Actual:** one closed **short** with P&L 0 (`exchange_reported`); the +10 is lost. **Expected:** a closed long of +10, plus an open position of unknown entry.
- **Root cause:** a closing fill that finds no lot opens a phantom opposite lot. Fill history is routinely truncated (OKX fills cover 3 days / 100 rows; `performance()` loads 1000).
- **Fix:** when no lot exists, use the exchange's close signal (non-zero realized P&L, position side) to record an "entry unknown" closed trade instead of opening a lot.
- **Regression test:** the truncated sequence above.

### Medium

| ID | Breaker | Status | Where | Defect → impact | Smallest fix | Regression test |
|---|---|---|---|---|---|---|
| SEC-1 | Security | CONFIRMED (lead-verified) | `api.py:139-145` | **Concern D.** The guard trusts `Content-Length`; a request declaring 2 bytes streamed 2,097,593 bytes → HTTP 200 (raw ASGI). Under real uvicorn/h11, `Content-Length` + `Transfer-Encoding: chunked` streamed 3 MiB → 200. Token required, so not open to outside callers. | Reject `transfer-encoding`; wrap `receive` to count bytes and return 413 above min(declared, cap) | Raw ASGI over-stream + h11 socket CL+TE tests |
| SEC-2 | Security | CONFIRMED (lead-verified) | `adapters/okx.py:245-257` | Live OKX key with `perm: ""` or `" , "` is **accepted** (read=None). Breaks "unknown is refused". Unlikely payload; raised from Low because it is the live-key safety boundary. | Empty token set or missing `read_only` → all flags None | Parametrized live-policy test for empty/blank perms |
| FLOW-3 | Flow | CONFIRMED | `store.py:352-364`, `trading.py:308` | A late "not seen" write overwrites NEW with UNKNOWN (lost update between non-final states). Both UIs offer Cancel only for NEW/PARTIALLY_FILLED, so a live order stuck at UNKNOWN/NOT_FOUND cannot be canceled. | "Not seen" writes only `WHERE state IN ('SENDING','UNKNOWN','NOT_FOUND')`; show Cancel for UNKNOWN/NOT_FOUND | Interleaved reconcile vs submit; UI shows Cancel |
| FLOW-4 / ENV-1 | Flow, Env | CONFIRMED | `trading.py:282-286, 322`; `credentials.py:247-256, 279-294` | An order with an unknown outcome is stranded when its connection goes away: expiry → `CredentialExpired`; crash on a session key → restart purge deletes the connection; reconnecting creates a new id. The row stays SENDING/UNKNOWN forever. | Keep connection metadata (as "reconnect needed") while open journal rows reference it, or let reconcile use a new connection with the same exchange, environment and key hint | SENDING row → restart → reconnect → `query_order` called |
| FLOW-5 | Flow | CONFIRMED | `trading.py:142-151`, `credentials.py:262-263` | `set_mode` reads then upserts the full metadata without the lock: a concurrent disconnect is undone (`purge_pending` reset, `trading_enabled` true), or deleted metadata is resurrected. | Compare-and-set under `CredentialStore._lock`; fail if gone or purge-pending | Barrier-ordered disconnect vs `set_mode` |
| DATA-3 | Data | CONFIRMED | `analytics.py:359, 395` | `"stock"` is not shortable, so an IBKR short (sell 10@100, buy 10@90) gives no trade, one unmatched sell, and a phantom long lot. | Add `stock` (or adapter-declared shortability) | Stock short round trip |
| DATA-4 | Data | CONFIRMED | `rules.py:548-550, 577` | Multi-timeframe `crosses_above` compares each timeframe's previous bar instead of values as of the previous trigger close → false MATCH. Reachable via raw-rule API and LLM rule proposals. | Evaluate previous values with `as_of = previous trigger close + 1` | 1h-close × 1d-SMA crossing oracle case |
| DATA-5 | Data | CONFIRMED | `market.py:339-352` | Incremental fetches never backfill a gap: after a partial provider response, analysis stays degraded (SMA200 None, trend UNKNOWN) for 200+ bars. | Full refetch when fresh data starts after a gap or the merged series has gaps | Partial response then healthy calls → gap closed |
| DATA-6 | Data | CONFIRMED | `analytics.py:283-288`, `ibkr.py:254-258` | IBKR positions in EUR are added to USD exposure and can trigger concentration warnings. | Carry the position currency; convert or list as unpriced | EUR position excluded from USD totals |
| PERF-1 | Perf | CONFIRMED | `backtest.py:111, 139`; `rules.py:208` | Backtests are quadratic (per-bar cost 110 µs → 1130 µs from 100 to 1499 bars) and hold the GIL: 20 concurrent 1499-bar backtests (within the default burst of 20) push `/health` p95 to 14.0 s. | Compute indicators once over the series, or a 1-worker process pool with its own limiter | Per-bar cost at 1499 ≤ 1.5× cost at 400 |
| PERF-2 | Perf | CONFIRMED (lead-verified) | `copilot.py:168-170, 191` | `_budget_lock` is held across the provider call (120 s timeout): 6 concurrent asks with a 0.5 s provider completed at 0.5, 1.0 … 3.0 s; with 60 concurrent asks, `/health` stalled 19.9 s (threadpool exhausted). | Reserve budget under the lock, release during the call, reconcile after; or a semaphore that fails fast with 429 | Slow fake provider in flight → `/health` < 100 ms |
| PERF-3 | Perf | CONFIRMED | `store.py:229-234` | Alerts are never deleted: 50 symbols × 20 rules → 439,800 rows / 336 MB after 1000 hourly ticks (≈ 2.9 GB/year). Python heap flat (no leak). | Retention by age or row cap; smaller payload | DB bounded after many ticks |
| SIMP-1 | Simplicity | CONFIRMED (lead-verified) | `pyproject.toml:21` | `analytics` extra installs pandas, which is never imported; docs tell users to install it. | Remove the extra; declare numpy where used | Suite passes without it (already true in CI) |
| SIMP-2 | Simplicity | CONFIRMED | `accounts/trading.py:178-224` | A preview that breaks exchange rules still makes 9–10 HTTP calls (3 signed) before refusing; `symbolConfig` fetched twice. | Raise right after `validate_against_rules` | Counting transport: no signed calls on a rule refusal |
| GH-A | Security | CONFIRMED | GitHub settings | **Concern A.** `main` is unprotected (`protected: false`, no rulesets) on a **public** repo: code can reach `main` without review or a passing CI. | Branch protection: PR required, `tests` check required, no force-push | GitHub API shows protection enabled |
| GH-B | Env | CONFIRMED | `.github/workflows/ci.yml` | **Concern B.** CI runs only the Python job; the 41 extension and 95 desktop tests never run in CI. | Add an extension job (`npm ci && npm test`) and an offscreen desktop job | CI shows three jobs green |
| GH-E | Security | CONFIRMED | `.github/workflows/ci.yml` | **Concern E.** No secret scanning in CI (only the local hook). | Pinned Gitleaks (or equivalent) full-history job | CI job fails on a planted fixture secret |
| DOC-1 | Simplicity | CONFIRMED (lead-verified) | `docs/EXTENSION.md:5, 77` | States the extension "places no orders… does not use exchange accounts or API keys" — false since R8 (it sends API keys once and testnet orders after confirmation). Security-relevant doc drift (concern H). | Rewrite the permissions/what-it-does sections | Doc review checklist |
| DOC-2 | Simplicity | CONFIRMED (lead-verified) | `docs/api/openapi.json` | Committed spec lists 20 of 41 live operations; all accounts, trading and reports routes missing. | Regenerate; add a test comparing it to `app.openapi()` | Spec-drift test |

### Low

| ID | Where | Defect | Fix |
|---|---|---|---|
| SEC-3 | `api.py:56-67`, `store.py:111-116` | Token file opened without `O_NOFOLLOW`/`fchmod`; existing token mode not checked; dangling DB symlink target is created (needs same-user or shared-dir misconfiguration) | `O_NOFOLLOW`, owner/mode check, atomic 0600 create |
| SEC-4 / ENV-5 | `credentials.py:179-185, 283-307` | A vault error that is not a `KeyringError` (e.g. `RuntimeError` from D-Bus) crashes `purge_on_start` → app fails to start; `get`/`list` return 500 | Map any backend exception to `VaultUnavailable`; isolate per entry |
| FLOW-6 | `trading.py:260-267` | Trading Mode switched off between submit's check and the send still sends; a preview built during the switch survives | Per-connection mode generation re-checked just before send |
| FLOW-7 | `streams.py:119-123` | Subscription version read after the handshake: a change during the handshake is never applied | Capture version with the stream names |
| FLOW-8 / SIMP-8 | `api.py:396-403`, extension `app.ts:64-71` | Settings PUT is a full replace: concurrent tabs can silently restore a revoked AI consent | `If-Match`/`updated_at` precondition or field PATCH |
| FLOW-9 | `store.py:168-177` | Concurrent strategy revisions → unhandled `IntegrityError` → 500 | `MAX(version)+1` inside `BEGIN IMMEDIATE` |
| FLOW-10 | `service.py:92-101` | Snapshot fetch overlapping a disconnect re-caches private data after the clear | Cache only if the connection still exists |
| FLOW-11 | `monitor.py:132`, `api.py:262-266` | Stop during a tick still fetches every remaining subscription; shutdown wait unbounded | Check `stop` per iteration; bounded wait |
| DATA-7 | `domain.py:83-85, 200` | Bars not aligned to their interval (Thursday weekly opens, 12:00 daily) are accepted | Check `open_ms % duration` (Monday offset for 1w) |
| DATA-8 | `perspectives.py:35-40` | Perspectives ignore `as_of_ms` and evaluate later bars (latent) | `series.up_to(as_of_ms)` |
| DATA-9 | `analytics.py:380, 388, 430` | Fee proration by division prints non-terminating decimals | Allocate remainder to the last chunk; quantize outputs |
| DATA-10 | `analytics.py:348-349` | A missing fee (None) is treated as 0 and not flagged | Count as excluded |
| DATA-11 | `rules.py:348-353` | `"right": true` accepted as 1.0 | `mode="before"` validator |
| DATA-12 | `rules.py:591` | numpy-float candles make `_eval` return `np.bool_` → INSUFFICIENT_DATA (latent) | Wrap in `bool(...)` |
| ENV-2 | `providers/binance.py:87` | Garbage HTTP / oversized headers (captive portal) → HTTP 500 instead of 502 | Catch `http.client.HTTPException` |
| ENV-3 | `api.py:52`, `store.py:104` | Empty `XDG_*` variables put the token and DB in the current directory; desktop then finds no token | `os.environ.get(X) or default`; ignore relative paths |
| ENV-4 | `accounts/service.py:65-71` | The key is sent to the exchange before checking the vault is available for the chosen lifetime | Check the vault first |
| ENV-6 | `api.py:405-414` | A lone surrogate in a strategy prompt → HTTP 500 (lead-verified); the only 5xx in a ~20-route Unicode fuzz | Reject non-UTF-8-encodable strings at validation |
| PERF-4 | `monitor.py:198, 73, 124` | Strategy rules re-read per subscription: 1778 SQL statements per tick (50 symbols × 20 rules) | Load once per tick |
| PERF-5 | `market.py:22` | Market cache (256 keys) smaller than the largest allowed watchlist (400 subscriptions) → 3.7× more rows fetched | Size from settings |
| PERF-6 | `market.py:54-56`, `monitor.py:104-108` | No backoff when the first clock sync fails: 60 calls/min and a logged exception per second | Backoff in `run_forever` |

### Informational

- **SEC-5:** private routes accept any extension origin unless `MY_AI_TRADING_EXTENSION_ID` is set; a bare token without `Bearer` is accepted.
- **SEC-6:** the public `PUT /settings` echoes values in its 422 via `str(ValidationError)` (private routes do not).
- **SEC-7 / FLOW-12:** a wrong-user or wrong-token submit consumes the preview (pop before check).
- **SEC-8:** the confirmation token is returned with the preview, so per-order confirmation is enforced by the UI, not the server (documented limitation; testnet only).
- **SEC-9:** Python dependencies use open ranges with no lockfile or hashes; `httpx2` provenance not verified.
- **ENV-7/8/9:** high-contrast detection needs Qt ≥ 6.10 while the floor is 6.8 (suspected); the forced-colors tab rule is overridden by a later rule (suspected); input borders measure 1.86:1, below WCAG 1.4.11's 3:1 for non-text, although README claims "WCAG AA contrast" (all text tokens pass ≥ 5.67:1).
- **PERF-7/8:** recovery after a 1 h outage takes ~880 s (backoff cap); stream reconnect delay can exceed its own 120 s cap (jitter applied after the cap).
- **SIMP-3…7:** duplicated clock-offset caches with inconsistent invalidation; dead methods (`TokenSource.has_pasted`, `ApiClient.report_json`); `ml.py` unreachable from the CLI/API; duplicated market/symbol rules and labels across backend and clients (extension Performance form lacks "stock"); oversized `create_app` (cyclomatic ≈ 79); a hidden `copilot` ↔ `chartvision` import cycle.
- **Process:** the orchestration docs prescribe heavy ceremony (six breakers, YAML contracts) even for small changes, mostly unenforced; make breakers risk-triggered.

### Disproved (checked and sound)

No live order at any layer; live-key policy for Binance, Bybit and OKX (except SEC-2); no secret in SQLite, journals, `repr`, pickle, 422 responses, extension storage or desktop QSettings; expiry exact at every lifetime boundary; `purge_pending` retry; double submit (8 and 16 threads → 1 order); preview cap (80 concurrent → 50 accepted); `journal_update`/`journal_lookup` atomicity (200 trials × 8 writers); Copilot budget atomic within one process; alert dedup; wake vs backoff; Host allowlist; origin checks; no CSRF path; fixed hosts, no redirects or proxies; IBKR TLS exception scoped to the fixed loopback URL; no AI → order path; all indicators, the 1M calendar, backtester, crossings, pattern no-look-ahead, ML labels/purge and complete-history FIFO matched independent oracles; no Python heap leak in the monitor; 429 thundering herd prevented by the provider-level block.

## 5. Known concerns A–H

| Concern | Verdict | Evidence |
|---|---|---|
| **A.** `main` unprotected | **CONFIRMED** | GitHub API: `protected: false`, `rulesets: []`, public repo (GH-A) |
| **B.** CI lacks extension and desktop jobs | **CONFIRMED** | `ci.yml` has one `python` job (GH-B) |
| **C.** NOT_FOUND may encourage another order | **CONFIRMED** | FLOW-1, FLOW-2 (lead-verified); exchange-specific reasons in FLOW-2 |
| **D.** Content-Length checked without byte counting | **CONFIRMED** | SEC-1 (lead-verified raw ASGI; breaker-verified real uvicorn CL+TE) |
| **E.** No full-history secret scanning in CI | **CONFIRMED** | No scan step (GH-E); GitHub-side secret scanning INCONCLUSIVE without auth |
| **F.** OAuth blocked by provider approval | **CONFIRMED, honestly disclosed** — one false doc sentence | `ACCOUNTS_TRADING.md` says "state/PKCE/callback validation are built and tested"; no such code or tests exist |
| **G.** Real testnet integration unverified | **CONFIRMED, honestly disclosed** | `ACCOUNTS_TRADING.md` and README say no testnet run happened |
| **H.** Stale or false documentation | **CONFIRMED** | DOC-1, DOC-2; CLAUDE.md/AGENTS.md "documentation-only"; PROGRESS.md test counts (68/33), "WebSocket not implemented", self-contradictions, no R8 rows; `ACCOUNTS_TRADING.md` desktop "in progress"; README static "519 passing" badge although CI runs 383; ROADMAP lacks R7/R8 |

## 6. Performance (selected measurements)

| Metric | Workload | n | Median | p95 / p99 |
|---|---|---:|---:|---:|
| `create_app()` | fake service, in-memory store | 12 | 27.0 ms | max 27.3 ms |
| `/analysis` | 4 timeframes | 300 | 5.97 ms | 6.50 / 6.81 ms |
| `/trading/preview` | simulated adapter | 300 | 1.56 ms | 1.72 / 1.90 ms |
| `analyze()` | 20,000 bars | 15 | 24.0 ms | ≤ +6 % |
| Monitor hourly tick | 50 symbols × 4 tf, 20 rules, 3 methods | 8 | 236 ms | max 350 ms |
| `/backtest` event | 1h, 1499 bars, walk-forward | 1 | 1770 ms | — |
| Peak RSS | `create_app` / monitor 50 symbols / desktop window | 1–4 | 61 / 51 / 82 MB | — |
| OCR | 20 held-out images | 20 | 357 ms | p95 490 ms |
| Extension `dist` | total / fonts | — | 364,467 B / 209,189 B (57 %) | — |

All RSS runs also passed under a 2 GB address-space limit (`prlimit --as`). Steady-state latency, memory and provider call volume are healthy; the failures are the burst and slow-provider behaviors in PERF-1 and PERF-2.

## 7. Product requirements coverage

| Area | State |
|---|---|
| Public multi-timeframe analysis, indicators, alerts, monitor | Implemented, oracle-verified; gap backfill (DATA-5) partial |
| Strategy Studio (prompt → typed rule) | Implemented; **meaning-preservation defects (DATA-1)** |
| 20 methodology profiles, fidelity modes | Implemented (7 with rule checks) |
| Copilot / Challenge My Thesis / AI routing and budgets | Implemented; **live LLM never exercised**; concurrency (PERF-2) |
| Chart vision (OpenCV + OCR) | Implemented; 99.53 % on a synthetic benchmark only |
| ML research lab | Library only (not reachable from CLI/API) |
| Backtesting | Implemented, oracle-verified; quadratic cost (PERF-1) |
| Exchange accounts (Binance, Bybit, OKX, IBKR paper) | Implemented offline; **real testnets unverified (G)** |
| Testnet Trading Mode | Implemented; **reconciliation defects (FLOW-1, FLOW-2, FLOW-3, FLOW-4)** |
| One-click OAuth | **Blocked by provider approval** |
| Chrome extension, desktop app | Implemented; tested offscreen / unit only; no CI |

## 8. Remediation plan (ordered by risk and dependency)

1. **Trading reconciliation (FLOW-1, FLOW-2, FLOW-3, FLOW-4/ENV-1).** Never downgrade an exchange-reported or acknowledged state; NOT_FOUND only without any exchange id, with spaced, reset-on-seen lookups; remove "place a new order"; conditional "not seen" writes; Cancel for UNKNOWN/NOT_FOUND; keep connection metadata while open journal rows exist. *Retest:* the FLOW repro scripts plus new regression tests; flow breaker re-audit.
2. **Rule compiler meaning (DATA-1, DATA-4).** Word-bounded timeframes, whole-clause consumption, negation and N-day handling; multi-timeframe crossing as of the previous trigger close. *Retest:* parametrized prompt table; crossing oracle.
3. **Trade analytics (DATA-2, DATA-3, DATA-6).** Entry-unknown closes, shortable stocks, position currency. *Retest:* truncated-history, stock-short and EUR cases.
4. **Request guard (SEC-1) and live-key policy (SEC-2).** Byte-counting `receive`, reject Transfer-Encoding; OKX empty permissions fail closed. *Retest:* ASGI and h11 socket tests; parametrized policy test.
5. **Availability (PERF-2, PERF-1, PERF-3).** Release the budget lock during calls; bound backtest concurrency or make it linear; alert retention. *Retest:* `/health` under a slow provider and concurrent backtests; DB-size bound.
6. **Repository controls (GH-A, GH-B, GH-E).** Owner action: branch protection; add extension and desktop CI jobs and a pinned secret-scan job.
7. **Documentation (DOC-1, DOC-2, concern H, F).** Correct EXTENSION.md, ACCOUNTS_TRADING.md (PKCE sentence, desktop status), PROGRESS.md, ROADMAP, CLAUDE.md/AGENTS.md starter wording, README badge and contrast claim; regenerate OpenAPI with a drift test.
8. **Low findings**, grouped by module, each with the regression test listed above.

After remediation, rerun the full suites and the affected breakers independently; a breaker passes only when its high-risk checks run.

## 9. Untested or externally blocked

Real exchange testnets (no authorized credentials); live LLM calls (no key/budget approval); real-browser extension E2E and service-worker suspension; real Wayland/X11 GUI; fresh-install and offline-venv runs; Python 3.11 locally (CI only); `pip-audit`/`bandit`/`gitleaks`/`semgrep` (not installed); GitHub-side secret scanning settings (needs auth); multipart temp-file spooling size (SEC-1 extension, suspected); DST/session calendars for equities (no equity calendar exists).

## 10. Audit-process notes

- Breakers could not save their reproduction scripts (their read-only mode forbade creating files, so all ran inline). The lead re-ran 8 reproductions with saved scripts: FLOW-1, FLOW-2, SEC-1, SEC-2, DATA-1, DATA-2, ENV-6, PERF-2, plus the DOC-1, DOC-2 and SIMP-1 spot checks.
- **Incident:** one environment-breaker run accidentally constructed the default app and sent a signed request with a **dummy, non-secret test key** to the official Binance testnet; Binance rejected it (HTTP 424 at the app layer). No real credential was involved. Later runs used fakes and an unroutable socket. Lesson: audit harnesses must always inject fake adapters; consider a test-mode flag that refuses real exchange hosts.

## 11. Remediation tracking (2026-10-27/28)

**Branch:** `fix/six-breaker-remediation`, from `main` at `9488a2e` (the audit merge). **Fix commit:** the owner commits the branch; this table refers to that branch commit. **Process:** the lead fixed trading, the compiler and evaluator, analytics, the request guard, the store and credentials. Three implementers worked in separate git worktrees with exclusive file ownership (performance; extension and desktop; CI and docs), and the lead integrated their owned files only. Six read-only breakers then re-audited the integrated tree. Their new findings were fixed, and two read-only verifiers retested those fixes. Every lead regression test was first run against the earlier code to confirm it fails there. The two exceptions are noted in the table.

**Final gates (local, 2026-10-28):** Python 561 passed, extension 55/55, desktop 114 passed, 0 failed, 0 skipped. `tools/export_openapi.py --check` reports the spec up to date. `tools/secret_guard.py --history` passes (305 file revisions). The 91 changed or untracked files have no secret findings. There is no new Critical or High.

### Original findings

| ID | Sev | Fix | Regression test | Re-audit verdict |
|---|---|---|---|---|
| FLOW-1 | High | No automatic NOT_FOUND; `verification` kept apart from `state` | `test_trading_reconcile.py::test_flow1_*` | FIXED |
| FLOW-2 | High | Exchange states never downgraded; spaced misses, reset when the order is seen | `test_flow2_*` | FIXED |
| DATA-1 | High | Word-bounded timeframes, whole-clause consumption, negation lexicon, N-day check, "1 M" | `test_rules_compiler.py::test_compiler_never_changes_the_meaning_of_a_clause`, `test_reaudit_compiler_and_rule_value_cases` | FIXED (after re-audit: "1 M", trend words) |
| DATA-2 | High | Entry-unknown closes; split decided by average-cost realized P&L | `test_accounts_analytics.py::test_a_close_whose_entry_*`, `test_realized_pnl_beyond_*`, `test_average_cost_*` (400 random cases) | FIXED. Limit: a break-even close older than the history is undetectable |
| SEC-1 | Med | Byte-counted, spooled body; chunked refused; duplicate headers refused | `test_request_guard.py` (raw ASGI and real uvicorn) | FIXED |
| SEC-2 | Med | OKX perm without `read_only` gives all permissions as unknown | `test_adapter_okx.py::test_an_okx_perm_list_without_read_only_*` | FIXED |
| FLOW-3 | Med | Late miss ignored after a newer confirmation; Cancel for uncertain orders | `test_flow3_*` | FIXED |
| FLOW-4/ENV-1 | Med | Fallback to a connection with the same exchange, environment and key | `test_flow4_*`, `test_n2_*` | FIXED. Limit: the key hint is the key's last 4 characters |
| FLOW-5 | Med | Compare-and-set connection metadata | `test_flow5_*` | FIXED |
| DATA-3 | Med | Reverted: stocks stay non-shortable, because IBKR's 7-day fills made phantom shorts | `test_a_stock_sell_before_the_loaded_history_is_unmatched_*` | **DOCUMENTED LIMIT** (needs per-position shortability) |
| DATA-4 | Med | Crossings evaluated as of the previous trigger close | `test_multi_timeframe_crossing_*` | FIXED (133,968 oracle checks, 0 mismatches) |
| DATA-5 | Med | Full refetch on new holes; known gaps re-checked after max(1 h, 24 bars) | `test_perf_market.py` | FIXED |
| DATA-6 | Med | Position currency; non-USD positions listed, excluded and noted | `test_a_position_in_another_currency_*` | FIXED |
| PERF-1 | Med | Per-bar values computed once; at most 2 concurrent backtests, more → 429 | `test_perf_backtest.py`, `test_api.py::test_a_busy_backtest_pool_*` | FIXED (1,266 → 13 µs/bar at 1,499 bars; identical outputs) |
| PERF-2 | Med | Reservation outside the lock; in-flight limit → 429; reservation is an upper bound | `test_perf_copilot.py` incl. `test_worst_case_concurrent_*` | FIXED |
| PERF-3 | Med | 45-day alert retention, newest row kept, `created_at` index | `test_store_retention.py` | FIXED (bounded by activity × 45 days) |
| SIMP-1 | Med | `analytics` extra removed; numpy declared | — (packaging) | FIXED |
| SIMP-2 | Med | Refusal before fees, snapshot and leverage | `test_trading.py::test_a_rule_breaking_preview_makes_no_signed_calls` | FIXED |
| GH-A | Med | — | — | **OWNER ACTION** (branch protection) |
| GH-B | Med | CI jobs: extension; desktop offscreen | — (CI) | FIXED (not yet run on GitHub) |
| GH-E | Med | `secret_guard.py --history` job; app secret names; type changes | `test_secret_guard.py` | FIXED. Gitleaks is an owner decision |
| DOC-1 | Med | Stale claims corrected across README and docs | — | FIXED |
| DOC-2 | Med | `tools/export_openapi.py`; drift test | `test_openapi_drift.py` | FIXED |
| SEC-3 | Low | O_NOFOLLOW, owner and mode checks, 0600; DB symlink refused; DB mode tightened | `test_request_guard.py` | FIXED |
| SEC-4/ENV-5 | Low | Any vault error becomes VaultUnavailable | `test_accounts_credentials.py` | FIXED |
| FLOW-6 | Low | Mode generation checked before storing and before sending | `test_flow6_*` (both branches) | FIXED. Limit: a send already past its final check still goes out |
| FLOW-7, PERF-8 | Low | Version captured with the names; jitter before the cap | `test_perf_streams.py` | FIXED |
| FLOW-8 | Low | ETag; `If-Match` required (428), 412 on conflict; clients send it | `test_settings_concurrency.py`, extension and desktop tests | FIXED |
| FLOW-9 | Low | `BEGIN IMMEDIATE` around read and insert | `test_store_retention.py::test_concurrent_revisions_*` | FIXED |
| FLOW-10 | Low | Cache epoch read before the fetch | `test_accounts_api.py::test_a_snapshot_overlapping_*` | FIXED |
| FLOW-11 | Low | Stop checked per subscription; shutdown wait 5 s | `test_perf_monitor.py`, `test_api.py::test_shutdown_*` | FIXED (shutdown test not run on old code: it would hang) |
| FLOW-12/SEC-7 | Low | Token and user checked before the pop | `test_flow12_*` | FIXED |
| DATA-7 | Low | UTC alignment; Monday weeks; 3d on whole days | `test_bars_that_do_not_open_*` | FIXED |
| DATA-8 | Low | `up_to(as_of)` for native and HYBRID layers | `test_methodology.py` | FIXED |
| DATA-9 | Low | Exact fee shares; plain output | `test_prorated_fees_*`, `test_performance_prints_plain_numbers` | FIXED |
| DATA-10 | Low | A missing fee is excluded, not zero | `test_a_missing_fee_*` | FIXED |
| DATA-11 | Low | bool, str and non-finite values and periods rejected | `test_boolean_*`, `test_a_period_must_be_a_real_integer` | FIXED |
| DATA-12 | Low | `float()` on metric values | `test_numpy_candles_*` | FIXED |
| ENV-2 | Low | HTTPException becomes MarketDataError | `test_perf_provider_http.py` | FIXED |
| ENV-3 | Low | Empty or relative XDG ignored (engine and desktop) | `test_request_guard.py`, desktop tests | FIXED |
| ENV-4 | Low | Vault checked before the key is sent | `test_the_vault_is_checked_before_*` | FIXED |
| ENV-6 | Low | Strict UTF-8 and lone-surrogate check for every JSON spelling | `test_request_guard.py` | FIXED |
| ENV-7/8/9 | Info | PySide6 ≥ 6.10; forced-colors last; border contrast ≥ 3:1 | extension and desktop tests | FIXED (real Chrome and display not tested) |
| PERF-4/5/6 | Low | Rules loaded once per tick; 814-key cache; sync backoff | `test_perf_monitor.py`, `test_perf_market.py` | FIXED |
| SEC-5 | Info | `Bearer ` prefix required | `test_request_guard.py` | FIXED |
| SEC-6 | Info | No value or unknown-key echo in settings 422 | `test_settings_concurrency.py` | FIXED |
| SIMP-4/5 | Info | Dead methods removed; extension markets from the catalog | extension and desktop tests | FIXED (desktop Performance form still uses fixed markets) |

### New findings from the re-audit and verification (all fixed unless marked)

| ID | Sev | Defect | Fix | Test |
|---|---|---|---|---|
| N1, D1 | **High** | An acknowledged order could end as USER_RESOLVED | `acknowledged=0` in SQL; refused while sending (marked before the row exists); exchange reports replace USER_RESOLVED | `test_n1_*` (4 tests) |
| N2 | Med | Fallback used another account's key | key-hint match | `test_n2_*` |
| N3 | Low | Older answer moved the state back; timeout overwrote a found state | forward-only state ranks; `only_from="SENDING"` | `test_n3_*` |
| N4 | Med | IBKR cancel ignored the stored order ID | cancel by the stored ID | `test_adapter_ibkr.py::test_an_order_from_an_earlier_gateway_session_*` |
| N5 | Low | Failures counted as misses; ignored miss reported "not listed"; refused mode change dropped previews | — | `test_n5_*` |
| ENV-12, B | Med | Garbage HTTP reply → 500 and a stuck SENDING row; broken error body | HTTPException becomes OutcomeUnknown; safe error-body read and close | `test_adapter_binance.py` |
| ENV-13, C | Med | Total deadline not enforced (drip, late connect) | socket watchdog | `test_perf_provider_http.py` |
| D2 | Low | Duplicate Content-Type bypassed the Unicode check | duplicate single-valued headers → 400 | `test_duplicate_content_type_*` |
| A | Med | First-in-first-out vs average-cost P&L made phantom trades | average-cost tracker | `test_average_cost_*` |
| Copilot | Low | Overspend when real usage exceeded the reservation | upper-bound input estimate | `test_worst_case_*` (old 44,091 > 40,000) |
| — | Low | Stuck acknowledged order that is no longer listed or cancellable | — | **DOCUMENTED LIMIT** |
| D4 | Low | 4-character key hint can collide | — | **DOCUMENTED LIMIT** |
| D5 | Low | Some secret spellings missed (`apiSecret`, `BINANCE_SECRET`) | — | **OPEN**: a generic `SECRET` rule matches a fake constant already in history; Gitleaks is the owner's decision |
| — | Low | Worst-case windowed rule backtest ≈ 2.2 s; alerts ≈ 840 MB at the extreme watchlist; full cache ≈ 328 MB | — | **OPEN** (follow-ups) |

### Owner actions

1. Protect `main`: require a pull request and the `python`, `extension`, `desktop` and `secret-scan` checks, and block force-pushes (GH-A).
2. Decide on Gitleaks or GitHub secret scanning with push protection, and on a Python lock file with hashes (SEC-9).
3. Provide testnet keys for a first real testnet run, and an LLM key and budget for live AI checks. Neither has run from this repository.
