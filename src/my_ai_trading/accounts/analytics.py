"""Portfolio, exposure, risk and trade analytics over normalized account data (R8 M5).

Money math uses Decimal, not floats, so results match exchange figures to the cent. Every output says whether
it is exchange_reported, derived (computed from reported fields) or estimate (e.g. USD values from public
prices). Missing inputs are listed, never filled in.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field

from .adapters.base import plain
from .models import AccountSnapshot, Fill

STABLECOINS = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "DAI", "TUSD", "USD"})
ZERO = Decimal(0)


class RiskLimits(BaseModel):
    """User-defined warning thresholds (percent)."""
    model_config = ConfigDict(extra="forbid")
    max_asset_concentration_pct: float = Field(default=50.0, gt=0, le=100)
    max_margin_utilization_pct: float = Field(default=80.0, gt=0, le=100)
    min_liquidation_distance_pct: float = Field(default=10.0, ge=0, le=100)
    max_drawdown_pct: float = Field(default=20.0, gt=0, le=100)


def fixed2(d: Decimal) -> str:
    """Money and percentages with exactly two decimals, e.g. '6500.00'."""
    return format(d.quantize(Decimal("0.01")), "f")


def _pct(part: Decimal, whole: Decimal) -> str | None:
    return fixed2(part / whole * 100) if whole else None


def portfolio(snapshots: list[AccountSnapshot], usd_price: Callable[[str], Decimal | None],
              limits: RiskLimits | None = None) -> dict[str, Any]:
    """Value, allocation, exposure and risk warnings across connections.

    `usd_price(asset)` returns a public USD price or None. Stablecoins count as 1 USD (an estimate: they can
    de-peg). Futures wallets are taken from the exchange's own equity figure, so their coins are not counted twice.
    """
    limits = limits or RiskLimits()
    by_asset: dict[str, Decimal] = defaultdict(Decimal)
    by_exchange: dict[str, Decimal] = defaultdict(Decimal)
    direction = {"long": ZERO, "short": ZERO}
    by_market: dict[str, Decimal] = defaultdict(Decimal)
    unpriced: list[str] = []
    warnings: list[str] = []
    positions_out: list[dict[str, Any]] = []
    margin_rows: list[dict[str, Any]] = []
    total = ZERO

    for snap in snapshots:
        equity_wallets = {t.wallet for t in snap.totals if t.equity is not None}
        for t in snap.totals:
            if t.equity is None:
                continue
            if t.quote_asset.upper() not in STABLECOINS:  # e.g. an IBKR account in EUR: no FX conversion here
                unpriced.append(f"{snap.exchange}:{t.wallet} equity in {t.quote_asset}")
                continue
            total += t.equity
            by_exchange[snap.exchange] += t.equity
            by_market[str(t.wallet)] += t.equity
            if t.maintenance_margin is not None and t.equity > 0:
                util = t.maintenance_margin / t.equity * 100
                margin_rows.append({"exchange": snap.exchange, "wallet": str(t.wallet),
                                    "maintenance_margin_pct_of_equity": fixed2(util),
                                    "provenance": "derived"})
                if util >= Decimal(str(limits.max_margin_utilization_pct)):
                    warnings.append(f"{snap.exchange} {t.wallet}: maintenance margin is {util:.1f}% of equity "
                                    f"(limit {limits.max_margin_utilization_pct:g}%)")
        for b in snap.balances:
            inside_equity = b.wallet in equity_wallets  # value already in the reported equity: exposure only
            amount = b.total if b.total is not None else ((b.free or ZERO) + (b.locked or ZERO))
            if amount == 0:
                continue
            if b.usd_value is not None:
                value = b.usd_value  # the exchange's own valuation (e.g. Bybit usdValue)
            else:
                price = Decimal(1) if b.asset in STABLECOINS else usd_price(b.asset)
                if price is None:
                    unpriced.append(f"{snap.exchange}:{b.asset}")
                    continue
                value = amount * price
            by_asset[b.asset] += value
            if not inside_equity:
                total += value
                by_exchange[snap.exchange] += value
                by_market[str(b.wallet)] += value
            if b.asset not in STABLECOINS:
                direction["long"] += value
        for p in snap.positions:
            if p.currency and p.currency.upper() not in STABLECOINS:  # no FX conversion here (DATA-6)
                unpriced.append(f"{snap.exchange}:{p.symbol} position in {p.currency}")
                positions_out.append({"exchange": snap.exchange, "symbol": p.symbol, "side": p.side,
                                      "quantity": plain(p.quantity), "notional_usd": None, "currency": p.currency,
                                      "unrealized_pnl": plain(p.unrealized_pnl) if p.unrealized_pnl is not None else None,
                                      "liquidation_distance_pct": None,
                                      "leverage": plain(p.leverage) if p.leverage is not None else None})
                continue
            notional = abs(p.notional) if p.notional is not None else (
                p.quantity * p.mark_price if p.mark_price is not None else None)
            if notional is not None:
                direction[p.side] += notional
                base = p.symbol.removesuffix("USDT").removesuffix("USDC") or p.symbol
                by_asset[base] += notional if p.side == "long" else -notional
            liq_dist = None
            if p.liquidation_price and p.mark_price:
                liq_dist = abs(p.mark_price - p.liquidation_price) / p.mark_price * 100
                if liq_dist <= Decimal(str(limits.min_liquidation_distance_pct)):
                    warnings.append(f"{snap.exchange} {p.symbol} {p.side}: mark price is {liq_dist:.1f}% from the "
                                    f"liquidation price (limit {limits.min_liquidation_distance_pct:g}%)")
            positions_out.append({"exchange": snap.exchange, "symbol": p.symbol, "side": p.side,
                                  "quantity": plain(p.quantity), "notional_usd": plain(notional) if notional is not None else None,
                                  "unrealized_pnl": plain(p.unrealized_pnl) if p.unrealized_pnl is not None else None,
                                  "liquidation_distance_pct": fixed2(liq_dist) if liq_dist is not None else None,
                                  "leverage": plain(p.leverage) if p.leverage is not None else None})

    gross = sum(abs(v) for v in by_asset.values()) or ZERO
    allocation = sorted(({"asset": a, "exposure_usd": fixed2(v), "share_pct": _pct(abs(v), gross)}
                         for a, v in by_asset.items() if v), key=lambda r: -abs(Decimal(r["exposure_usd"])))
    if allocation and gross:
        top = allocation[0]
        if Decimal(top["share_pct"]) >= Decimal(str(limits.max_asset_concentration_pct)) and top["asset"] not in STABLECOINS:
            unpriced_positions = sum(1 for u in unpriced if " position in " in u)
            note = (f"; {unpriced_positions} position(s) in other currencies are not included"
                    if unpriced_positions else "")
            warnings.append(f"{top['asset']} is {top['share_pct']}% of gross exposure "
                            f"(limit {limits.max_asset_concentration_pct:g}%){note}")
    return {
        "total_value_usd": {"value": fixed2(total), "provenance": "estimate",
                            "note": "exchange-reported futures equity plus spot balances valued at public prices; "
                                    "stablecoins counted as 1 USD"},
        "by_exchange": {k: fixed2(v) for k, v in by_exchange.items()},
        "by_wallet": {k: fixed2(v) for k, v in by_market.items()},
        "allocation": allocation,
        "exposure_by_direction": {k: fixed2(v) for k, v in direction.items()},
        "net_exposure_usd": fixed2(direction["long"] - direction["short"]),
        "positions": positions_out,
        "margin": margin_rows,
        "unpriced_assets": unpriced,
        "warnings": warnings,
        "limits": limits.model_dump(),
    }


@dataclass
class ClosedTrade:
    symbol: str
    side: str  # side of the position that was closed
    quantity: Decimal
    pnl: Decimal  # after the fees that could be counted (see fees_excluded)
    fees: Decimal
    closed_ms: int
    provenance: str
    fees_excluded: int = 0  # fills whose fee was paid in another asset (e.g. BNB): not subtracted, only counted


QUOTE_ASSETS = ("FDUSD", "USDT", "USDC", "BUSD", "TUSD", "USD", "EUR", "TRY", "BTC", "ETH", "BNB")


def quote_asset(symbol: str) -> str | None:
    """The quote asset of a symbol such as BTCUSDT (longest known suffix), else None."""
    return next((q for q in QUOTE_ASSETS if symbol.endswith(q) and len(symbol) > len(q)), None)


def _quote_fee(f: Fill) -> Decimal | None:
    """The fee if it was paid in the symbol's quote asset (so it can be subtracted from PnL), else None.

    A fee the exchange did not report is unknown, not zero (DATA-10)."""
    if f.fee is None:
        return None
    if not f.fee:
        return ZERO
    return f.fee if f.fee_asset and f.fee_asset == quote_asset(f.symbol) else None


@dataclass
class TradeHistory:
    trades: list[ClosedTrade]
    unmatched_sells: int = 0  # spot sells with no earlier buy in the loaded fills (history starts later)


# Not "stock": IBKR fills cover 7 days and carry no realized PnL, so a sell closing an older long would become a
# phantom short. A stock short round trip is reported as an unmatched sell instead (re-audit NEW-1).
SHORTABLE_MARKETS = frozenset({"usdm_perpetual", "coinm_perpetual", "futures", "option"})
SPLIT_MARKETS = frozenset({"usdm_perpetual"})  # realized PnL is in the quote asset, like the matched prices
PNL_TOLERANCE = Decimal("0.000001")
FEE_STEP = Decimal("1e-10")  # fee shares of partial fills are rounded here; the remainder stays with the lot


def _share(fee: Decimal, take: Decimal, of: Decimal) -> Decimal:
    """The part of `fee` for `take` units out of `of`: all of it for the last units, so no fee is lost or invented."""
    return fee if take == of else (fee * take / of).quantize(FEE_STEP, rounding=ROUND_HALF_EVEN)


def closed_trades(fills: Iterable[Fill]) -> TradeHistory:
    """Round trips from fills, matched first-in-first-out per symbol and market.

    Whether a fill opens or closes is decided from the position it meets (direction and size), never from the
    realized PnL, so a break-even exit is still a closed trade. For the closed part, the exchange's realized PnL
    is used when the fill reports one (exchange_reported); otherwise it is computed from the matched prices
    (derived). Entry and exit fees paid in the quote asset are subtracted proportionally; fees paid in other
    assets are counted in `fees_excluded`, never mixed in. Spot cannot go short: a spot sell with no earlier buy in
    the loaded fills is counted as unmatched. Derivatives and stocks keep short lots. A closing fill whose
    position opened before the loaded history (no lot to close, non-zero realized PnL) is an
    "exchange_reported_entry_unknown" trade, never a new lot in the other direction."""
    out: list[ClosedTrade] = []
    lots: dict[tuple[str, str], deque[list]] = defaultdict(deque)  # -> [signed qty, price, entry fee left, excluded]
    # The same position at average cost, as exchanges compute realized PnL: [signed qty, average entry price].
    average: dict[tuple[str, str], list] = defaultdict(lambda: [ZERO, ZERO])
    unmatched = 0
    for f in sorted(fills, key=lambda x: (x.time_ms, x.trade_id)):
        if f.quantity <= 0:
            continue  # no quantity, nothing to match (and no division by zero)
        quote_fee = _quote_fee(f)
        own_excluded = int(quote_fee is None)  # this fill's fee was paid in another asset
        excluded = own_excluded
        fill_fee = quote_fee or ZERO
        signed = f.quantity if f.side == "buy" else -f.quantity
        book, avg = lots[(f.symbol, f.market)], average[(f.symbol, f.market)]
        avg_closed = min(abs(signed), abs(avg[0])) if avg[0] and (avg[0] > 0) != (signed > 0) else ZERO
        expected = avg_closed * (f.price - avg[1]) * (1 if avg[0] > 0 else -1)  # what the exchange should realize
        avg[0] += avg_closed if signed > 0 else -avg_closed
        remaining, pnl, closed_qty, fees = signed, ZERO, ZERO, ZERO
        while remaining and book and (book[0][0] > 0) != (remaining > 0):
            lot = book[0]
            take = min(abs(remaining), abs(lot[0]))
            pnl += take * (f.price - lot[1]) * (1 if lot[0] > 0 else -1)
            entry_fee, exit_fee = _share(lot[2], take, abs(lot[0])), _share(fill_fee, take, abs(remaining))
            lot[2] -= entry_fee
            fill_fee -= exit_fee
            fees += entry_fee + exit_fee
            excluded += lot[3]
            closed_qty += take
            lot[0] = lot[0] - take if lot[0] > 0 else lot[0] + take
            remaining = remaining + take if remaining < 0 else remaining - take
            if lot[0] == 0:
                book.popleft()
        # Realized PnL beyond what the known position explains (at average cost, as the exchange computes it) means
        # the rest of the fill closed a position opened before the loaded history (DATA-2). Otherwise the fill
        # flipped the position and the rest opens a new lot.
        extra = ZERO
        if remaining and not book and f.realized_pnl is not None and (not closed_qty or f.market in SPLIT_MARKETS):
            extra = f.realized_pnl - expected
            if abs(extra) < PNL_TOLERANCE:
                extra = ZERO
        if closed_qty:
            reported = f.realized_pnl is not None and not extra
            gross = f.realized_pnl if reported else pnl
            out.append(ClosedTrade(f.symbol, "long" if f.side == "sell" else "short", closed_qty, gross - fees, fees,
                                   f.time_ms, "exchange_reported" if reported else "derived", excluded))
        if extra:
            out.append(ClosedTrade(f.symbol, "long" if f.side == "sell" else "short", abs(remaining),
                                   extra - fill_fee, fill_fee, f.time_ms, "exchange_reported_entry_unknown",
                                   own_excluded))
        elif remaining < 0 and f.market not in SHORTABLE_MARKETS:
            unmatched += 1  # spot and stocks: this sell closes a buy older than the loaded history
        elif remaining:
            book.append([remaining, f.price, fill_fee, own_excluded])
            held = abs(avg[0])
            avg[1] = (held * avg[1] + abs(remaining) * f.price) / (held + abs(remaining)) if held else f.price
            avg[0] += remaining
    return TradeHistory(out, unmatched)


def trade_stats(trades: list[ClosedTrade], *, limits: RiskLimits | None = None) -> dict[str, Any]:
    limits = limits or RiskLimits()
    if not trades:
        return {"trades": 0, "note": "no closed trades in the selected fills"}
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    net = sum((t.pnl for t in trades), ZERO)
    curve, peak, max_dd = ZERO, ZERO, ZERO
    for t in sorted(trades, key=lambda x: x.closed_ms):
        curve += t.pnl
        peak = max(peak, curve)
        max_dd = max(max_dd, peak - curve)
    gross_win, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
    q = Decimal("0.0001")
    stats = {
        "trades": len(trades),
        "win_rate_pct": fixed2(Decimal(len(wins)) / len(trades) * 100),
        "net_pnl": plain(net),
        "average_win": plain((gross_win / len(wins)).quantize(q)) if wins else None,
        "average_loss": plain((-gross_loss / len(losses)).quantize(q)) if losses else None,
        "expectancy_per_trade": plain((net / len(trades)).quantize(q)),
        "profit_factor": plain((gross_win / gross_loss).quantize(q)) if gross_loss else None,
        "max_drawdown": plain(max_dd),
        "fees_paid": plain(sum((t.fees for t in trades), ZERO)),
        "fills_with_fees_in_other_assets": sum(t.fees_excluded for t in trades),
        "provenance": sorted({t.provenance for t in trades}),
        "note": "realized results of past trades; they do not predict future results",
    }
    if peak > 0 and max_dd / peak * 100 >= Decimal(str(limits.max_drawdown_pct)):
        stats["warning"] = f"drawdown reached {max_dd / peak * 100:.1f}% of peak realized profit (limit {limits.max_drawdown_pct:g}%)"
    return stats
