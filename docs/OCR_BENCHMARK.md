# Price-axis OCR benchmark

**Status:** synthetic benchmark, measured locally on 2026-10-27 (Ubuntu, 16 cores, Python 3.14,
OpenCV 5.0.0, Tesseract 5.5.0 with the stock `eng` model). It has **not** been run on real exchange
screenshots. Read the [limitations](#what-this-benchmark-does-not-show) before quoting any number.

## Result

Held-out split: 400 images, 2,970 axis labels. "Accuracy" means the label's exact numeric value was
reported, matched one to one. "Precision" means the share of reported values that are true labels.

| Held-out split (seeds 100000-100399) | Baseline (`chartvision/1`) | Final (`chartvision/2`) |
|---|---|---|
| Exact-match accuracy (label recall) | **83.06%** (2,467 / 2,970) | **99.53%** (2,956 / 2,970) |
| Precision of reported labels | 94.12% (2,467 / 2,621) | 99.93% (2,956 / 2,958) |
| Images with every label right and no extras | 231 / 400 | 391 / 400 |
| Latency per image, median / p95 / max (`run_ocr`, one image at a time) | 0.141 / 0.179 / 0.199 s | 0.347 / 0.478 / 0.572 s |

Other splits, same code:

| Split | Baseline accuracy / precision | Final accuracy / precision |
|---|---|---|
| dev (seeds 0-299, 2,270 labels; used for tuning) | 79.65% / 92.20% | 99.52% / 99.87% |
| mono_stress (seeds 200000-200099, 812 labels; monospace fonts with dotted zeros) | 53.82% / 60.28% | **87.68%** / 94.68% |

The held-out number is above 99% on this synthetic data. The mono_stress split is well below it.

The current-price tag is not counted as an axis label. Both versions are scored on the same images.
Only 29 of the 2,970 held-out labels (about 1%) were settled by the scale check
(`scale_fit`, described below). Every other label was the plain majority reading.

### Held-out accuracy by condition (baseline -> final)

| Condition | Values (labels) |
|---|---|
| Theme | Binance dark 90.2 -> 99.1% (815) · Binance light 82.0 -> 99.4% (644) · TradingView dark 82.4 -> 99.8% (796) · TradingView light 76.6 -> 99.9% (715) |
| Font | DejaVu Sans 92.7 -> 99.7 · Liberation Sans 91.3 -> 100 · Liberation Sans Narrow 85.6 -> 99.7 · Noto Sans 92.3 -> 100 · Hershey Simplex 84.1 -> 100 · Duplex 75.9 -> 99.7 · Complex 65.0 -> 99.3 · Triplex 82.7 -> 99.7 · Complex Small 71.3 -> 100 · Plain 84.2 -> 97.1 |
| Digit height (font scale) | 0.35-0.47: 73.0 -> 99.1% (991) · 0.47-0.58: 86.0 -> 99.6% (934) · 0.58-0.70: 90.1 -> 99.9% (1,045) |
| Pixel density | 1x 81.6 -> 99.6% (1,898) · 2x (HiDPI) 85.7 -> 99.4% (1,072) |
| Encoding | PNG 81.9 -> 99.8 · JPEG q60s 81.3 -> 99.8 · q70s 84.5 -> 98.2 · q80s 87.4 -> 99.6 · q90s 80.6 -> 100 |
| Magnitude | ~1e-4 66.9 -> 99.3 · 0.x 82.4 -> 99.8 · 1-99 84.8 -> 99.8 · 1k 91.1 -> 100 · 10k 86.0 -> 99.8 · ~1M 88.3 -> 98.7 |
| Decimals | 0: 87.9 -> 99.2 · 1: 85.1 -> 100 · 2: 91.1 -> 100 · 3: 80.0 -> 99.4 · 4: 86.0 -> 100 · 5: 85.4 -> 100 · 6: 74.5 -> 99.3 · 7: 58.1 -> 99.4 · 8: 68.9 -> 99.1 |
| Thousands separators | without 81.3 -> 99.6% (2,121) · with 87.5 -> 99.3% (849) |
| Blur / noise / anti-aliasing / tick marks | blur 78.0 -> 99.6 · noise 82.4 -> 98.9 · no anti-aliasing 80.9 -> 98.5 · tick marks 86.8 -> 99.2 |

`tools/ocr_benchmark.py` prints the full table, with precision, for any split.

## What changed in `chartvision.py`

Title OCR is unchanged. Price-axis reading was rewritten. `run_ocr` used to run one `--psm 6` pass
over the right 12% of the image. It now does the following:

1. **Find the label column.** Look only at the right 40% of the image. Text pixels are neutral (low
   chroma `max(B,G,R) - min(B,G,R)`) and differ from the median background by at least
   `max(40, 0.5 x the 99.5th percentile)`. Colored pixels and a 2 px margin around them are masked
   out: candles, moving averages, the current-price tag and its white text. Chroma is used instead of
   HSV saturation because navy dark themes have high saturation. Morphological opening removes runs
   longer than 41 px, which are grid lines and borders. Rows come from the vertical projection of the
   connected components. The label column is the largest group of rows that share a left or right
   edge, preferring the group furthest right. Rows are then rebuilt from that column only, and rows of
   the wrong height or on a colored box are dropped.
2. **Cut one image per row.** The crop is made dark-on-light (theme-aware inversion),
   contrast-stretched (1st-99th percentile), upscaled with `INTER_CUBIC` to a fixed digit height, and
   padded. Marks inside a glyph's hole (dotted or slashed zeros) are erased.
3. **Read every row three ways and vote.** The renderings are gray at 28 px, Otsu-binarized at 36 px,
   and gray at 40 px. Each is read with `tesseract --psm 7 --oem 1 --dpi 300 -c
   tessedit_char_whitelist=0123456789.,`. The digit sequence that most renderings agree on wins, and
   ties go to the higher Tesseract confidence. The renderings fail in different ways: binarizing
   helps clean text but ruins blurred thin strokes, and scale changes how glyphs merge.
4. **Parse.** `parse_axis_number` accepts `65,312.50`, `1,234,567`, leading zeros (`0.000123`) and a
   lone decimal comma (`0,0534`). A thousands group never starts with 0. Text that does not parse is
   dropped.
5. **Check the scale (`axis_consistency`).** A linear price axis has evenly spaced labels on whole
   multiples of one step. The line `value = a + b*y` is fitted by exhaustive RANSAC over pairs of
   readings (at most 40 rows). The step is the most common gap per label spacing between neighboring
   inliers. It must occur at least twice, and it must match the slope within 15%, which tolerates a
   label hidden behind the price tag. A reading is consistent only if it is within 0.35 step of the
   line and an exact multiple of the step. The multiple-of-step rule also catches a misread that is
   the same on every label (for example `1,708,000`, `1,658,000`, ...).
6. **Re-decide only the rows that break the scale.** These rows are read three more times. The
   candidates are the row's own readings, those readings with `.` and `,` swapped, and the same
   digits with the axis's usual number of decimals (a lost or extra decimal point). A candidate that
   lands exactly on the scale is used and marked `scale_fit`. Values are never filled in from the
   scale: every reported number is a sequence of digits that Tesseract read on that row.
7. **Report.** `price_axis_labels` holds only consistent readings. Readings that break the scale go
   to `price_axis_outliers` and are not used. `price_axis_step` is the detected step, or `None` when
   the scale could not be verified (fewer than three usable labels or no agreement). In that case
   readings with confidence of at least 60 are reported unverified, as before.

**Effect on safety and cost.** Every security property is kept. Image size is checked before
decoding. Tesseract runs with a fixed argv and no shell, gets the image through stdin, and writes no
files. Each call has the 15 s timeout. The whitelist is validated (digits, `.` and `,` only).
`OMP_THREAD_LIMIT=1` is set for each process. At most `min(8, CPU count)` Tesseract processes run in
parallel. No new axis OCR call starts more than 10 s (`AXIS_DEADLINE_S`) after axis reading begins.
Images are never stored. The cost is about 3 to 4 Tesseract calls per label row instead of one
call per image, which takes the median latency from 0.14 s to 0.35 s.

**API note.** An injected `ocr_runner` is now called as `runner(png, psm, whitelist=None)`. A
whitelist is passed for axis rows. The only in-repo caller (`copilot.py`) uses the default runner.
The fake runner in `tests/test_chartvision.py` was updated. `METHOD_VERSION` is now
`chartvision/2`. `parsed` gains `price_axis_outliers` and `price_axis_step`.

## How the dataset is generated

`tools/ocr_benchmark.py` uses only the standard library, numpy and OpenCV. It draws every image
from its seed, so the same seed always gives the same bytes and the same ground truth. Images live
in memory only. Nothing is downloaded, and nothing is written to disk unless `--dump DIR` is passed.

- **Layout.** Width 640-1,500 px and height 380-900 px (doubled for HiDPI). There is a candlestick
  random walk with wicks, a 7-period moving average, horizontal and vertical gridlines, a time axis
  with `HH:00` labels and an OHLC title line. The separator between plot and axis is present 70% of
  the time and tick marks 40%. Labels are left- or right-aligned with sub-pixel placement, and the
  last candle can touch the axis.
- **Themes.** Approximate TradingView and Binance dark and light palettes. These are not official
  values.
- **Fonts.** Six Hershey faces. Four proportional TrueType fonts found on this machine are drawn by
  a small glyph-outline rasterizer in the script, because OpenCV's `putText` has only Hershey fonts
  and no new dependency was allowed: DejaVu Sans, Liberation Sans, Liberation Sans Narrow and Noto
  Sans. The rasterizer supersamples 4x and has no hinting. The two monospace faces (DejaVu Sans
  Mono, Liberation Mono) appear only in `mono_stress`. If fonts are missing, only Hershey is used.
- **Size.** The font scale is drawn from 0.35-0.70, measured against Hershey Simplex. Every face is
  sized to the digit height Simplex has at that scale: 7.7-15.4 px at 1x, doubled at 2x. Without
  this, Hershey Plain would be about 5 px tall, which no real chart uses.
- **Values.** Six magnitudes: ~1e-4, 0.x, 1-99, 1k, 10k and ~1M. Precision is 0-8 decimals. Thousands
  separators are used for half of the 1k and 10k cases and 70% of the ~1M cases. There are 5-12
  labels, reduced if they would be closer than 2.4 text heights. The step is a nice number (1, 2,
  2.5 or 5 x 10^k) and a multiple of the last shown digit. Labels are whole multiples of the step.
- **Current-price tag.** A colored box on the axis with white price text and sometimes a `mm:ss`
  countdown. A dashed line crosses the plot. Axis labels under the tag are hidden, as on real charts,
  and are not in the ground truth.
- **Degradations.** Anti-aliasing is off 20% of the time. Gaussian blur (sigma 0.3-0.7) is applied
  30% of the time. JPEG quality 60-95 is used 60% of the time. Gaussian noise (sigma 1-5) is added to
  half of the JPEGs. It is not added to PNGs, because noisy lossless PNGs exceed the 5 MB upload limit
  and screenshots do not carry sensor noise.
- **Ground truth.** Each label's exact text, its `Decimal` value and its y position.

**Splits.** `dev` (0-299) was the only split used while developing and tuning. `heldout`
(100000-100399) was run once on the finished code for the reported number, then re-run unchanged to
confirm the number repeats (identical results). `mono_stress` (200000-200099) is reported on its own.
The generator was changed twice during development, to equalize font heights and to keep the step
representable at the chosen precision. Both baseline numbers above were measured on the final
generator.

## Remaining failure modes (held-out)

The 14 misses fall into three groups:

- **Tesseract returns nothing for a legible row in all six renderings.** This was confirmed for
  `88000` (seed 100223) and `0.800` (seed 100031). Five more images miss exactly one label each
  (seeds 100062, 100107, 100155, 100206, 100298), with no wrong value reported. They look like the
  same failure, but they were not inspected one by one. The label is missing, but nothing wrong is
  reported. A different engine or a model fine-tuned on digits would be needed.
- **Hershey Plain at about 8 px with JPEG q70** (seed 100134). Readings disagree on almost every
  punctuation mark (`1708,000`, `1.700.000`, `1,525,080`). No two neighbors agree on a step, so the
  scale cannot be verified, and three values are reported unverified. Two of them are wrong (the
  two extras in the table).
- **A row merged with the price tag's edge** (seed 100055). The label next to the tag is cut badly
  and lost.

On `mono_stress`, dotted zeros at 1x merge into a blob that the English model reads as `8`, `6` or
`9` (`0.000250` -> `8.600250`). Erasing the mark inside the hole helps only when the hole survives,
which means larger sizes. A digits-only Tesseract model or a template matcher for the axis font would
be needed.

## What this benchmark does not show

- **Real screenshots.** It contains none from TradingView, Binance or any other site. Real font
  rendering (hinting, subpixel ClearType or LCD filtering), browser zoom levels, scaling such as
  1.25x or 1.5x, partially clipped top and bottom labels, overlapping indicator labels, alert lines,
  log scales, percent or indexed axes, and negative values in indicator panes are all missing.
  Negative values cannot be read: `-` is not in the whitelist.
- **Fonts.** Only the fonts listed above. Not tested: TradingView's and Binance's own fonts (Trebuchet
  MS, `-apple-system`, BinancePlex), bold weights, condensed UI fonts, and non-Latin digits.
- **Generator bias.** The generator and the reader were written by the same author, so the
  benchmark may share assumptions with the code: right-hand axis, linear scale, labels on whole
  multiples of the step, neutral label colors and colored tags. Charts that break these assumptions
  get no scale check, at best, and wrong column detection, at worst.
- **Downstream use.** Numbers read from a screenshot are still only cross-checked against exchange
  data and never used in place of it (see `cross_check`). This benchmark does not change that.

## Rerun

From the repository root (`tesseract` must be on `PATH`):

```bash
# final number (sequential, for honest latency): about 2.5 minutes
PYTHONPATH=src .venv/bin/python tools/ocr_benchmark.py --split heldout --json /tmp/ocr_heldout.json
# faster accuracy-only runs (latency is then contended), other splits
PYTHONPATH=src .venv/bin/python tools/ocr_benchmark.py --split dev --workers 4
PYTHONPATH=src .venv/bin/python tools/ocr_benchmark.py --split mono_stress --workers 4
# look at the images
PYTHONPATH=src .venv/bin/python tools/ocr_benchmark.py --split heldout --limit 10 --dump /tmp/ocr_imgs
# baseline: the same script against the previous module (commit 206f47c)
git worktree add /tmp/ocr-baseline 206f47c
PYTHONPATH=/tmp/ocr-baseline/src .venv/bin/python tools/ocr_benchmark.py --split heldout
git worktree remove /tmp/ocr-baseline
# tests (the benchmark smoke test is skipped without tesseract)
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_chartvision.py tests/test_ocr_benchmark.py
```
