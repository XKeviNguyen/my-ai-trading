# Project journey

This page records how My AI Trading grew from a documentation-only starter into a working research platform: what we built, which problems we hit, and how we solved them. It's written for future contributors and for anyone who wants to know why the code looks the way it does.

For the current feature status, see the [README](../README.md) and [PROGRESS.md](PROGRESS.md). For the rules that shaped every decision here, see [AGENTS.md](../AGENTS.md) and [SECURITY.md](SECURITY.md).

## Timeline

| Date | Milestone | Commit or PR |
|---|---|---|
| 2026-10-26 | Repository starts as product requirements and engineering policy only | `0622a95` |
| 2026-10-26 | v0.2: Fast Engine, methodologies, Strategy Studio, monitor, copilot, local API, Chrome extension | `ba9799a`, PR #1 |
| 2026-10-26 | CI fixed for the Starlette test client | `c5b73e4` |
| 2026-10-26 | Local chart-screenshot analysis with OpenCV and OCR; README rewritten for readers | `993b7d7` |
| 2026-10-27 | MACD, Bollinger Bands, ADX, chart patterns, monthly timeframe, ML lab, saved reports, AI answer cache, desktop app | `8bedde8`, PR #2 |
| 2026-10-27 | Four code review findings on PR #2 fixed, each with a regression test | `2c9c49c` |
| 2026-10-27 | Neon night design system for the Chrome side panel and the desktop app | PR #3 |
| 2026-10-27 | Exchange accounts and testnet Trading Mode (R8): Binance, Bybit, OKX, Interactive Brokers paper | PR #5 |
| 2026-10-27 | README rebuilt as a visual tour: architecture, workflows, database, screenshots | PR #6 |
| 2026-10-27 | Independent six-breaker audit of `main` (`0586cd1`): not release-ready, 4 High findings | `docs/audit-2026-10-27` |
| 2026-10-28 | Six-breaker remediation: all findings fixed or documented, re-audited by six breakers and two verifiers | `fix/six-breaker-remediation` |

## Starting point

The first commit held no code. It held a product requirements document (PRD v1.1), the engineering policy, a security policy and a roadmap. A separate Python V0.1 prototype existed outside the repository. Before we reused any of it, we audited it and recorded the findings in [V0.1_AUDIT.md](audits/V0.1_AUDIT.md).

Three rules from the policy shaped everything that followed:

1. **Read-only.** The product never places orders and never asks for exchange credentials.
2. **Evidence decides.** A change is done when tests and real runs prove it, not when it looks done.
3. **Security first.** Secrets never enter the repository, and a secret scan must pass before every commit.

## Phase 1: the engine and v0.2

We built the core in Python:

- the market data layer
- indicators
- the rule compiler
- methodology profiles
- the monitor
- the SQLite store
- a local FastAPI server with token authentication
- a Chrome side panel written in TypeScript

### Problems and how we solved them

**The environment had no `ensurepip`.** Python 3.14 on the development machine could not create a working virtual environment. We downloaded the official pip wheel, verified its SHA-256 hash, and bootstrapped the environment from it.

**The newest candle was always missing.** To get N closed candles, the cache asked the exchange for N rows. One of those rows is always the candle that is still forming, so after the engine correctly discarded it, only N − 1 closed candles were left. The fix was to request N + 1 rows. The lesson: "closed bars only" has to be counted at every layer, not just checked at the end.

**The PRD's own example rule did not compile.** The example says "a 4H candle closes above SMA25 ... and the daily trend is bullish". The second clause relies on the timeframe from the first. We added context inheritance: a clause without a timeframe uses the previous clause's timeframe, and the preview shows that assumption so the user can confirm it.

**The word "token" was rejected as a credential.** The prompt safety filter treated any "token" as a secret, which blocked ordinary questions about AI token budgets. We narrowed the pattern to credential phrases such as "API key", "access token" and "private key".

**Minervini was marked unsupported on crypto.** The profile listed relative strength against a stock universe as required data, so it was never evaluated on crypto at all. We split each profile's data into required and optional parts. Now Minervini runs on the price criteria and reports what it could not check.

**SQLite connections leaked under Python 3.14.** The test suite treats warnings as errors, and Python 3.14 warns about unclosed database connections. We added an explicit `close()` and a finalizer so connections close even when the caller forgets.

**A cache lookup fetched data even on a cache hit.** `cache.setdefault(tf, upstream(...))` calls `upstream` before checking the cache. We replaced it with an explicit lookup.

**The CI secret scan could never fail.** The scan step ended with `|| true`, which hid real failures. We removed it. A security check that can't fail isn't doing anything.

### The breaker review

Before the first merge, we ran independent read-only review passes ("breakers"). Each one attacked one of the six areas in our policy: security, data, flow, environment, performance, and simplicity. They found real defects, and each was fixed with a regression test. The main ones:

| Area | Finding | Fix |
|---|---|---|
| Security | The rule compiler's regular expressions could be made to backtrack for a very long time (ReDoS) | Collapse whitespace and cap each clause at 200 characters before matching |
| Security | The API read request bodies before checking authentication | A guard checks the host, the token and `Content-Length` before any body is read |
| Security | The API accepted the test client's host name in production | Strict host allowlist |
| Security | Enabling API docs loaded scripts from a CDN by default | Docs are off unless `serve --docs` is passed |
| Security | The HTTP client followed redirects | Redirects are refused, so the token can't be forwarded |
| Flow | One bad watchlist symbol blocked the whole monitor | Each symbol is resolved separately, and unresolved symbols back off |
| Flow | A failure on a secondary timeframe marked the bar as processed, so it was skipped for good | A bar counts as processed only after all its checks finish |
| Flow | The extension's alert history skipped alerts when paging | Cursor-based paging with an ascending sequence number |
| Flow | Two concurrent requests could both pass the AI budget check | The budget check and the usage record share one lock |
| Data | A cache with missing candles was served as if complete | Gaps are detected and the cache refetches |
| Data | Invalid data could be stored in the cache | Validate before storing |
| Data | Stale data was polled at full speed | Back off when data is stale |

### CI went red

The first CI run on GitHub failed even though everything passed locally. The Starlette test client had moved to a new HTTP client package (`httpx2`), and with warnings treated as errors, its deprecation warning failed every test. We added the package to the test dependencies and kept `filterwarnings = ["error"]`, so new warnings still fail the build. We also started reproducing CI in a fresh clone with a fresh environment before each handoff, so "it works on my machine" can't hide a missing dependency.

## Phase 2: chart screenshots and a reader-first README

We added local screenshot analysis:

- OpenCV finds candle bodies and colors.
- Tesseract OCR reads price-axis labels.
- Every reading is cross-checked against real exchange data.

### Problems and how we solved them

**Images are hostile input.** A small file can decode into a huge image. We read the image dimensions from the header and reject anything over 8192 pixels per side or 16 megapixels before decoding. Tesseract runs as a subprocess with a fixed argument list, no shell, and a 15-second timeout.

**Fake test images stopped working.** Once real decoding was in place, the old placeholder byte strings were correctly rejected. We wrote a small helper that builds a real, valid PNG with the standard library, and the tests now use it.

**OCR on a real machine.** Once Tesseract 5.5 was installed, OCR on synthetic charts reached about 93% confidence in about 170 ms.

**The README was written for maintainers.** We rewrote it for people discovering the project, in plain US English. It has an honest status for every feature: available, experimental or planned.

## Phase 3: indicators, patterns, ML and a desktop app

This was the largest step. We split it into independent packages and built them in isolated git worktrees, each owning its own files. Then we integrated them one at a time and reran every test after each one.

### What we added

- **Indicators:** MACD, Bollinger Bands and ADX, with documented formulas and known-value tests.
- **Chart patterns:** swing points that need confirmation bars on the right, so nothing looks into the future.
- **Monthly timeframe:** real calendar months, not 30-day blocks.
- **Machine learning lab:** walk-forward validation with a purge gap, calibration and look-ahead checks.
- **Saved reports:** with Markdown export.
- **AI answer cache:** keyed on closed-candle times, never on the request clock.
- **Desktop app:** a native PySide6 client of the same local API.

### Problems and how we solved them

**Pattern detectors fire on random data.** Testing on random walks showed that geometric pattern detectors find "patterns" in noise. Instead of hiding that, the app says it: patterns are labeled as prompts to look, not signals. That's also why the feature is marked experimental.

**A spelling cleanup broke accessibility.** Converting the codebase to US English turned the ARIA attribute `aria-labelledby` into `aria-labeledby` in 11 places. Screen readers ignore the misspelled attribute. We restored the spec spelling and added a test that fails the build if the wrong spelling ever comes back.

**A regular expression became a tuple.** Inside a Python f-string, the quantifier `{1,3}` is read as a Python expression and becomes the tuple `(1, 3)`. We escaped it as `{{1,3}}`.

**A name collision.** FastAPI's `Path` shadowed Python's `pathlib.Path` in the same module. We import the FastAPI one as `PathParam`.

**Parallel branches started from an old base.** The worktrees were created from the first commit, not from the latest work. We didn't merge whole branches. We copied only the files each package owned, then reviewed and tested the combined result.

## Phase 4: code review on PR #2

Automated code review on PR #2 found four real defects, all in new code:

| Finding | Root cause | Fix |
|---|---|---|
| `close above the upper Bollinger band on 1d` compiled into an hourly rule | The Bollinger and MACD patterns did not capture a timeframe written before or after the indicator, so the chart default was used | Both patterns now read the timeframe on either side |
| `MACD above zero` tested the histogram instead of the MACD line | Only one MACD metric existed | New `macd_line` metric. Only an explicit "histogram" uses the histogram. |
| The AI cache could serve yesterday's answer after a new daily candle | The cache key held only each perspective's name, status and state, so a moved price level with the same state looked unchanged | The cache key now includes a fingerprint of the whole perspective |
| The desktop Analysis tab kept showing old methodology results after a settings change | The tab only listened for instrument changes | It now reloads after a settings save |

For each finding, we wrote the regression test first and confirmed it failed on the old code. Then we fixed the root cause and confirmed the test passed. Code that's green in CI isn't automatically safe: these defects passed every existing test.

## Phase 5: the neon night design system

The side panel worked but read like a plain form. Every timeframe repeated a nine-row card, so comparing 1h with 1d meant scrolling. The goal was a professional trading-terminal look for long sessions, without breaking any behavior or test.

### What we built

- **One color per kind of information.** Perspectives, model output, risk and invalidation, market events, data faults and freshness each have their own color, and the color means the same thing everywhere. Red and green never mean price direction, because the product never shows buy or sell signals.
- **A timeframe table.** Timeframes are columns and metrics are rows, so a value can be compared across 1h, 4h, 1d and 1w on one line. The Copilot facts use the same table.
- **A neon headline** with the symbol, the last close and the data freshness.
- **The visual style was chosen from real screenshots.** We rendered three styles on the same data (neumorphism, neon glow and neo-brutalism) and picked neon glow, pushed toward a rainbow 1980s-Tokyo cyberpunk look.
- **Bundled fonts.** Chakra Petch for interface text and Share Tech Mono for numbers, both under the SIL Open Font License, with license files next to them.
- **The same design in the desktop app.** The same tokens, lanes and fonts, plus tests that fail if the desktop tokens or font files ever drift from the extension's.

### Problems and how we solved them

**We needed to see the real panel, not guess.** Current Google Chrome no longer loads unpacked extensions from the command line, and the owner's live engine was already using port 8765. We served a copy of the built panel over local HTTP with a small stand-in for the Chrome APIs, pointed it at a second engine on port 8766 with synthetic data, and took headless screenshots of every tab at 320, 400 and 480 pixels wide.

**The page scrolled sideways on narrow panels.** Grid items default to `min-width: auto`, so a container grew to fit the width of the wide table inside it instead of letting the table scroll. One rule (`min-width: 0` on grid children) fixed every tab.

**Our overflow check could not fail.** The first automatic check reported "ok" for every tab. We added a positive control, a page made too wide on purpose, and it also reported "ok". The cause: the screenshot reader ignored two of the five PNG row filters. After the fix, the control trips and every real tab passes.

**A measurement was wrong because of a browser cache.** The first DOM-size comparison said the old and new Analysis tab both had 270 elements. The two runs had shared a browser profile, and the second run got the old build from the cache. With separate profiles, the result was 257 elements instead of 270, and 4 cards instead of 8. Every screenshot run now starts with a fresh profile.

**Independent reviews found real defects in the new code:**

| Finding | Fix |
|---|---|
| A status string such as `"constructor"` returned a function from the label map and crashed the render | A `Map` instead of an object literal, with a regression test for hostile names |
| "Too few bars" had been shortened to "Short", which reads like a trade direction next to a trend column | One shared label map for both tabs that never says "Short" |
| The per-timeframe trend reason and the exact bar-close time were no longer shown | Both restored |
| A screen-reader label on a paragraph, and decorative ▲/▼ glyphs read aloud | A visible label, and CSS alternative text that silences the glyphs |
| The stylesheet was not covered by the security test | A test that allows only the bundled local font files in the stylesheet |

**The interface font had uneven digits.** Chakra Petch draws the digit 1 at about 57% of the width of 0, so decimal points would not line up in a price column. Numbers use the equal-width Share Tech Mono instead.

**Qt does not take gradients everywhere.** Qt stylesheets accept a gradient as a background but not as a border image, so the desktop rainbow line is a 2-pixel widget with a gradient background. With an application stylesheet, Qt also wraps the style in a proxy, so the test checks which style the theme asked for, not the proxy's name.

**Legibility was measured, not judged by eye.** Every text color was checked against every background it appears on, and all of them meet WCAG AA (at least 4.5:1). The lowest is 5.23. A desktop test keeps it that way.

## Phase 6: exchange accounts and testnet trading (R8)

Until R8, the product never touched an exchange account. We changed that on purpose, in small, explicit steps.

### The boundary came first

Before any code, we changed the policy (`AGENTS.md`, the PRD, `SECURITY.md`) and reviewed the diff on its own:

- Live accounts are read-only.
- Orders go only to official testnets and demo accounts, after a preview and an explicit per-order confirmation.
- Withdrawals, transfers and automatic trading stay out of scope.
- Credentials live only in memory or the operating system's credential vault, with an expiry the user chooses.

The rest of the work implements that boundary and tests it.

### What we built

- **Credential lifecycle:** session-only by default. Optional lifetimes of 1 h to 30 days, or until removed, use the OS vault.
- **Exchange adapters:** Binance (spot and USD-M futures), Bybit V5, OKX V5, and Interactive Brokers paper accounts through IBKR's own local gateway. All four sit behind one adapter interface.
- **Trading service:** preview against exchange rules, a single-use confirmation that expires after 60 s, a journal row written before every send, and reconciliation of unknown outcomes.
- **Portfolio and trade analytics:** exact decimal math, and every figure labeled as reported, derived or estimated.
- **UI:** an Accounts tab in the side panel and the desktop app.
- **Streams:** Binance kline streams that wake the monitor when a bar closes.

### Problems and how we solved them

**An unknown permission must not mean "off".** We first read Binance key permissions with `bool(...)`, so a missing field became "not allowed". If Binance ever renamed a field, a key that can trade would have passed as read-only. Every flag is now True, False or unknown, and a live key with any unknown write permission is refused.

**Several exchange facts we would have gotten wrong from memory.** We checked every endpoint against the official docs or the official SDK types, and that caught:

- Binance's symbol config reports margin mode as `CROSSED`, not `cross`.
- Binance's V3 position endpoint no longer reports leverage.
- Binance retired the legacy futures WebSocket URLs on 2026-04-23, so kline streams must use `/market`.
- Binance documents HTTP 408 and error -1006 as "execution status unknown", not as rejections.
- OKX demo trading uses the live host plus a header.
- OKX swap sizes are in contracts, not coins.
- IBKR has no way to confirm that a live session cannot trade, so live IBKR is unsupported.

**"Not found" is not "never sent".** Right after a timeout, an exchange can briefly report an order as missing even though it was accepted. We used to mark it NOT_FOUND on the first lookup. At the time, an order stayed UNKNOWN until it had been missing for at least three lookups spread over a minute; Phase 9 replaced that rule. Orders are never resent automatically, and a final state such as FILLED or CANCELED is never downgraded when an exchange purges old orders.

**One number must never mix two kinds of money.** An independent review found that the portfolio added live and testnet balances together. The portfolio now shows one environment at a time, and prices come from the same exchange and environment as the balance.

**Fees are money too.** The trade analytics first dropped entry fees, mixed fees paid in BNB into USDT results, and invented short trades for spot sells whose buy was older than the loaded history. All three are fixed, and each has a test with hand-computed values.

**Expired secrets must not be stranded.** If the OS vault is locked when an expired key should be deleted, we used to drop the metadata anyway, leaving the secret in the vault with nothing pointing to it. The metadata is now kept, marked "pending delete", and retried on the next start.

**Secrets can leak through error messages.** FastAPI's default 422 response echoes the submitted value, and the field path could echo a key pasted as a JSON field name. Both are scrubbed now. Credentials must be printable ASCII, because a non-ASCII character used to crash the HTTP layer with part of the key in the log.

**Parallel work, one integration gate.** We built the Bybit, OKX, Interactive Brokers and desktop parts in parallel, in isolated worktrees with strict file ownership. We integrated them one at a time, reran every suite after each, and kept only the files each part owned. Independent read-only security and data reviews then found 4 high and 8 medium issues, none critical. We fixed them all with regression tests, except two that we documented as known limits: UI-enforced per-order confirmation, and market-order size rules.

### Code review on PR #5

CI was green, but automated code review on PR #5 found five real defects. All five came from rules that were right in one place and wrong in another:

| Finding | Root cause | Fix |
|---|---|---|
| A futures position closed at its entry price showed no trade | Opening and closing fills were told apart by "realized PnL is zero", which is also true for a break-even exit | Opening or closing is decided from the position the fill meets; the exchange's realized PnL is used only for the closed part |
| Bybit futures shorts disappeared from performance | The new "spot cannot go short" rule also ran on futures fills, because Bybit reports no realized PnL | The no-short rule applies only to spot; derivatives keep short lots |
| IBKR paper orders were impossible in the desktop app | The desktop ticket offered a fixed list of markets without "stock" | The ticket uses the connected exchange's own markets, as the extension does |
| A late status check could overwrite CANCELED with NEW | "Keep a final state" was checked in Python, then written in a separate step | The rule lives in the SQL `UPDATE` itself, so it is atomic |
| An OKX inverse swap left out of positions was not disclosed | Adapter warnings stopped at the snapshot | Both clients list them as "Not included" next to exposure and positions |

As before, each fix has a regression test that reproduces the reviewer's exact example.

### OCR accuracy, measured honestly

We asked for 99% accuracy on chart price-axis labels, and made the claim testable first. A seeded generator renders chart-like images with dark and light themes, Hershey and real TrueType fonts, prices from 0.00008 to 1,700,000 with thousands separators, gridlines, price tags, JPEG compression, noise and HiDPI scales. Tuning used one seed range only; the reported number comes from a separate held-out range that was never tuned on.

| Held-out (400 images, 2,970 labels) | Before | After |
|---|---|---|
| Exact-match accuracy | 83.06% | 99.53% |
| Precision | 94.12% | 99.93% |
| Images fully correct | 231 | 391 |

The gains came from four changes:

- finding the label column by chroma instead of saturation
- reading each row separately, upscaled, with a numeric whitelist and a vote between readings
- checking that labels sit on one evenly spaced scale
- re-reading only the rows that break that scale

Readings that break the scale are reported as outliers, never silently "corrected". A monospace font with dotted zeros is still a weak spot (87.7%), and the benchmark does not prove anything about real exchange screenshots. Both are written down in [OCR_BENCHMARK.md](OCR_BENCHMARK.md).

### Not verified yet

No adapter has run against a real testnet from this repository. That needs the owner's testnet keys, entered in the app. The desktop app has been tested offscreen only.

## Phase 7: a README that shows how it works

The README described features, but readers could not see how the parts fit together. We rebuilt it for two audiences: people evaluating the engineering, and people who want to use the app.

- **Diagrams rendered by GitHub:** Mermaid diagrams for the system architecture, the candle-to-insight pipeline, monitoring, AI routing, the testnet order sequence and its states, the credential lifecycle, and the database (an ER diagram generated from the real schema).
- **Real screenshots:** taken from the actual extension and desktop app against a simulated testnet account, and labeled as synthetic data.
- **Tables built from the code:** the API surface, tech stack, security model, engineering decisions and test counts come from the code and test runs, not from memory.

### Problems and how we solved them

**Overclaiming creeps into marketing text.** The first draft called the app a workbench "for crypto and stock traders" and listed the desktop app as running on three operating systems. Stock analysis is not supported (only IBKR paper accounts), and the desktop app has been tested only on Linux. Both claims were corrected before review.

**A screenshot exposed a real layout flaw.** In the desktop portfolio, the tables used only part of the window width and left empty space under short tables. We fixed the shared table helper: the last column stretches to fill the width, numbers are right-aligned, and the height fits the rows. Then we retook the screenshot.

**Route listings can mislead.** A script that listed the API routes showed none of the account or trading routes, because this FastAPI version nests included routers. The tests proved the routes exist; the README table was built from the router source instead.

## Phase 8: an independent six-breaker audit

After R8 and the README were merged, we audited `main` (`0586cd1`) as if someone else had written it. Six read-only breakers (security, data, flow and concurrency, environment, performance, simplicity) attacked an isolated copy of the repository in parallel; nobody changed product code during the audit. The full report is [audits/2026-10-27-six-breaker-audit.md](audits/2026-10-27-six-breaker-audit.md).

**Verdict: not release-ready.** All three test suites were green (383 + 41 + 95, no skips), and the core boundaries held under attack: no path to a live order at any layer, no secret in the database, logs, responses or git history, and every indicator matched an independent numpy/pandas oracle. But the audit confirmed 4 High and 19 Medium findings that the tests had never exercised.

| High finding | What a user could experience |
|---|---|
| An acknowledged order could be declared "never arrived" | The app suggested placing a new order while the first one existed: a duplicate |
| An order the exchange reported as NEW could later be downgraded to NOT_FOUND | The same duplicate risk, e.g. after an IBKR gateway session change |
| The rule compiler changed explicit timeframes and dropped negations | "1 month" became 1-minute bars; "does not close above" became "closes above"; the user could approve a rule that means something else |
| Truncated futures history invented trades | A +10 realized gain was reported as a short trade with P&L 0 |

### Problems and how we solved them

**Green tests had hidden the riskiest paths.** Every High finding sat in behavior the suites did not cover: late or missing exchange listings, words around a recognized phrase, fill histories that start mid-position. The fix plan pairs each finding with the regression test that would have caught it.

**"Not found" was again treated as proof.** Phase 6 taught us that "unknown is not no", yet the 60-second NOT_FOUND rule still turned repeated empty lookups into permission to place a new order. A lesson written in prose was not enough; the remediation encodes it in the state machine (never downgrade an acknowledged or exchange-reported order).

**Every serious claim was re-run by the lead.** Breaker reproductions ran inline because their read-only mode forbade creating files. The lead therefore re-ran the four Highs and four Mediums with saved scripts before reporting them as confirmed.

**An audit harness touched a real host.** One breaker run built the default app and sent a request with a dummy, non-secret test key to the official Binance testnet, which rejected it. No real credential was involved, and we recorded it as an incident: audit harnesses must always inject fake adapters.

**Some checks could not run, and we said so.** Security scanners were not installed, real-browser and real-display GUI tests had no isolated environment, and real testnets had no authorized keys. They are listed as BLOCKED, not counted as passes.

## Phase 9: fixing what the breakers found

We fixed the audit findings on `fix/six-breaker-remediation`, starting from `main` at `9488a2e`. Each fix comes with a regression test. We first ran each new test against the code before the fix, to make sure it failed there. The full tracking table, one row per finding, is in [section 11 of the audit](audits/2026-10-27-six-breaker-audit.md#11-remediation-tracking-2026-102728).

**Result:**
- All 4 High and 19 Medium findings are fixed or documented with evidence.
- The re-audit found one new High, which is fixed.
- No Critical or High is open.
- The suites went from 399 / 41 / 95 to **563 / 55 / 114** passing (Python, extension, desktop), with nothing failed or skipped.
- Release readiness still depends on CI running on GitHub and on the owner actions below.

| Area | What changed |
|---|---|
| Order reconciliation | `state` is the last state the exchange confirmed, and it only moves forward. A lookup that does not find the order is recorded as "not listed" and never changes the state, so the app never suggests a new order. Cancel works for any order that is not final. "Mark resolved" exists only for orders the exchange never acknowledged, and is refused while a send is in flight. A lost connection falls back only to the same exchange, environment and key. |
| Rule compiler and evaluator | An explicit timeframe is never replaced. Any word the compiler does not understand, including every negation, leaves the clause UNRESOLVED. Multi-timeframe crossings use values as of the previous trigger close. An independent oracle made 133,968 checks with 0 mismatches. |
| Trade analytics | A close older than the loaded history becomes an "entry unknown" trade. The split is decided by average-cost realized P&L, the way exchanges compute it. Fee shares are exact, and positions in other currencies are not added to USD. |
| Security | The body cap counts real bytes; uploads spill to disk; chunked bodies, duplicate headers and invalid Unicode are refused. Token and database files refuse symlinks. Settings saves need `If-Match`. CI scans the full git history for secrets. |
| Performance | Backtests went from 1,266 to 13 µs per bar at 1,499 bars, with identical results. AI calls no longer hold a lock during the network call, and their budget reservation is an upper bound. The monitor went from 1,053 to 2 SQL statements per tick. Alerts have a retention window. |
| CI and docs | New jobs for the extension, the desktop app (offscreen) and secret scanning. Every action is SHA-pinned. An OpenAPI drift test, and corrected claims across the README and docs. |

### Problems and how we solved them

**State and evidence were one field.** The old journal mixed "what the exchange last told us" with "what our last lookup saw". We split them into `state` and `verification`, so a missing listing can no longer rewrite history.

**Our first "Mark resolved" opened a new hole.** The re-audit showed that the user could resolve an order while its send was still in flight, and the acknowledgement would arrive afterwards. A verifier then found a second gap: the "being sent" mark was set just after the journal row was written. We closed both in three layers:
- The SQL update itself requires `acknowledged=0`.
- The order is marked as being sent before its journal row exists.
- Any later exchange report replaces the user's mark.

**A fix created a new bug.** Making stocks shortable fixed one test case, but IBKR fills cover only 7 days. Selling a long bought earlier then became a phantom short. We reverted the change and documented stock shorts as a known limit, instead of trading one wrong answer for another.

**A clever check failed against a different P&L method.** Our first way to tell a flip from an old close compared first-in-first-out P&L with the exchange's realized P&L. Exchanges use average cost, so 87 of 1,340 random histories got phantom trades. We now track the average-cost position as well. A 400-case property test against an exchange-style oracle guards it.

**Regular expressions matched part of a sentence.** `re.search` found "closes above SMA 50" inside "does not close above SMA 50". Every word in a clause must now be used, apart from a few filler words.

**A deadline that was only checked between reads did not hold.** A server that sends one byte at a time never triggered the check. A watchdog now shuts the socket down at the deadline, including sockets that connect late.

**Our own test tripped our secret scanner.** A test used a variable named `secret_key`, which the scanner's assignment rule matches. We renamed it, and now scan every changed file before proposing a commit.

**An outside reviewer found two more defects after the push.** The Codex reviewer on PR #8 reported two problems:
- **P1:** a split exit gave the matched part its first-in-first-out P&L and the rest its average-cost P&L. The parts no longer added up to what the exchange reported: 60 instead of 110 in its example. The matched share now uses average cost too, so every split adds up to the reported P&L.
- **P2:** the connection fallback still trusted the key's last four characters, a limit we had only documented. Two different keys ending the same way counted as one, so private requests could go to the wrong account. Each connection and order now stores a one-way SHA-256 fingerprint of the API key, and the fallback matches on that. The four characters are kept for display only.

Both fixes have tests that fail on the pushed commit. The lesson: a limit that can send requests to the wrong account is a bug, not a footnote.

**Workers hit a usage limit.** All three worktree workers stopped at a session limit at the same time. Their partial work was saved in their worktrees, and we resumed them instead of starting again.

### Still open

- **Documented limits:**
  - Stock short sales and break-even closes older than the history cannot be detected.
  - An acknowledged order that the exchange no longer lists or cancels stays open in the journal.
  - A send already past its final Trading Mode check still goes out.
- **Follow-ups:**
  - More secret spellings for the scanner.
  - Alert and cache memory at the largest watchlist.
  - Worst-case windowed rules in backtests.
  - Fixed market list in the desktop Performance form.
- **Owner actions:**
  - Protect `main`.
  - Decide on Gitleaks and a Python lock file.
  - Provide testnet keys and an AI budget for the first real runs.

## Lessons we keep

1. **Count closed bars at every layer.** Most of our data bugs came from the boundary between forming and closed candles.
2. **Check before you parse.** Authenticate before reading a body, check image dimensions before decoding, and cap length before running a regular expression.
3. **Never substitute a default silently.** If a user writes a timeframe, use it. If a value is assumed, show the assumption.
4. **Cache keys must include everything the answer depends on.** If the key leaves out an input, the cache will someday return a wrong answer.
5. **Every bug gets a regression test that failed first.** This includes tooling mistakes such as the spelling sweep.
6. **Reproduce CI in a clean environment.** A clean clone and a fresh virtual environment catch missing dependencies before CI does.
7. **Be honest in the product.** Mark experimental features as experimental, label hypothetical results as hypothetical, and say when a detector also fires on noise.
8. **Every check needs a positive control.** A check that has never failed may be broken. Make it fail on purpose once.
9. **Look at the real thing.** Screenshots of the real panel found layout bugs that every unit test had passed.
10. **Unknown is not "no".** A missing permission flag, a "not found" order and a 3xx response all mean "we do not know yet". Treat them as unknown and fail closed.
11. **Check exchange facts against the docs, every time.** APIs change: hosts move, fields disappear, codes gain meanings.
12. **Audit what you merged as if a stranger wrote it.** Green suites only prove the paths someone thought to test. Adversarial, read-only breakers with independent oracles found what the tests missed.
13. **Re-audit the fixes too.** Our own fixes introduced one High and several Mediums; only an independent re-audit found them.
14. **Check a heuristic against how the other side computes.** A check built on first-in-first-out failed against exchanges that use average cost.

## What's next

- **UI polish follow-ups:** the comparison table in the desktop app (it still uses one card per timeframe), and a real-display check of the desktop app.
- **R8 follow-ups:** a first run on each exchange's testnet with real testnet keys, market-order size rules, hedge-mode positions, and partner approval for one-click exchange sign-in.
- **Blocked on external inputs:**
  - live AI features, which need an API key and a budget
  - stock data, which needs a licensed provider
  - self-hosted models, which need datasets, hardware and licenses
