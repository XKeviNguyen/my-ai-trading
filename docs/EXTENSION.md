# Chrome Side Panel extension (R5)

A Manifest V3 **Side Panel** client for the local My AI Trading engine (read-only for live accounts; testnet orders only after a per-order confirmation). It shows the
engine's analysis next to a Binance chart; all analysis is computed by the Python engine on
`http://127.0.0.1:8765`. The extension holds no strategy logic. Since R8 it also has an **Accounts** tab:
an exchange API key you type there is sent once to the local engine and is not kept by the extension.
Live exchange connections are read-only. Orders go only to testnet/demo accounts, with Trading Mode on
and after you confirm each order (see [ACCOUNTS_TRADING.md](ACCOUNTS_TRADING.md)).

## Build

Requires Node 22 and npm. The only dependency is the pinned dev dependency `typescript`.

```bash
cd extension
npm ci            # installs typescript from package-lock.json
npm run build     # tsc + scripts/build.mjs → extension/dist/
npm test          # build, then node --test on test/*.test.mjs
```

## Load in Chrome / Chromium

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select `extension/dist`.
4. Click the toolbar icon: the side panel opens.

After rebuilding, press the reload icon on the extension card.

## Connect to the local engine

1. Start the backend: `my-ai-trading serve` (binds to `127.0.0.1:8765` only; add `--no-monitor`
   to skip background alert monitoring).
2. Run `my-ai-trading token`. It prints the **path** of the token file (normally
   `~/.config/my-ai-trading/api_token`), not the token itself.
3. Open that file, copy its contents, and paste them into **Settings → Local API token → Save token**.
   The panel then runs **Test connection** (`/api/v1/health`, then an authenticated call).

The token is stored only in `chrome.storage.local` for this browser profile (never `storage.sync`),
is never shown again or logged, and is sent only as `Authorization: Bearer …` to the fixed base URL
`http://127.0.0.1:8765`. Redirects are refused, so the token cannot be forwarded elsewhere.

## What the panel does

| Tab | Endpoint(s) | Notes |
|---|---|---|
| Instrument bar | — | Symbol and market come from the **active tab URL only** (`/futures/SANDUSDT` → USD-M perpetual, `/trade/SAND_USDT?type=spot` → spot). Otherwise you type a symbol or pick one from the watchlist. Timeframe selector 1h/4h/1d/1w/1M. |
| Analysis | `GET /analysis` | Freshness (`as_of_utc`, per-timeframe bar close, STALE/PROVIDER_ERROR badges), multi-timeframe summary, alignment verdict, and each selected methodology perspective on its own: applicability status, exact mode label, evaluation vs analysis timeframe, checks, unevaluated criteria, reasons, offered routes. No BUY/SELL badge and no vote across methodologies. |
| Copilot | `POST /copilot/ask`, `/copilot/challenge`, `/copilot/vision` | Deterministic facts are shown separately from model interpretation (label, model, tokens, estimated cost) and from `fallback_reason`. Challenge My Thesis shows supporting vs contradicting evidence, assumptions, unknowns, falsification conditions, proposed tests and the assessment note. Screenshot upload is optional and only sends a file you pick (PNG/JPEG/WEBP ≤ 5 MB). |
| Studio | `/strategies/compile`, `/dry-run`, `POST /strategies`, `/approve`, `/deactivate`, `GET /strategies` | Compile prompts to a previewed rule, dry-run on the current symbol, save a draft, then approve with a second click that shows the exact `rule_hash`. |
| Accounts | `/accounts/*`, `/trading/*` | Connections (live read-only or testnet), portfolio, positions, orders, performance. On testnet connections with Trading Mode on: preview, then place after an explicit confirmation within 60 s; check status and cancel. Live connections show a read-only note and no order ticket. Details in [ACCOUNTS_TRADING.md](ACCOUNTS_TRADING.md). |
| Accounts (order journal, performance) | `/trading/orders`, `/trading/orders/{id}/reconcile`, `/cancel`, `/resolve`; `/accounts/connections/{id}/performance` | Each journal order shows the last check's result (a not-listed order says "Exchange did not list this order at the last check — do not place it again"), last checked and last seen times. Cancel order (confirmed) is offered for every order that is not final, including UNKNOWN, NOT_FOUND and acknowledged-but-not-listed ones. "Mark resolved…" appears only for orders the exchange never acknowledged (SENDING, UNKNOWN, NOT_FOUND), behind a confirmation that you have checked the exchange's order history; it only stops tracking and sends nothing to the exchange. Performance offers the markets the connection's exchange lists (including stock), and account symbols follow the engine's account rule (e.g. `BRK.B`). |
| Alerts | `GET /alerts?after_seq=N` | The service worker polls once a minute (and every 30 s while the Alerts tab is open). Newest first; quiet-hours alerts are de-emphasised. Optional desktop notifications (off by default, plain text only; the first poll only sets a baseline). |
| Reports | `POST /reports`, `GET /reports`, `GET /reports/{id}?format=markdown` | Save the current analysis as a frozen report, list saved reports, and download one as Markdown. |
| Usage | `GET /usage` | Monthly tokens and estimated cost vs budgets, per feature/model rows, and the estimate note. |
| Methods | `GET /methodologies`, `PUT /settings` | All 20 profiles with maturity and evidence level. Enable or disable any of them (including all) and choose NATIVE/HYBRID/ADAPTED. |
| Settings | `GET`/`PUT /settings` | Token, connection test, watchlist, default analysis timeframe, quiet hours (UTC), pause alerts, AI data consent, deep-analysis opt-in, monthly token/USD budgets. Saves send the full settings object back with `If-Match` (the version the form was loaded from); if the settings changed elsewhere (another panel, window or the desktop app), the engine answers 412, nothing is saved, and the form reloads the newer settings and says so. |

Errors are explicit: 401 → paste the token; 421 or connection refused → "local engine not
running: run `my-ai-trading serve`"; 429/503 → retry-after; 422 → the validation detail.

## Permissions and why

| Permission | Why |
|---|---|
| `sidePanel` | Show the UI in Chrome's side panel. |
| `storage` | Keep the token, alert cache and small preferences in `chrome.storage.local`. |
| `alarms` | Poll the local alerts feed once a minute from the service worker. |
| `notifications` | Optional desktop notifications for non-quiet alerts (off until you enable them). |
| host `https://www.binance.com/*` | Lets Chrome expose the active tab's **URL** for Binance pages so the symbol can be detected. No script is injected and the page is never read. |
| host `http://127.0.0.1:8765/*` | Talk to the local engine API. |

The extension has no `tabs`, `scripting` or `<all_urls>` permission and no content scripts. It
keeps the default MV3 content security policy, and all code ships in the package (no remote
scripts, CDNs or fonts). The two typefaces, Chakra Petch and Share Tech Mono, are bundled under
`static/fonts/` with their SIL Open Font License files; a test fails the build if the stylesheet ever
references anything but those local font files.

## What it does NOT do

- It does not scrape the page DOM, canvas or chart data, and does not capture screenshots itself.
- It does not trade on live exchange accounts: live connections are read-only. It never places an
  order on its own; a testnet order is sent only after you confirm that exact order.
- It does not store exchange API keys. A key is sent once to the local engine, which keeps it in memory
  or the OS vault for the lifetime you choose.
- It does not talk to any host other than the local engine.
- It does not render API, model or user text as HTML. Everything goes in with `textContent`/DOM
  APIs, and model output is treated as untrusted text.
- It does not combine methodologies into a vote or produce BUY/SELL signals.

## Known limitations

- Only USD-M perpetual and spot URLs are recognized; coin-margined, margin and other pages fall
  back to manual entry.
- Each poll asks for at most 100 alerts after the last one seen; the API returns them oldest first. If more
  than 100 arrive between two polls, the rest come on the next polls, so none are skipped, only delayed.
- Approving a draft from the strategy list shows its `rule_hash`, trigger and description, but not
  the full preview that the list endpoint also returns.
- Strategy revision (`PUT /strategies/{id}`) and monitor status (`GET /monitor/status`) are not
  surfaced in the UI yet.
