# R8: exchange accounts and testnet trading

Status: **built and tested offline (M1–M8); not yet run against real exchange testnets.** This page is the design, the status and the resume point for R8. The boundary it implements is in [AGENTS.md](../AGENTS.md) §2; if this page and AGENTS.md disagree, AGENTS.md wins.

## Boundary in one table

| Capability | Live exchange | Testnet / demo |
|---|---|---|
| Public market analysis | Yes, no account needed | Not needed |
| Account sync (balances, positions, orders, fills, P&L, transfer **history**) | Yes, **read-only keys only** | Yes |
| Order preview, place, modify, cancel | **No** (disabled in code) | Yes, Trading Mode on + per-order confirmation |
| Withdrawals, transfers, key administration, automatic trading | No | No |

A live key that has trade, withdrawal or transfer rights is rejected when it is connected, where the exchange reports key permissions. Where it does not, the connection is refused for live use.

## Architecture

```text
Extension / Desktop ── local API (loopback, paired token, origin checks, rate limits)
                          │
        ┌─────────────────┼──────────────────────────┐
   /api/v1/market     /api/v1/accounts          /api/v1/trading
   (public)           (private, read-only)      (testnet only, Trading Mode)
        │                 │                          │
   MarketDataService  AccountService ── PortfolioAnalytics
                          │                          │
                     ExchangeAdapter (one interface) ┘
                     Binance · Bybit · OKX · IBKR (paper)
                          │
                     CredentialStore: memory (session) or OS vault (keyring), with expiry
```

- **One adapter interface.** Each exchange maps its payloads to normalized records. Every figure is labeled `exchange_reported`, `derived` or `estimate`.
- **CredentialStore.** Lifetimes are session only (default), 1 h, 24 h, 7 d, 30 d, or until removed. Persisted secrets go only to the OS vault. SQLite holds metadata without secrets (exchange, label, environment, scopes, expiry). Expiry is checked before every use, and expired entries are purged at startup. The store supports local deletion. Exchange-side revocation, which usually happens in the exchange's own key management, is explained to the user.
- **TradingService.** Every order follows the same path:
  1. **Preview.** Exchange filters, precision, minimum quantity and notional, fees, exposure and collateral impact are checked and shown.
  2. **Confirmation.** A single-use token is bound to the preview hash and expires after 60 s.
  3. **Submission.** The order is sent with a unique client order ID.
  4. **Reconciliation.** If the outcome is unknown, the service queries the order by client order ID. It never resends blindly. See [Reconciliation rules](#reconciliation-rules).
- **Separated endpoints.** Market, account and trading routes are separate. Preview and submit are refused with 409 (and the reason) unless Trading Mode is on and the connection is a testnet. Status checks, cancel and "mark resolved" keep working after Trading Mode is turned off, because they can only lower risk.

## Status

| # | Milestone | State | Evidence |
|---|---|---|---|
| M0 | Policy update | Done | `AGENTS.md` §2, PRD §7–8, `SECURITY.md` |
| M1 | Models, CredentialStore (OS vault, expiry), API hardening | Done | `tests/test_accounts_credentials.py`, `tests/test_accounts_api.py` |
| M2 | Binance adapter (spot + USD-M, live read-only, testnet orders) | Done | `tests/test_adapter_binance.py` (official doc payloads) |
| M3 | TradingService: preview, confirm, journal, reconcile | Done | `tests/test_trading.py` (double submit, unknown outcome, expiry, crash) |
| M4 | Bybit and OKX adapters | Done | `tests/test_adapter_bybit.py`, `tests/test_adapter_okx.py` |
| M5 | Portfolio and trade analytics | Done | `tests/test_accounts_analytics.py` (hand-computed values) |
| M6 | UI: Accounts tab (extension); desktop parity | Done (extension and desktop) | `extension/test/api.test.mjs`, `desktop/tests/test_accounts_desktop.py` |
| M7 | Kline WebSocket streams as monitor wake-up | Done | `tests/test_streams.py` |
| M8 | Interactive Brokers paper via Client Portal Gateway | Done (paper only) | `tests/test_adapter_ibkr.py` |

Every adapter test runs offline against payload shapes from each exchange's official documentation or official SDK. No adapter has been run against a real testnet from this repository yet: that needs the owner's testnet keys, entered in the app.

## How to try it on a testnet

1. Create testnet keys: Binance spot at testnet.binance.vision, Binance USD-M demo at demo.binance.com, Bybit at testnet.bybit.com, OKX demo trading in your OKX account (with a passphrase).
2. `pip install -e '.[api,accounts]'`, then `my-ai-trading serve`.
3. In the extension, open **Accounts → Connections**, pick the exchange and **Testnet / demo**, paste the key, and keep the default *This session only*.
4. Open **Portfolio**, **Positions** and **Orders**. To place a testnet order, turn on Trading Mode for that connection, preview the order, and confirm within 60 seconds.

## Reconciliation rules

An order whose outcome is unknown (timeout, HTTP 5xx, the app stopped while sending) stays in the journal until it is settled.

- **Check status** looks the order up by client order ID. If the exchange lists it, its state comes from the exchange.
- **A "not listed" answer never changes the stored state.** Exchanges delay and purge order listings, so "not listed" does not prove the order was never executed. The engine counts misses (spaced at least 20 s apart; a failed lookup is not a miss) and tells you not to place the order again. It never sets NOT_FOUND on its own (that state appears only in journals from older versions).
- **Exchange answers win, and states only move forward.** An acknowledged order that is later not listed keeps the last state the exchange reported. States move NEW → PARTIALLY_FILLED / PENDING_CANCEL → final (FILLED, CANCELED, REJECTED, EXPIRED); a slower, older answer never moves one back, and a late "not listed" result cannot override a newer confirmation.
- **Cancel works for uncertain orders too** (UNKNOWN or not listed), and stays available after Trading Mode is turned off, because canceling only lowers risk.
- **Mark resolved** (`POST /trading/orders/{id}/resolve`) is only for orders the exchange never acknowledged (SENDING, UNKNOWN or NOT_FOUND). Use it only after you checked the exchange's own order history. The state becomes USER_RESOLVED. It is refused while the order is being sent, and refused in the same SQL statement if the exchange acknowledged it meanwhile. If the exchange later reports the order, that report replaces USER_RESOLVED.
- **If the connection that sent the order is gone** (expired, disconnected or a session key from an earlier run), status checks and cancels use a current connection with the same exchange, environment **and key** (matched by a one-way fingerprint of the API key, not by its last four characters; another key may belong to another account, where the order would look missing). Without one, the order record is kept and the app asks you to connect that key again.
- **Known limits.** An acknowledged order that the exchange no longer lists and no longer cancels (for example after a Binance purge) stays open in the journal; check it at the exchange. Interactive Brokers cancels by the order ID it gave at placement, even in a later gateway session. Turning Trading Mode off stops every send that has not passed its final check; a send already past that check (there is no I/O between the check and the send) still goes out.

## Design decisions worth knowing

- **Live keys are confirmed read-only or refused.** When an exchange cannot report a permission, it counts as "could not confirm" and the live connection is refused.
- **Streams are only a wake-up signal.** Analysis always uses validated closed bars from REST, so a lost or duplicated stream message cannot change a result.
- **Unknown order outcomes are looked up, never resent.** This follows each exchange's documentation of which responses mean "status unknown".
- **Settings saves can be conditional.** `GET /settings` returns an `ETag`. A `PUT /settings` with `If-Match` saves only if the settings have not changed since; otherwise it returns 412, so two open windows cannot silently undo each other (for example, restore a revoked AI consent). A `PUT` without `If-Match` is refused with 428.
- **Money math uses Decimal.** Fees paid in another asset (for example BNB) are counted but never mixed into P&L.
- **Interactive Brokers is paper-only.** IBKR offers no way to confirm that a live session cannot trade. The gateway's self-signed certificate is accepted only for the fixed loopback address.

## Known limitations

- **Per-order confirmation is enforced by the app's UI.** The preview response carries the single-use confirmation token. Any local program that holds the engine's API token (`~/.config/my-ai-trading/api_token`) could therefore preview and confirm an order itself. Keep that file private (it is created with owner-only permissions). Pin your extension with `MY_AI_TRADING_EXTENSION_ID` so other extensions cannot use the private routes.
- **Market-order size limits.** Previews check limit-order size rules. Binance `MARKET_LOT_SIZE` and OKX `maxMktSz` are not checked yet, so the exchange may still reject a large market order. That is a definite rejection, so nothing is sent twice.
- **Non-USD values are not converted.** An account whose equity is reported in another currency (for example an IBKR account in EUR) is listed under "not valued" and left out of the total. A position in another currency is listed as unpriced and left out of USD exposure.
- **Performance works best with full history.** A spot or stock sell with no earlier buy in the loaded fills is counted as unmatched, not as a trade. A futures fill whose realized P&L is more than its matched lots explain closed a position opened before the loaded history: that part is labeled `exchange_reported_entry_unknown` and uses the exchange's P&L. A fee the exchange did not report counts as excluded, not as zero. Positions in currencies other than USD are listed but not added to USD exposure.
- **Known analytics limits.** Stock short sales are reported as unmatched sells, because IBKR fills cover only 7 days and carry no realized P&L, so a short cannot be told apart from closing an older long. A break-even close (realized P&L exactly 0) of a position opened before the loaded history cannot be detected and opens a lot.
- **Hedge (long/short) position mode** is not supported for orders. Bybit and OKX orders assume one-way mode.

## Blocked or not verifiable here

- **One-click OAuth (Binance, Bybit, OKX): BLOCKED BY PROVIDER APPROVAL.** These programs need partner approval and client credentials this project does not have. No OAuth flow is implemented: the exchange catalog marks OAuth as blocked, and the UIs show it as text only.
- **Live testnet runs** need the owner's testnet keys, entered in the app's connection form, never in chat. Automated tests use recorded, documented payloads only.
- **Interactive Brokers** needs the owner to run IB's own gateway application and a paper account.
