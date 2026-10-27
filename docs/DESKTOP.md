# Native desktop client (PySide6/Qt)

PRD F11 lists a later native client as an option to evaluate. This is a first PySide6 implementation
under `desktop/` (distribution `my-ai-trading-desktop`, command `my-ai-trading-desktop`). Like the Chrome
side panel, it is a thin **client of the local engine API**: all analysis, rule compilation, monitoring
and AI routing stay in the Python engine. The desktop app talks to no host other than the local engine.
Exchange API keys typed into the Accounts tab are passed straight to the local engine and not kept by the
app; orders go only to testnet/demo connections, through the engine's preview-and-confirm flow
([ACCOUNTS_TRADING.md](ACCOUNTS_TRADING.md)).

Install, run and test commands are in [`desktop/README.md`](../desktop/README.md).

## Architecture

| Module | Role |
|---|---|
| `client.py` | Typed stdlib (`urllib`) client. Fixed base URL `http://127.0.0.1:8765`, bearer token, timeouts, response size cap, redirects refused, proxies disabled. HTTP and network failures map to typed exceptions. No Qt imports. |
| `formatting.py` | Pure helpers for numbers, times, labels, validation, alert cursor and notification text. No Qt imports. |
| `worker.py` | `TaskRunner`: runs blocking API calls in a `QThreadPool`; results come back to the GUI thread through a queued signal. The UI never blocks on the network. |
| `widgets.py` | Plain-text building blocks (labels, cards, lists, read-only text boxes, confirmation dialog). |
| `context.py` | Shared state: client, runner, active instrument, token source, settings cache. |
| `views/*.py` | One module per tab. |
| `app.py` | Main window, instrument bar, tabs, tray icon, entry point. |

The port is a constructor argument of `ApiClient`/`MainWindow` **for tests only** (the engine's Host
allowlist is tied to its port); the app itself always uses 8765 and has no option to change host or port.

## Tabs

| Tab | Endpoint(s) | Notes |
|---|---|---|
| Instrument bar | — | Symbol entry (normalized, `^[A-Z0-9]{3,20}$`), market (USD-M perpetual / spot), analysis timeframe (1h/4h/1d/1w/1M). Remembered in QSettings. |
| Analysis | `GET /analysis` | Instrument header (symbol, market, venue, as-of time and age, analysis timeframe, source, STALE/PROVIDER_ERROR counts); per-timeframe status, trend, bar close and age, close, SMA50/200, RSI14, volume ratio, levels, events with invalidation, data notes; alignment verdict; each selected methodology perspective on its own with exact mode label, applicability status, requested mode, evaluation vs analysis timeframe, evaluation state, checks, unevaluated criteria, invalidation, tactical layer, reasons, offered routes (switch mode or open Strategy Studio) and provenance. No BUY/SELL and no vote. |
| Copilot | `POST /copilot/ask`, `/copilot/challenge`, `/copilot/vision` | Deterministic facts are shown separately from labeled model interpretation (model, tokens, estimated cost) and from the fallback reason. Challenge My Thesis shows assessment, supporting and contradicting evidence, assumptions, unknowns, falsification conditions, proposed tests and observations. Screenshot upload sends only a file you pick (PNG/JPEG/WEBP up to 5 MB) and shows `local_analysis` (geometry, OCR status, cross-checks, ambiguities) and optional visual hypotheses. |
| Strategy Studio | `/strategies/compile`, `/strategies/dry-run`, `POST /strategies`, `/approve`, `/deactivate`, `GET /strategies` | Compile prompts to preview, assumptions, unresolved and rejected items; dry-run on the current symbol; save a draft; approve through a confirmation dialog that shows the exact `rule_hash` (Cancel is the default button); deactivate; list with versions and each version's server `preview`. |
| Accounts | `/accounts/exchanges`, `/accounts/connections` (GET, POST, DELETE), `/accounts/connections/{id}/snapshot`, `/accounts/portfolio`, `/accounts/connections/{id}/performance`, `/trading/consent`, `/trading/mode`, `/trading/preview`, `/trading/orders` (POST, GET), `/trading/orders/{id}/reconcile`, `/trading/orders/{id}/cancel`, `/trading/orders/{id}/resolve` | Same sections and wording as the extension's Accounts view. **Connections**: label, Live · read-only or Testnet, `••••` key hint, lifetime and expiry, permissions, IP restriction; Trading Mode switch only on testnet keys that can trade (turning it on shows the engine's exact consent text in a confirmation dialog, Cancel is the default); Disconnect with a confirmation that explains local deletion versus revoking the key at the exchange; connect form (exchange, environment with unavailable ones disabled, lifetime defaulting to this session, label, key/secret/passphrase in password fields, passphrase only where needed); OAuth note as text only. **Portfolio**: total with provenance, risk warnings, connection errors, allocation, exposure, value by exchange, margin, unpriced assets. **Positions** table. **Orders**: per-connection open orders and unavailable sections; for testnet connections with Trading Mode on, an order ticket (symbol defaults to the active instrument) → preview with rule checks, estimates with provenance and warnings → "Place testnet order" with a visible 60 s countdown that disables it at expiry; order journal with Check status, the last check's result (a not-listed order says "Exchange did not list this order at the last check — do not place it again"), last checked and last seen times, Cancel order (confirmed) for every order that is not final (also UNKNOWN, NOT_FOUND and acknowledged-but-not-listed ones), and "Mark resolved…" only for orders the exchange never acknowledged (SENDING, UNKNOWN, NOT_FOUND), behind a confirmation that you have checked the exchange's order history; it only stops tracking and sends nothing to the exchange. Live connections show a read-only note instead. **Performance** from fills for a connection, symbol and market. |
| Alerts | `GET /alerts?after_seq=N` | Polls every 30 s with the `next_after_seq` cursor while the app runs. The first poll only sets a baseline. Optional tray notifications (off by default) for non-quiet alerts only, at most 5 per poll, plain text with markup characters removed. |
| Reports | `POST /reports`, `GET /reports`, `GET /reports/{id}?format=markdown` | Save the current analysis as a report, list saved reports, export Markdown to a file chosen with a save dialog (new files are created with owner-only permissions). |
| Methods | `GET /methodologies`, `PUT /settings` | All 20 profiles; enable or disable any of them (including all); NATIVE/HYBRID/ADAPTED per profile with the exact F13 label; saves the full settings object. |
| Usage | `GET /usage` | Tokens and estimated cost vs budgets, calls with unknown cost, cache hits and cache hit rate, per feature/model table, estimate note. |
| Settings | `GET`/`PUT /settings`, `GET /health` | Token source and connection test, default analysis timeframe, watchlist, pause alerts, quiet hours (UTC), AI data consent and deep-analysis opt-in with explanations, monthly token/USD budgets. Saves fetch the current settings and PUT the full object back with `If-Match` (the version the form was loaded from). If the settings changed elsewhere in between, the engine answers 412: nothing is saved, the form reloads the newer settings and says so, and you apply your change again. |

Keyboard: Ctrl+1 … Ctrl+9 switch tabs; field labels have Alt mnemonics; Tab moves through controls
(multi-line text boxes pass Tab on instead of inserting it). Layouts wrap at small window sizes (minimum
360 × 420).

## Theme

`theme.py` applies the same neon night design as the Chrome side panel (`extension/static/sidepanel.css`):
the same color tokens, one color per kind of information (indigo perspectives, magenta model output, amber
risk and invalidation, cyan events, red data faults, green freshness), and the same bundled fonts
(Chakra Petch for interface text, Share Tech Mono for numbers, both SIL OFL 1.1, licenses in
`my_ai_trading_desktop/fonts/`). Red and green never mean price direction. Every text color meets WCAG AA
(4.5:1) on its background, input borders reach 3:1 (WCAG 1.4.11), and tests check that the tokens and font files
match the extension. When the OS asks for high contrast, the app keeps the system palette and fonts instead
(detection needs PySide6 6.10 or later; without the API the neon theme is used and nothing fails).

## Security notes

- **Token**: read from `~/.config/my-ai-trading/api_token` (respects `XDG_CONFIG_HOME`) on each request, or
  pasted in Settings and kept in memory only for the session. It is never written to QSettings, never
  shown, never logged, and is sent only as `Authorization: Bearer …` to `127.0.0.1:8765`. Error messages
  redact it even if a server echoes it back.
- **No forwarding**: HTTP redirects are refused and `http_proxy`/`HTTPS_PROXY` settings are ignored, so the
  token cannot reach another host.
- **Plain text only**: API, model and user strings are rendered with `Qt.PlainText` labels, read-only
  `QPlainTextEdit` and table items. A test fails the build if the desktop source contains `setHtml`,
  `RichText`/`AutoText`, bare `eval(`/`exec(`, `subprocess`, `shell=True`, `os.system`, `pickle` or
  `__import__`, or creates a `QLabel` outside the plain-text helpers.
- **Exchange keys (Accounts tab)**: API key, secret and passphrase fields use password echo mode with
  sensitive-input hints (no copy, no prediction). The fields are cleared as soon as "Check and connect" is
  pressed, before the engine answers, whether the request succeeds or fails. The values live only in the
  request job and are dropped when it ends; they are never written to QSettings, shown, logged, or put in
  error text (the client also redacts them if an error detail ever echoes them). After connecting, the app
  only shows the engine's `••••` hint. The order confirmation token is kept in memory for its preview only
  and never shown.
- **Errors**: 401 → token hint with an Open Settings button; 421 or connection refused → "run
  `my-ai-trading serve`"; 429/503 → retry-after; 422 → the validation detail; 502 → provider error;
  timeouts are reported separately. Account and trading statuses never show a token hint: 403 → "Not
  allowed" (outside the account policy), 409 → conflict or trading refusal with the reason, 410 → connection
  expired, 424 → the exchange rejected the key, 501 → not supported, 504 → outcome unknown (check the order
  status, do not resend). Unexpected exceptions show a generic message without internal details.

## Tests

`desktop/tests` (run with `QT_QPA_PLATFORM=offscreen`):

- `test_client.py` — token only to the fixed base URL, health without token, error mapping for every
  status, token never in messages, redirects refused (301/302/303/307/308) with a second server proving
  nothing is forwarded, proxy variables ignored, connection refused and timeout mapping, client-side
  validation, multipart shape, Markdown text responses, token file location.
- `test_formatting.py` — pure helpers, exact mode labels, alert cursor/baseline logic, markup-free
  notifications.
- `test_integration.py` — starts the real core API with uvicorn on a free local port and the synthetic
  fake exchange, builds the main window offscreen, and drives analysis, compile → save → approve (and
  cancel), dry-run, ask, challenge, screenshot upload, methods (all off), usage, settings, reports export,
  alert baseline/notification, wrong token and engine-down errors.
- `test_accounts_desktop.py` — starts the real core API with a memory-only credential vault and a simulated
  exchange adapter (no network), then drives the Accounts tab: connect through the form (secret fields
  cleared, secrets absent from every label, field and QSettings), key hint and permissions, passphrase and
  environment rules, portfolio and positions, consent required for Trading Mode, preview → place → journal
  NEW → check status → cancel, countdown expiry, live connection without a ticket, 403 shown as a policy
  refusal, performance, disconnect; plus client error kinds and ID validation.
- `test_source_safety.py` — the static guard described above.

## Known limitations

- Tested only offscreen (`QT_QPA_PLATFORM=offscreen`). Tray notifications depend on the desktop
  environment's StatusNotifier/system tray support; GNOME needs an extension for a tray. Screen capture
  is not implemented: you pick a screenshot file yourself.
- Alerts are polled only while the app is open; the alert cursor is in memory, so each start sets a new
  baseline (no notifications for alerts that arrived while the app was closed; they still appear in the list).
- Strategy revision (`PUT /strategies/{id}`), backtests (`POST /backtest`) and monitor status are not
  surfaced yet.
- PySide6-Essentials is a large dependency (about 240 MB installed for 6.12.0; the full `PySide6` meta-package
  with add-ons and WebEngine came to about 670 MB in total and is not needed).
- PRD F11 asks to *evaluate* PySide6 versus Tauri. This implementation provides a working PySide6
  baseline; memory use and packaging have not been measured yet.
