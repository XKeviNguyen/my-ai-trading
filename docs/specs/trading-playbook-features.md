# Trading Playbook → Features Spec

- **Status:** Features 1–5 implemented on 2026-11-01 (see "Implementation status" below). Open questions in §6 use the defaults listed there.
- **Date:** 2026-10-31
- **Source:** the owner's `trading-playbook` skill (`~/AI-Engineering-Toolbox/skills/trading-playbook/SKILL.md`, not in this repo). It paraphrases two books. Section references such as `§9.4` point into that skill. Book references follow it: `[EM chN]` = Edwards, Magee & Bassetti, *Technical Analysis of Stock Trends*, 9th ed. (2007); `[BP chN]` = Peter L. Brandt, *Diary of a Professional Commodity Trader* (2011).
- **Scope:** map every rule in §3–§11 to the current codebase, define two trader profiles (Brandt, Edwards & Magee), and propose the first five features. §12 (source conflicts) feeds the profile switches. §13–§15 (adding books, advisory layout, psychology) are out of scope except where noted.

## Implementation status (2026-11-01)

| Feature | State | Where | Differences from this spec |
|---|---|---|---|
| 1 Risk calculator | Done | `risk.py`, `POST /api/v1/risk/size`, Risk tab in extension and desktop | Risk fraction is bounded to 0.01%–2%. Not yet shown inside the testnet order preview. |
| 2 Stop library | Done | `stops.py` | Last Hour Rule and the `StopState` helper were removed. Nothing in the app used them, and a 1h-bar version needs its own design. |
| 3 Breakout quality | Done | `playbook.py`, rectangles and doubles in `patterns.py` | Out-of-line moves and recompletion (R-14, R-15) are not modeled. A sub-threshold first close may still complete within 5 bars. |
| 4 Two profiles | Done | `strategies.py`, `methodology.py` (`catalog/2`) | MAJOR means at least 10 weeks long, decided at the signal bar. Weekly confirmation is reported on its own (`weekly_close_confirmed`), so a past signal never changes class. The Edwards & Magee stop is the nearest minor swing, and basing points are reported for trailing. |
| 5 Playbook backtest | Done | `backtest.py` (`PlaybookSource`, `exit_policy`, `sizing`, `robustness`) | The stop-order fill model (E-2) is not built yet; entries fill at the next open, and the API says so. Every run also reports zero-cost and double-cost replays and a hold baseline. |
| Extra (from user-complaint research) | Done | `playbook.track_record`, `playbook.pivot_sensitivity`, `backtest.robustness` | See `docs/research/competitor-complaints-2026-10.md`. |

## 0. Ground rules this spec keeps

These come from `AGENTS.md`, the PRD and existing module docstrings. Every proposed feature must keep them.

1. **Closed bars only, no look-ahead.** Every value is computed from `CandleSeries.up_to(as_of)` (`domain.py:241`) on `contiguous_tail()` (`domain.py:222`). Swing points are confirmed only after `right` bars (`patterns.py:125`).
2. **Facts, not instructions.** Evaluators return descriptive states plus the checks that produced them, never BUY/SELL (`strategies.py` docstring, `Applicability` in `domain.py:38`). Plan levels (entry, stop, size) are labeled *scenario levels for study*.
3. **Attribution honesty.** Profiles are "research profiles inspired by publicly described methodologies" (`methodology.py` docstring). Any threshold the books do not state is marked `chosen here` with its provenance. Applying a profile outside its published market or timeframe is `ADAPTED_UNVALIDATED`.
4. **R8 boundary.** No live-money orders and no autonomous trading. Sizing output may at most prefill a **testnet** order preview that a human submits.
5. **Hypothetical results are labeled.** Backtests keep `LABEL` (`backtest.py:55`) and report costs and gaps.

## 1. Platform constraints that change the mapping

| Constraint | Effect on playbook rules |
|---|---|
| Crypto only (Binance USD-M perpetual and spot). `asset_class()` returns `crypto` or `unknown` (`methodology.py:146`). | EM was written for **equities**, so every EM rule is an extension (`ADAPTED`) here. BP's book covers futures and forex. Crypto behaves closer to BP's markets: 24x7, leverage, no shares outstanding. |
| 24x7 UTC calendar. Weekly bars open Monday 00:00 UTC (`domain.py:88`). | BP's **Weekend Rule** (Friday close) and "no thin night session" rule have no direct equivalent. They need an adapted definition (weekly-bar close = Sunday 23:59:59 UTC) or are marked not applicable. |
| Volume is venue volume (one exchange), not consolidated tape. | EM volume confirmation is only partially meaningful. Treat volume as `optional evidence` for EM on crypto, and ignore it for BP (as BP does for futures/forex, §3). |
| No fundamentals, beta universe, dividends or watchlist breadth data (`AVAILABLE_DATA_DEFAULT = {OHLCV}`, `methodology.py:24`). | EM stop table by USD price × beta, ex-dividend adjustments and the Magee Evaluative Index cannot run natively. They need adaptations: ATR% instead of beta, and the user's watchlist instead of a market universe. |
| Intraday klines are available (`1h`, `4h`, …). | BP's **Last Hour Rule** is programmable from `1h` bars. |
| Backtester fills at the **next bar's open** after a closed-bar signal (`backtest.py:9`). | BP enters with resting **stop orders** at the boundary. A stop-order fill model is needed to test BP entries faithfully. Until then, next-open is a conservative approximation and must be labeled as such. |

**Finding (verify):** the existing `brandt` profile lists `("futures", "fx", "crypto")` as `published_assets` (`methodology.py:74-79`). The 2011 *Diary* trades futures and forex. Crypto is not in that book. Unless another Brandt source is cited, crypto should move to "extension" so the gate prints the same honest note it prints for other profiles.

## 2. Status legend

| Tag | Meaning |
|---|---|
| ✅ **Exists** | Implemented today; cite file and line. May still differ from the book (noted). |
| 🟢 **Programmable** | Precise enough to implement now with OHLCV and closed bars. |
| 🟡 **Needs definition** | Implementable after the owner fixes a numeric definition (listed in §6 Open questions). |
| 🔴 **Subjective** | Discretionary judgment. Can only be *assisted* (measured, highlighted), never decided by code. |
| ⛔ **N/A here** | Needs data or a market structure this platform does not have. Adaptation possible only as `ADAPTED`. |

## 3. Two trader profiles

Both profiles describe *how a perspective reads a chart*. They share one detector layer (patterns, swings, stops) and differ only in configuration. This follows the PRD rule that perspectives are reported independently, with no vote (`perspectives.py` docstring).

### 3.1 Profile `brandt` (upgrade of the existing entry)

| Field | Value | Source / status |
|---|---|---|
| Identity | Peter L. Brandt, classical chart-pattern breakout trader | existing `methodology.py:74` |
| Evidence | `published_book` — *Diary of a Professional Commodity Trader* (Wiley 2011) | existing |
| Published markets | futures, forex (**not crypto**, see finding above) | [BP Intro] |
| Native timeframes | Signal: `1w` + `1d`. Management: the same timeframe as entry ("enter on weekly, manage on hourly is how you lose") | [BP ch5] |
| Price inputs | High, low, close only. Close-only view to confirm breakouts. No indicators; ADX very limited | [BP ch1, ch14] |
| Volume | Ignored | [BP ch9], §3 |
| Pattern preference | Horizontal boundaries (rectangle, right-angle triangle, flat-neckline H&S) over diagonal ones | [BP ch3, ch11] |
| Minimum duration | Major ≥ 10–12 weeks, visible on weekly and daily. Minor: continuation ≥ 4–8 weeks, reversal ≥ 8–10 weeks. After self-review: reject < 6–8 weeks unless tied to a weekly pattern | [BP ch4, ch5, ch13] |
| Completion filter | **Close** beyond the boundary **and** beyond the swing high/low that defines it. Decisive; no "3%" rule (too wide for futures) | [BP ch3], §12 |
| Signal classes | `MAJOR_ANTICIPATORY`, `MAJOR_BREAKOUT`, `MAJOR_PYRAMID`, `MINOR`, `INSTINCT`, `MISC` (avoid) | §9.1 |
| Entry style | Stop order at the boundary on completion; no waiting for a retest | §9.2 |
| Initial stop | **Last Day Rule (LDR)**: low of the breakout day (long). If the breakout day is mostly outside the pattern or gaps, use the prior day; for a gap breakout, the close before the gap. **Last Hour Rule** when LDR risk > ~0.8% of capital | §9.4A |
| Management | Retest Failure Rule; intervening pattern; 3-day Trailing Stop Rule (not for major breakouts until price is far toward target); one recompletion per pattern | §9.5 |
| Exit | Take profit at the measured target for most trades; non-major trades exit within days | §9.6 |
| Risk | 0.5–1% per trade, normalized ~0.7–0.8%; correlated group ≤ 2%; hard cap 2% | §8, §12 |
| Maturity today | `experimental`, evaluator `brandt_classical_patterns` (`strategies.py:271`): daily-only geometric detections, no weekly confirmation, no duration filter, no LDR | existing |

### 3.2 Profile `edwards_magee` (new)

| Field | Value | Source / status |
|---|---|---|
| Identity | Robert D. Edwards & John Magee (9th ed. editor W.H.C. Bassetti). Classical technical analysis and Dow Theory | [EM] |
| Evidence | `published_book` — *Technical Analysis of Stock Trends*, 9th ed. (2007) | [EM] |
| Published markets | equity (stocks, indexes). Crypto = extension → `ADAPTED_UNVALIDATED` label for every evaluation | [EM] |
| Native timeframes | `1d` for patterns and stops; `1w` for primary-trend and S/R context | [EM ch3, ch13] |
| Price inputs | OHLC + **volume** (central). Log scale for trendlines and percentages. MAs and oscillators auxiliary only | [EM ch2, ch36] |
| Primary trend | Dow Theory: higher highs and higher lows of intermediate swings (and the mirror) | [EM ch3–5] |
| Completion filter | Close beyond the boundary by ~3% (indexes ~2%); an upside breakout needs a clear volume increase; a downside breakout does not | [EM], §5 step 5 |
| Entry style | **Pullback** after the signal: retracement of 40–50% of the move, the basic trendline, or the old minor top. Never chase the breakout day. No third buy after ~3 steps | §9.2 EM |
| Initial stop | Percent below the nearest minor low, from the price × beta table (never < 5%) | §9.4B — ⛔ table, adapt with ATR% |
| Management | **Basing points** ("3 days away" rule); stops only move with the trade | §9.5 EM |
| Exit | Target is a **minimum**; prefer a progressive / hair-trigger stop on climactic volume | §9.6 |
| Risk | Text allows 2–3% (EN) / Turtle 1–2%; playbook default caps at 0.5–1% | §8, §12 |
| Portfolio | Composite leverage; Evaluative Index scales total long/short exposure; never put most capital against the primary trend | §8 |
| Unevaluable here | beta-based stop table, ex-dividend gaps, Evaluative Index over a market universe, Dow index confirmation (two averages) | §1 above |

### 3.3 Profile switches (from §12 conflicts)

Everything below is one parameter in a shared `ProfileConfig`. Values come from §12. "Default" is the playbook's conservative merge, used when the user picks no profile.

| Switch | `brandt` | `edwards_magee` | Default (§12) |
|---|---|---|---|
| `breakout_filter` | close beyond boundary **and** defining swing | close ≥ 3% beyond (index 2%) | both: boundary + swing, with 3% as a reference for volatile symbols |
| `volume_required_up` | false | true | true for equities, false for crypto/futures/fx |
| `entry_style` | `breakout_stop_order` | `pullback_40_50` | report both scenarios; prefer the one with the smaller absolute risk |
| `initial_stop` | `ldr` (→ `last_hour` if risk > 0.8%) | `pct_below_minor_low` (adapted: k × ATR) | compute both; take the tighter one that is still structural; never widen, shrink size instead |
| `retest_policy` | deep close inside pattern → exit; hard retest → Retest Failure Rule | throwback to the boundary is normal / a buy zone | throwback that holds is fine; a close deep inside → exit; one recompletion |
| `exit_policy` | target for most; major = LDR then trailing | target is a minimum; progressive stop | minor: target or days; major: partial at target, rest trailed |
| `risk_per_trade` | 0.5–1% | up to 2–3% (EN) | 0.5–1%, normalized 0.7–0.8%, cap 2% |
| `rising_wedge` | bearish; "running wedge" in strong commodity/fx uptrends = trade the break | bearish | bearish by default; neutral + trade the break in strong uptrends for non-equities |
| `min_pattern_weeks` | 10–12 major / 6–8 minor | none (flag ≤ 4 wk, wedge > 3 wk) | apply BP thresholds to standalone entries; shorter patterns only for pyramid / early entry / stop management |
| `use_support_resistance` | context only | core for entries and stops | context: stops and resistance estimates; never the main reason |
| `double_top_rules` | ≥ 2 months apart, ≥ 15% deep | > 1 month (usually 2–3), 15–20% deep | ≥ 2 months, ~15–20% |
| `triangle_apex_cutoff` | > 2/3–3/4 unreliable | best 1/2–3/4; > 3/4 drop | avoid past 3/4; drop the pattern after the first failed break |

**Implementation note:** `methodology.py:142` hard-asserts exactly 20 profiles. Adding `edwards_magee` requires changing that invariant (and its test) on purpose, with a catalog version bump (`CATALOG_VERSION = "catalog/2"`).

## 4. Rule-by-rule mapping (§3–§11)

Columns: **ID** · **Rule (paraphrased)** · **Status** · **Existing code** · **Proposed home** · **Test criteria**. Test criteria are deterministic: synthetic OHLCV built with `fixtures.py` helpers (`candles_from_closes`, `series_from_closes`), or hand-built candles where highs/lows matter. Every new detector also gets a **no-look-ahead test**: its result for bar *k* must equal its result on `series.up_to(close_k + 1)`.

### §3 Foundations and volume

| ID | Rule | Status | Existing code | Proposed home | Test criteria |
|---|---|---|---|---|---|
| F-1 | Trend is assumed to continue until a clear reversal signal | ✅ (proxy) | `engine.trend_state` (`engine.py:108`): SMA50/SMA200 + slope | keep; add swing-based trend (F-2) | existing tests |
| F-2 | Primary trend = higher highs + higher lows of **intermediate** swings (Dow) | 🟡 | `swing_points` (`patterns.py:125`) gives minor swings only | `structure.py: swing_trend(series, degree)` on `1w` | ascending zig-zag → `UP`; descending → `DOWN`; a lower high after HH/HL → `TRANSITION`; result for bar k unchanged by bars > k |
| F-3 | Reversal needs time and volume; a bigger pattern means a bigger move | 🔴 | — | report pattern width and height only | — |
| F-4 | Win rate ~30–35%, ≥ 8 consecutive losses per year expected, ~10% of trades carry net profit | 🟢 (as statistics) | `backtest.metrics` has win rate and expectancy (`backtest.py:585`) | add `max_consecutive_losses`, `avg_R`, `top_decile_share` to metrics | known trade list → exact counts; empty list → `None`s |
| F-5 | Volume is relative to the symbol's own recent average | ✅ | `volume_ratio` (`indicators.py:105`) excludes the tested bar | — | existing tests |
| F-6 | Healthy uptrend: volume rises on advances, dries up on pullbacks; warning when it reverses | 🟢 | — | `volume_profile.py: trend_volume_balance(series, swings)` | synthetic up-legs at 2× volume, pullbacks at 0.5× → `CONFIRMING`; inverted → `WARNING` |
| F-7 | Upside breakout must have a volume increase; downside need not | ✅ (partial) | engine `BREAKOUT` requires ratio ≥ 1.5 (`engine.py:21`), applied to a 20-bar high, not to a pattern boundary | breakout-quality check (Feature 3), profile switch `volume_required_up` | same breakout with ratio 1.0 → EM `UNCONFIRMED`, BP `CONFIRMED` |
| F-8 | Blow-off: volume above every prior minor top → stop opening new positions in that direction | 🟢 | `UNUSUAL_VOLUME` ≥ 2× (`engine.py:22`) is not this rule | `volume_profile.blow_off(series, swings)` | volume spike exceeding all swing-high volumes in the leg → flag; smaller spike → none |
| F-9 | Ignore volume for forex/futures | 🟢 | — | profile switch | BP profile never reads volume (assert via a series with zero volume) |

### §4 Reversal patterns, traps, failures

| ID | Rule | Status | Existing code | Proposed home | Test criteria |
|---|---|---|---|---|---|
| R-1 | H&S top/bottom geometry | ✅ (geometric) | `_detect_head_and_shoulders` (`patterns.py:366`), thresholds `patterns.py:69-72` (chosen here) | keep; add book filters R-2..R-4 | existing `tests/test_patterns.py` |
| R-2 | Shoulders overlap in price; right shoulder on **lower volume** (top) | 🟢 | not checked | extra conditions in the H&S detector (reported, not gating, behind a version bump `head_and_shoulders/2`) | fixture with non-overlapping shoulders → `shoulders_overlap: false` |
| R-3 | Neckline slope: flat or against the trend is better; rising neckline at a top lowers reliability | 🟢 | neckline points emitted, slope not graded | `neckline_slope_pct` + `neckline_quality` label | three fixtures (flat / rising / falling) → expected labels |
| R-4 | Target = head-to-neckline height from the break, capped by the prior leg (`min(formula, prior move)`) | 🟢 | not computed | `targets.py: measured_move(pattern)` | head 120, neck 100, break at 100 → target 80; prior leg 15 → target 85 |
| R-5 | **H&S failure**: price closes back beyond the right shoulder → independent signal, target = pattern height | 🟢 | detector reports `FAILED` on a close above the right shoulder (`patterns.py:366`), but not as its own signal | new kind `HS_FAILURE` | after a FAILED H&S top, `HS_FAILURE` appears with target = height from the right-shoulder high |
| R-6 | Rectangle (horizontal top/bottom, volume declining) — BP's favorite | 🟡 | **missing** | `_detect_rectangle` | flat 60-bar range with ≥ 2 touches per side → detected; slanted channel → not detected. Needs: touch tolerance, minimum touches (Q3) |
| R-7 | Double/triple top/bottom: ≥ 2 months apart, 15–20% depth, confirmed **only** by a close through the middle low | 🟢 | **missing** | `_detect_double_top_bottom` | tops 60 bars apart, 18% dip, close below the dip → `BREAKDOWN_CONFIRMED`; tops 10 bars apart → not detected; no close through → `FORMING` |
| R-8 | Symmetrical / right-angle triangles; breakout best at 1/2–3/4 of the way to the apex | ✅ (geometry) / 🟢 (apex fraction) | `_detect_triangles` (`patterns.py:502`) computes `apex_bars_after_last_swing` but not the breakout position | add `breakout_apex_fraction`; flag `> 0.75` as `LATE_APEX` | fixture breaking at 0.6 → no flag; at 0.85 → `LATE_APEX` |
| R-9 | **Triangle failure** (break through the sloped side of a right-angle triangle) → trade the opposite way | 🟢 | the detector already resolves this as `FAILED` (`_TRIANGLE_STATES`, `patterns.py:493`) | expose as kind `TRIANGLE_FAILURE` | ascending triangle closing below the rising line → `TRIANGLE_FAILURE` |
| R-10 | Broadening top (higher highs, lower lows, end of bull market; ~9/10 bearish) | 🟡 | missing | `_detect_broadening` | 5 alternating swings with diverging lines → detected. Needs minimum span (Q4) |
| R-11 | Diamond, horn, saucer/rounding | 🔴 | missing | do not detect yet; rare and shape-dependent | — |
| R-12 | Wedges (rising = bearish, falling = bullish), > 3 weeks | ✅ (geometry) | `_detect_triangles` classifies wedges; `TRIANGLE_MIN_BARS = 10` (`patterns.py:82`) < 3 weeks of daily bars | per-profile minimum duration | wedge of 12 daily bars → below EM minimum (21 bars) |
| R-13 | One-day reversal / selling climax (highest volume in months, gap, reversal, close near the prior price) | 🟡 | missing | `events: ONE_DAY_REVERSAL` | Needs: "months" lookback and "near" tolerance (Q5) |
| R-14 | Out-of-line move: 1–2 days outside, then back → do not redraw the boundary | 🟢 | detectors resolve on the first close beyond | track `bars_outside` before a resolution; allow ≤ 2 | 1 close outside then back → still `FORMING` under EM/BP policy |
| R-15 | Premature breakout → recompletion (one retry only) | 🟢 | not modeled | `breakout_state` machine: `PREMATURE` → `RECOMPLETED` (max 1) | close outside, back inside, outside again → `RECOMPLETED`; a third time → ignored |
| R-16 | Bull/bear trap (pierce, close back inside same day, next day fully inside) | 🟢 | `FAILED_BREAKOUT` (`engine.py` events) uses the prior 20-bar high, not a pattern boundary | trap check against pattern boundaries | intrabar pierce + close inside + next bar fully inside → `BEAR_TRAP` |
| R-17 | Fishhook, end-around, interlocking H&S | 🔴 | — | report sequence of pattern states only | — |
| R-18 | Hikkake (failed inside bar) | 🟢 | missing | `events: HIKKAKE` | inside bar, 1–2 bars above it, then a close below its low → `HIKKAKE_SELL` |
| R-19 | Pattern widely discussed in media fails more often | ⛔ | no news data | — | — |

### §5 Continuation patterns and gaps

| ID | Rule | Status | Existing code | Proposed home | Test criteria |
|---|---|---|---|---|---|
| C-1 | Flag/pennant after a near-vertical pole, volume falling, breakout within ≤ 4 weeks | ✅ (partial) | `_detect_flags` (`patterns.py:414`): `FLAG_MAX_BARS = 15` (~2 weeks daily), volume ratio reported not enforced | per-profile `flag_max_bars` (EM/BP ≈ 20–28 daily bars); volume-falling check for EM | 25-bar flag → detected under EM; flag volume rising → EM `UNCONFIRMED` |
| C-2 | Half-mast measuring: next leg ≈ pole | 🟢 | pole height emitted | `targets.measured_move` for flags | pole 10, break at 105 → target 115 |
| C-3 | Flags clustering late in a bull run = warning | 🟡 | — | count flags over N bars | Needs N (Q6) |
| C-4 | Channel completion → move ≈ channel width | 🟡 | no channel detector | `_detect_channel` | Needs parallelism tolerance (Q3) |
| C-5 | Gap classes: common/area, breakaway, runaway, exhaustion, island | 🟢 (breakaway, island) / 🔴 (runaway vs exhaustion) | `CandleSeries.gaps` are **data** gaps, not price gaps | `gaps.py: price_gaps(series)` with class only when determinable: `BREAKAWAY` = gap across a pattern boundary; `ISLAND` = two opposite gaps around ≥ 1 bar | gap from inside to outside a rectangle → `BREAKAWAY`; gap inside the range → `COMMON` |
| C-6 | For a gap breakout, LDR = close before the gap | 🟢 | — | in the LDR function (S-1) | see S-1 |
| C-7 | Crypto 24x7 has few true price gaps (only on venue outages or weekly/hourly opens) | ⛔-ish | — | report gap counts; do not overfit gap rules on crypto | — |

### §6 Support and resistance

| ID | Rule | Status | Existing code | Proposed home | Test criteria |
|---|---|---|---|---|---|
| S/R-1 | Role reversal: broken top → support, broken bottom → resistance | 🟢 | `DEMAND_ZONE` / `SUPPLY_ZONE` (`patterns.py:548`) are impulse zones, not role reversal | `levels.py: role_reversal_levels(swings, closes)` | swing high 100 broken by close 105 → support level 100 listed |
| S/R-2 | Correct support = prior minor **top**, not the latest pullback low | 🟢 | — | same function, label `support_kind` | fixture → support at prior top, not at pullback low |
| S/R-3 | Zone strength: volume traded there, distance traveled after (> 10%), age; third attack usually breaks | 🟡 | — | strength score with reported inputs (no single number) | Needs weights (Q7) — or report raw inputs only (recommended) |
| S/R-4 | Zone center = volume-weighted mean close of the congestion | 🟢 | — | `levels.congestion_center` | three bars (100 @ vol 1, 110 @ vol 3) → 107.5 |
| S/R-5 | Round numbers act as resistance near all-time highs | 🟢 | — | list round numbers within k% of price | close 49,500 → 50,000 listed |
| S/R-6 | **Ice line** after a clean breakout; retest without deep penetration is normal | 🟡 | — | part of the breakout state machine | Needs "deep" (Q2) |
| S/R-7 | Do not buy a free-falling market at support; wait for a reversal day | 🔴 | — | checklist warning only | — |

### §7 Trendlines, channels, moving averages

| ID | Rule | Status | Existing code | Proposed home | Test criteria |
|---|---|---|---|---|---|
| T-1 | Uptrend line through pullback **lows**, needs 2 confirmed swings, never crosses price | 🟢 | `_line` helper (`patterns.py:194`) only inside detectors | `trendlines.py: trendlines(swings, closes)` | line through two swing lows with no close below it in between → valid; a close below → rejected |
| T-2 | Line authority: touches > length > slope | 🟢 (report) | — | report touches, length, slope; no combined score | — |
| T-3 | Valid break: close beyond by ~3% (index 2%); watch for intrabar shakeouts | 🟢 | — | `trendline_break` with profile filter | close 2% below → EM not broken; 4% → broken |
| T-4 | Diagonal boundaries are error-prone; trade only after many tests | 🟢 (as a flag) | — | `boundary_kind: HORIZONTAL / DIAGONAL` on every detection | rectangle → HORIZONTAL; symmetrical triangle → DIAGONAL |
| T-5 | Return line not reached → weakening trend | 🟡 | — | channel module (C-4) | Needs "not reached" tolerance |
| T-6 | Fan rule (third line broken) | 🔴 | — | defer | — |
| T-7 | MA = automatic trendline; price above a rising MA → hold; 50/200 widely watched | ✅ | `trend_state` (`engine.py:108`), SMA set `engine.py:18` | — | existing tests |
| T-8 | Oscillators only auxiliary; Brandt uses none | ✅ | indicators exist (`indicators.py`) | profile switch hides them for `brandt` | BP evaluation output contains no RSI/MACD fields |
| T-9 | Use log scale for trendlines and percentages | 🟢 | lines are linear in price | `log_scale=True` option for trendline fitting | exponential series → straight log line, curved linear line |

### §8 Risk and capital management

| ID | Rule | Status | Existing code | Proposed home | Test criteria |
|---|---|---|---|---|---|
| K-1 | Size = (capital × r) ÷ (\|entry − stop\| × point value) | 🟢 | **missing**; the backtester uses 100% of equity notional (`backtest.py:20`) | `risk.py: position_size(capital, r, entry, stop, point_value, lot_step)` using `Decimal` like `analytics.py` | 100,000 × 0.8%, entry 20, stop 18 → 400; rounds **down** to lot step; entry == stop → error |
| K-2 | Default r 0.5–1%, normalized ~0.7–0.8%, hard cap 2% | 🟢 | — | `RiskPolicy` model (pydantic, like `RiskLimits` in `analytics.py:23`) | r = 2.5% → validation error; r = 0.8% → ok |
| K-3 | Stop too wide → shrink size, **never widen the stop** | 🟢 | — | sizing returns size for the given stop only; no stop adjustment API | no code path changes the stop |
| K-4 | Turtle unit = 1% ÷ (N × point value), N = ATR(20); stop 2N | ✅ (partial) | `turtle_breakout` reports N and 2N "for study only" (`strategies.py:114`) | reuse `position_size` with stop = entry − 2N | N = 2, entry 100 → stop 96 |
| K-5 | Portfolio open risk = Σ (price − stop) × qty; cut starting with losing positions | 🟡 | `analytics.portfolio` has concentration/margin/liquidation warnings, **no stops** | `open_risk(positions, stops)` once positions carry a stop (testnet orders or a user-entered stop) | two longs with known stops → exact sum; missing stop → listed as `unknown`, never assumed |
| K-6 | Correlated group ≤ ~2% total risk | 🟡 | — | user-defined groups (crypto has no published groups) | Needs group definition (Q8) |
| K-7 | Margin used rarely > 15% of equity; capital by risk, not by margin | 🟢 | `max_margin_utilization_pct` default 80% (`analytics.py:23`) | profile preset 15% for BP | — |
| K-8 | Annual drawdown target ~10%, emotional max 15% | 🟢 | `max_drawdown_pct` default 20% (`analytics.py:23`) | profile preset | — |
| K-9 | Turtle drawdown rule: −10% → reduce notional capital 20% | 🟢 | — | backtest sizing mode | equity path crossing −10% → next size uses 80% capital |
| K-10 | Evaluative Index (% of watchlist in primary uptrend) scales total exposure | 🟡 / ⛔ natively | — | over the user's **watchlist** only, labeled `ADAPTED` | 3 of 4 symbols `UP` → 75% |
| K-11 | Never average down; never "double up to recover" | 🟢 (as a check) | — | warning in the order preview when adding to a losing position (testnet) | adding to a position below entry → warning |
| K-12 | Stress test 2–6 SD moves and gap-through stops | 🟢 | gap-through fill exists (`backtest.py:13`) | scenario table per position | — |
| K-13 | Suitability (day trader, balance watcher), money you can afford to lose | 🔴 | — | onboarding text only | — |

### §9 Entry and exit tactics

| ID | Rule | Status | Existing code | Proposed home | Test criteria |
|---|---|---|---|---|---|
| E-1 | Signal classification (major anticipatory / breakout / pyramid, minor, instinct, misc) | 🟡 | — | `classify_signal(pattern, weekly_patterns)`: major = weekly pattern ≥ 10 wk; pyramid = ≤ 4 wk continuation inside an active major trend; minor = daily-only pattern within the minor thresholds; others = `UNCLASSIFIED` (never `INSTINCT`) | weekly 12-week rectangle break → `MAJOR_BREAKOUT`; 3-week flag after it → `MAJOR_PYRAMID`; isolated 3-week flag → `MINOR_SHORT` (BP: avoid) |
| E-2 | BP entry: stop order at the boundary when the pattern completes | 🟡 | backtester enters at next open (`backtest.py:9`) | `fill_model="stop_order"`: fill at `max(open, level)` on the first bar whose high crosses the level (long), worsened by slippage | bar opens 99, high 103, level 101 → fill 101(+slip); opens 102 → fill 102 (gap) |
| E-3 | Breakout must be decisive; do not draw boundaries too tight | 🔴 / 🟡 | — | breakout filter (Feature 3) is the decisive part; "too tight" stays subjective | — |
| E-4 | Bracket: a mature pattern has obvious entries on both sides | 🟢 (report) | detectors emit both levels | report `upper_level`, `lower_level` for every FORMING pattern | — |
| E-5 | Layering: only add when previous layers are in profit; never add to a loser; ≤ 1% total per market | 🟢 | — | backtest pyramid mode + risk cap | add rejected when open layer is red |
| E-6 | EM entry: pullback of 40–50% of the leg, to the basic trendline, or to the old minor top | 🟢 | — | `entry_zones(leg, trendline, supports)` returns the overlapping zone | leg 100→120 → zone 110–112; plus trendline/support levels when present |
| E-7 | EM: no third buy after ~3 steps up | 🟢 | — | count breakouts since the last intermediate correction | third step → `NO_NEW_ENTRY` |
| E-8 | Signal catalogue (EM ch34): exit/open conditions | 🟢 (mostly) | events partially exist (`engine.py:125`) | map each catalogue item to a detector state | one test per mapped item |
| S-1 | **LDR**: stop = low of the breakout bar; prior bar if the breakout bar is mostly outside / gaps; close before the gap for gap breakouts; weekly = Last Week Rule | 🟢 | — | `stops.py: last_day_rule(series, breakout_index, side, level)` | normal break → breakout-bar low; bar opening above the level → prior bar low; gap → prior close; "mostly outside" needs a definition (Q1) |
| S-2 | **Last Hour Rule** when LDR risk > ~0.8% | 🟢 | `1h` data available | not built (see Implementation status) | — |
| S-3 | EM stop % table (price × beta) | ⛔ | — | adaptation: `k × ATR%` with k chosen here, labeled `ADAPTED` | — |
| S-4 | Stops never widen; long stops only move up | 🟢 | backtest stop is fixed (`backtest.py:11`) | basing points only tighten (`stops.basing_points`); the trailing rule only exits | `test_basing_points_only_tighten_and_short_mirrors` |
| M-1 | Deep pullback into the pattern → exit; a close against you on entry day is a bad sign | 🟡 | — | breakout state `DEEP_RETEST` | Needs "deep" (Q2) |
| M-2 | **Retest Failure Rule**: hard retest through the boundary within days → move the stop just beyond the retest swing | 🟡 | — | `retest_failure(series, entry_index, level, window)` | close back through the level on day 3, then recovery → stop moves to the retest low. Needs window and "hard" (Q2) |
| M-3 | Intervening pattern → pyramid and move the whole position's stop to the new LDR | 🟢 (after S-1, C-1) | — | management engine | — |
| M-4 | **Trailing Stop Rule (3-day)**: peak day → setup day (close below the peak-day low) → exit on a break of the setup-day low | 🟢 | — | `stops.trailing_three_day(series, entry_index, side)` | peak bar low 100; next close 99 = setup day with low 97; a later bar trading 96.9 → exit at 97 (or the open if it gaps) |
| M-5 | BP self-critique: no trailing on major breakouts until far toward the target; ≤ 1 stop change per week | 🟡 | — | profile flag `trail_major_after_fraction` | Needs "far" (Q9) |
| M-6 | Weekend Rule (Friday) / no losers over the weekend | ⛔ (24x7) | — | adapted to weekly-bar close, labeled `ADAPTED` | — |
| M-7 | Recompletion: one retry per pattern, new LDR, ~0.5% risk | 🟢 | — | breakout state machine (R-15) + sizing | — |
| M-8 | EM **basing points** (3 bars fully above the low-day high; or new high ≥ 3% above the prior high) | 🟢 | Darvas uses a different 3-day rule (`strategies.py:151`) | `stops.basing_points(series, side, filter_pct)` | low at bar 10, then bars 11–13 fully above bar 10's high → basing point = bar 10 low; a new low before 3 bars → reset |
| X-1 | Target = pattern height from breakout; swing target; P&F count | 🟢 (height, swing) / 🔴 (P&F) | — | `targets.py` | covered by R-4, C-2 |
| X-2 | BP: take profit at target; quick exits for non-major trades | 🟢 | `hold_bars` time exit only (`backtest.py:78`) | backtest `exit_policy="target"` | trade hitting target mid-bar exits at target (or open if gapped) |
| X-3 | EM progressive / hair-trigger stop after a climactic volume bar | 🟡 | — | `progressive_stop` | Needs "especially large volume" (Q5) |
| X-4 | Blow-off: exit at the second exhaustion gap or key reversal | 🔴 | — | warning only | — |

### §10 Warning checklist and §11 OHLCV computations

| ID | Rule | Status | Existing code | Proposed home | Test criteria |
|---|---|---|---|---|---|
| W-1 | Countertrend to the primary trend? | 🟢 | thesis audit flags countertrend higher-timeframe context (`thesis.py`) | checklist item fed by F-2 | — |
| W-2 | Pattern completed? Long enough? Horizontal? Late in the triangle? Retry count? | 🟢 | — | output of Feature 3 | — |
| W-3 | Volume confirmation; thin market | 🟢 / 🟡 | `volume_ratio` | checklist | — |
| W-4 | Bull-market stage 3, media attention, mania | 🔴 / ⛔ | — | not automated | — |
| W-5 | R:R ≥ 2–3 (Brandt ~3:1); ≥ 15% minimum move for stocks (EM) | 🟢 | — | `plan.reward_risk` | target 130, entry 100, stop 90 → 3.0 |
| W-6 | Stop set, size by risk, correlated ≤ 2%, portfolio risk in limits | 🟢 | — | Feature 1 output | — |
| W-7 | Behavioral flags (averaging down, revenge trading, chasing) | 🟡 | trade history exists (`analytics.closed_trades`, `analytics.py:203`) | journal checks on **testnet** trades | re-entry within N bars after a stop with no new signal → flag |
| §11 | ATR(20)/ATR%, MA50/200 slope, volume ÷ 50-bar mean, weekly resample, pattern length in weeks, LDR, trailing stop, basing points, 20/55 highs/lows, S/R clusters | ✅ mostly / 🟢 rest | `atr_wilder`, `sma`, `volume_ratio` (`indicators.py`); `prior_high/low`; weekly bars fetched directly (no resampling needed); Turtle 20/55 (`strategies.py:114`) | the rest is covered by S-1, M-4, M-8, S/R-4 | — |

## 5. Five features to build first

Ranked by value for the owner's goal (a playbook-driven research tool) against effort and risk. Each one is deterministic, has zero LLM cost, and keeps the R8 boundary.

### Feature 1 — Risk and position-size calculator (§8: K-1…K-4, K-7, K-8, W-5, W-6)

- **Why first:** §0 of the playbook ("no plan without a stop and size") and the biggest safety gap: today nothing in the repo sizes by risk, and the backtester uses 100% of equity.
- **Scope:** `risk.py` with `RiskPolicy` (r default 0.8%, cap 2%, per-profile presets) and `position_size(...)` in `Decimal`, rounding down to the venue lot step (already available as `SymbolRules` to the testnet preview). Also `reward_risk(entry, stop, target)`. API: `POST /api/v1/risk/size` (read-only, no exchange call). Optional: show it as an informational check in the **testnet** order preview (`accounts/trading.py:104` `validate_against_rules`), never auto-submitting.
- **Acceptance:** worked example from §8 (100,000 / 0.8% / 20 / 18 → 400); rounding down; zero or negative stop distance → error; r > 2% → validation error; long and short; property test: `size × |entry − stop| ≤ capital × r` for random valid inputs.

### Feature 2 — Stop and trade-management library (§9.4–9.5: S-1, S-2, S-4, M-4, M-8)

- **Why:** these are the most precisely written rules in both books and the main difference between the two profiles. They are pure functions over closed bars.
- **Scope:** `stops.py`: `last_day_rule`, `TrailingStopRule` / `trailing_three_day` and `basing_points` (a Last Hour Rule and a `StopState` helper were planned and dropped). Each returns the level, the bar it came from, and the rule version (like `_emit` in `patterns.py:214`).
- **Acceptance:** one hand-built candle fixture per rule branch (normal, prior-bar, gap, short side); no-look-ahead identity test (result as of bar k equals the result on `up_to(close_k + 1)`); stops never loosen (property test).

### Feature 3 — Breakout-quality and completion check, plus horizontal patterns (§4–§5: R-6, R-7, R-8, R-14, R-15, T-4, C-1, W-2)

- **Why:** the playbook's core claim is "trade only completed, long, horizontal patterns". Today's detectors resolve on the first close beyond a line, with no swing filter, no % filter, no minimum length in weeks, and no rectangle or double top/bottom (the boundaries Brandt prefers).
- **Scope:**
  - Add `_detect_rectangle` and `_detect_double_top_bottom` to `patterns.py`.
  - Add a `quality` block to every detection: `duration_weeks`, `boundary_kind`, `beyond_defining_swing`, `close_beyond_pct`, `volume_ratio_on_break`, `apex_fraction` (triangles), `bars_outside_before_resolution`, `recompletion_count`.
  - Add a profile-driven verdict `COMPLETED / NOT_COMPLETED / UNCONFIRMED_VOLUME / TOO_SHORT / LATE_APEX`.
  - Keep the subjective "one of the 5 best examples this year" out of code. Instead, rank detections by measurable inputs and show them for human judgment.
- **Acceptance:** fixture per verdict; same fixture judged differently by `brandt` vs `edwards_magee` where §12 says they differ (3% filter, volume); detector version bumps (`rule_version` `…/2`) so old reports stay reproducible.

### Feature 4 — The two trader profiles as evaluators (§3 of this spec)

- **Why:** this is the owner's request, and it turns Features 1–3 into perspectives users can select.
- **Scope:**
  - `ProfileConfig` (the switches in §3.3) as data.
  - Upgrade `brandt_classical_patterns` (`strategies.py:271`) to `brandt_classical_patterns/2`: weekly + daily, duration filters, signal classes (E-1), LDR scenario level, a measured target, and reward/risk.
  - Add `edwards_magee` profile + evaluator `edwards_magee_trend_and_patterns/1`: Dow swing trend on `1w` (F-2), pattern completion with the 3% + volume filter, pullback entry zone (E-6), basing-point stop (M-8), and `unevaluated_criteria` listing the beta stop table, ex-dividend and Evaluative Index.
  - Fix the `brandt` published-assets finding.
  - Bump `CATALOG_VERSION`, update the 20-profile invariant (`methodology.py:142`) and `tests/test_methodology.py`.
- **Acceptance:** evaluators never output BUY/SELL (string scan test like existing ones); `edwards_magee` on crypto is always `ADAPTED_UNVALIDATED` and lists unevaluated criteria; a weekly-confirmed fixture yields `MAJOR_BREAKOUT` for `brandt` and a pullback zone for `edwards_magee`; `perspectives.evaluate_perspectives` returns both independently with no combined verdict.

### Feature 5 — Playbook backtest mode: R-based sizing, stop-order entries, rule-based exits (F-4, E-2, X-2, K-9)

- **Why:** the books stress that the edge is small and shows only over hundreds of trades (§3). Without an honest backtest, the profiles are opinions.
- **Scope:** extend `BacktestConfig` (`backtest.py:77`) with `sizing="fixed_risk"` (uses Feature 1), `fill_model="stop_order"` (E-2), `stop_rule` in {`atr`, `ldr`, `basing_points`} and `exit_policy` in {`hold`, `target`, `trailing_three_day`} (Feature 2), and a pattern-event signal source (Feature 3). Metrics gain `avg_R`, `max_consecutive_losses`, and the share of net profit from the top 10% of trades (Brandt's "bottom liners"). Keep the `HYPOTHETICAL` label, costs, funding, gap handling and walk-forward.
- **Acceptance:** existing identity tests unchanged for the default config; new fixtures where each exit rule fires on a known bar; same-bar stop + target → stop first (worse outcome, as today); no-look-ahead: truncating data never changes earlier trades (existing guarantee, extended to new rules).

**Build order:** 1 → 2 → 3 → 5 → 4. Features 1–3 are shared building blocks; the backtest (5) proves them before the profiles (4) present them to users. Each feature is a separate PR with its own tests and journal entry.

**Next after these:** S/R role-reversal levels (S/R-1, S/R-2, S/R-4), swing-based primary trend for both profiles (F-2), gap classification (C-5), and a testnet trade journal with Brandt-style self-review statistics (§15, W-7).

## 6. Open questions for the owner

These block the 🟡 items. Each needs one number or one choice; the recommended default is in brackets.

1. **Q1 — "Mostly outside the pattern"** (LDR prior-bar case): what share of the breakout bar's range must lie beyond the level? [> 50%]
2. **Q2 — "Deep" retest / "hard" retest window:** how far back inside the pattern, and within how many bars? [close more than 1/3 of the pattern height back inside; window 5 daily bars]
3. **Q3 — Horizontal boundary tolerance** for rectangles and channels: max deviation between touches? [≤ 0.5 × ATR(14) or ≤ 2%, whichever is smaller; ≥ 2 touches per side]
4. **Q4 — Minimum span for broadening patterns:** [≥ 20 daily bars]
5. **Q5 — "Highest volume in months" / "especially large volume":** lookback and ratio? [volume ≥ 2.5× the 50-bar mean and the highest of the last 60 bars]
6. **Q6 — Flag clustering warning:** how many flags in how many bars? [≥ 3 flags in 60 daily bars]
7. **Q7 — S/R strength:** combine into one score or report raw inputs only? [raw inputs only]
8. **Q8 — Correlated groups for crypto:** user-defined lists, or a default (for example "BTC-beta majors")? [user-defined; default: every USDT-margined perpetual in one group]
9. **Q9 — "Far toward the target"** before trailing a major breakout: [≥ 2/3 of the distance to the measured target]
10. **Q10 — Plan output:** may evaluators show scenario entry/stop/size levels (labeled "for study"), or only pattern facts? [scenario levels, labeled, never "buy/sell"]
11. **Q11 — Equities:** should the platform add an equity data source so `edwards_magee` can run natively? [not now; keep it `ADAPTED` on crypto]

## 7. Risks

- **Pattern detectors fire on random data** (already stated in the `brandt` profile limitations). Feature 5 must include a shuffled-data or random-walk baseline before any profile result is shown as meaningful.
- **Threshold fishing.** §15 warns that tuning rules to recent results is "a fool's game". Thresholds are fixed per version; changes need a new `rule_version` and are never auto-optimized.
- **Attribution.** Outputs must say "inspired by", list unevaluated criteria and carry `ADAPTED` on crypto for EM. No output may imply Brandt or Edwards & Magee endorse a crypto trade.
- **Scope creep toward execution.** Sizing may prefill a testnet preview only. Live trading stays outside R8.
