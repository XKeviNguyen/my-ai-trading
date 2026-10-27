"""R8 M5: portfolio and trade analytics against hand-computed values."""
from decimal import Decimal as D

import pytest

from my_ai_trading.accounts.analytics import RiskLimits, closed_trades, portfolio, trade_stats
from my_ai_trading.accounts.models import AccountSnapshot, AccountTotals, Balance, Environment, Fill, Position, WalletType


def fill(i, side, qty, price, fee="0", fee_asset="USDT", pnl=None, symbol="BTCUSDT", market="spot"):
    return Fill(trade_id=str(i), order_id=str(i), symbol=symbol, market=market, side=side, price=D(price),
                quantity=D(qty), fee=D(fee), fee_asset=fee_asset, realized_pnl=None if pnl is None else D(pnl),
                time_ms=1_000 + i)


def test_fifo_round_trips_with_entry_and_exit_fees_and_spot_never_goes_short():
    fills = [fill(1, "buy", "1", "100", "0.1"), fill(2, "buy", "1", "110", "0.11"),
             fill(3, "sell", "1.5", "120", "0.18"),          # closes lot 1 and half of lot 2
             fill(4, "sell", "1", "100", "0.1"),             # closes the rest of lot 2; 0.5 has no earlier buy
             fill(5, "buy", "0.5", "90", "0.0001", "BNB"),   # opens a new lot (fee in BNB)
             fill(6, "sell", "0.5", "95", "0.05")]           # closes it: entry fee in BNB is excluded, not mixed in
    h = closed_trades(fills)
    trades = h.trades
    # lot1: +20 pnl, fees 1*(0.1+0.12); lot2 half: +5, fees 0.5*(0.11+0.12)  -> 25 - 0.335
    assert [(t.side, t.quantity, t.pnl) for t in trades] == [
        ("long", D("1.5"), D("24.665")),
        ("long", D("0.5"), D("-5.105")),   # -5 - 0.5*(0.11+0.10)
        ("long", D("0.5"), D("2.45")),     # +2.5 - 0.05 exit fee; the BNB entry fee is excluded
    ]
    assert h.unmatched_sells == 1 and trades[2].fees_excluded == 1
    assert all(t.provenance == "derived" for t in trades)
    s = trade_stats(trades)
    assert s["trades"] == 3 and s["net_pnl"] == "22.01"
    assert s["win_rate_pct"] == "66.67" and s["expectancy_per_trade"] == "7.3367"
    assert s["profit_factor"] == "5.3115"  # 27.115 / 5.105 = 5.31146...
    assert s["max_drawdown"] == "5.105" and s["fills_with_fees_in_other_assets"] == 1


def test_a_sell_before_any_buy_in_the_loaded_history_is_unmatched_not_a_short():
    h = closed_trades([fill(1, "sell", "1", "110"), fill(2, "buy", "1", "100")])
    assert h.trades == [] and h.unmatched_sells == 1


def test_zero_quantity_fills_and_lookalike_fee_assets_are_handled():
    h = closed_trades([fill(1, "buy", "0", "100", "0.1"), fill(2, "buy", "1", "100", "0.1", fee_asset="T"),
                       fill(3, "sell", "1", "110", "0.1")])
    assert len(h.trades) == 1 and h.trades[0].fees_excluded == 1  # "T" is not the quote asset of BTCUSDT
    assert h.trades[0].pnl == D("9.9")


def test_futures_fills_use_the_exchange_reported_realized_pnl():
    # Binance reports realizedPnl "0" on opening fills; closing fills carry the realized result.
    fut = {"market": "usdm_perpetual"}
    fills = [fill(1, "buy", "1", "100", "0.05", pnl="0", **fut), fill(2, "sell", "1", "90", "0.05", pnl="-10", **fut),
             fill(3, "sell", "1", "95", "0.05", pnl="0", **fut), fill(4, "buy", "1", "80", "0.05", pnl="15", **fut)]
    trades = closed_trades(fills).trades
    # entry fees (0.05 on each opening fill) are charged when the position closes
    assert [(t.side, t.pnl, t.provenance) for t in trades] == [("long", D("-10.10"), "exchange_reported"),
                                                                ("short", D("14.90"), "exchange_reported")]
    s = trade_stats(trades)
    assert s["profit_factor"] == "1.4752" and s["average_loss"] == "-10.1" and s["fees_paid"] == "0.2"


def test_no_trades_and_no_losses_do_not_divide_by_zero():
    assert trade_stats([])["trades"] == 0
    s = trade_stats(closed_trades([fill(1, "buy", "1", "10"), fill(2, "sell", "1", "12")]).trades)
    assert s["profit_factor"] is None and s["average_loss"] is None and s["win_rate_pct"] == "100.00"


def snap(**kw):
    base = dict(connection_id="c1", exchange="binance", environment=Environment.TESTNET, as_of_ms=1)
    return AccountSnapshot(**{**base, **kw})


def test_portfolio_values_exposure_and_risk_warnings():
    s = snap(totals=[AccountTotals(wallet=WalletType.FUTURES_USDM, equity=D("1000"), maintenance_margin=D("850"))],
             balances=[Balance(asset="BTC", wallet=WalletType.SPOT, free=D("0.1"), locked=D("0")),
                       Balance(asset="USDT", wallet=WalletType.SPOT, free=D("500")),
                       Balance(asset="USDT", wallet=WalletType.FUTURES_USDM, total=D("990")),  # inside equity
                       Balance(asset="XYZ", wallet=WalletType.SPOT, free=D("7"))],
             positions=[Position(symbol="BTCUSDT", market="usdm_perpetual", side="long", quantity=D("0.04"),
                                 mark_price=D("50000"), liquidation_price=D("47000"), notional=D("2000"))])
    prices = {"BTC": D("50000")}
    out = portfolio([s], prices.get)
    assert out["total_value_usd"]["value"] == "6500.00"  # 1000 equity + 5000 BTC + 500 USDT, futures USDT not twice
    assert out["total_value_usd"]["provenance"] == "estimate"
    assert out["unpriced_assets"] == ["binance:XYZ"]
    assert out["exposure_by_direction"] == {"long": "7000.00", "short": "0.00"}
    # futures USDT (inside equity) counts for exposure, not for the total: 7000 BTC of 8490 gross
    assert out["allocation"][0] == {"asset": "BTC", "exposure_usd": "7000.00", "share_pct": "82.45"}
    assert out["positions"][0]["liquidation_distance_pct"] == "6.00"
    w = " | ".join(out["warnings"])
    assert "85.0% of equity" in w and "6.0% from the liquidation price" in w and "BTC is 82.45%" in w


def test_custom_limits_silence_warnings_and_short_positions_net_out():
    s = snap(positions=[Position(symbol="ETHUSDT", market="usdm_perpetual", side="short", quantity=D("1"),
                                 mark_price=D("3000"), notional=D("-3000"))],
             balances=[Balance(asset="ETH", wallet=WalletType.SPOT, free=D("1"))])
    out = portfolio([s], {"ETH": D("3000")}.get, RiskLimits(max_asset_concentration_pct=100))
    assert out["net_exposure_usd"] == "0.00" and out["warnings"] == []
    assert out["allocation"] == []  # long spot ETH and short ETH perpetual cancel out


def test_risk_limits_are_validated():
    with pytest.raises(ValueError):
        RiskLimits(max_margin_utilization_pct=0)


def test_unified_account_coins_count_for_exposure_and_foreign_equity_is_not_added_as_usd():
    s1 = snap(totals=[AccountTotals(wallet=WalletType.UNIFIED, equity=D("10000"))],
              balances=[Balance(asset="BTC", wallet=WalletType.UNIFIED, total=D("0.1"), usd_value=D("6000")),
                        Balance(asset="USDT", wallet=WalletType.UNIFIED, total=D("4000"))])
    out = portfolio([s1], {}.get)
    assert out["total_value_usd"]["value"] == "10000.00"  # reported equity, coins not counted twice
    assert out["allocation"][0] == {"asset": "BTC", "exposure_usd": "6000.00", "share_pct": "60.00"}
    assert out["exposure_by_direction"]["long"] == "6000.00"
    ib = snap(exchange="ibkr", totals=[AccountTotals(wallet=WalletType.SECURITIES, equity=D("5000"), quote_asset="EUR")])
    out = portfolio([ib], {}.get)
    assert out["total_value_usd"]["value"] == "0.00" and out["unpriced_assets"] == ["ibkr:securities equity in EUR"]


def test_a_break_even_futures_exit_is_still_a_closed_trade():
    fut = {"market": "usdm_perpetual"}
    trades = closed_trades([fill(1, "buy", "1", "100", "0.05", pnl="0", **fut),
                            fill(2, "sell", "1", "100", "0.05", pnl="0", **fut)]).trades
    assert [(t.side, t.pnl, t.fees) for t in trades] == [("long", D("-0.10"), D("0.10"))]


def test_futures_shorts_without_reported_pnl_are_kept_and_derived():
    fut = {"market": "usdm_perpetual"}  # e.g. Bybit executions carry no realized PnL
    h = closed_trades([fill(1, "sell", "1", "100", **fut), fill(2, "buy", "1", "90", **fut)])
    assert [(t.side, t.pnl, t.provenance) for t in h.trades] == [("short", D("10"), "derived")]
    assert h.unmatched_sells == 0
