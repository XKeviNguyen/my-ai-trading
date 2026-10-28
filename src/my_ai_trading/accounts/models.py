"""Normalized, exchange-neutral account records.

Every amount says where it came from: `exchange_reported` (copied from the exchange), `derived` (computed by
this app from reported fields) or `estimate` (approximation, e.g. a USD value from a public price). Amounts
are Decimal so exchange precision survives; missing values stay None and are never filled with zero.
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Environment(StrEnum):
    LIVE = "live"
    TESTNET = "testnet"  # official testnet or demo-trading environment of the exchange


class Provenance(StrEnum):
    EXCHANGE_REPORTED = "exchange_reported"
    DERIVED = "derived"
    ESTIMATE = "estimate"


class WalletType(StrEnum):
    SPOT = "spot"
    FUTURES_USDM = "futures_usdm"
    FUTURES_COINM = "futures_coinm"
    MARGIN = "margin"
    FUNDING = "funding"
    UNIFIED = "unified"  # one account for several products (e.g. Bybit UTA, OKX trading account)
    SECURITIES = "securities"  # brokers such as Interactive Brokers


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PENDING_CANCEL = "PENDING_CANCEL"
    UNKNOWN = "UNKNOWN"  # sent but not confirmed either way: must be reconciled, never resent blindly


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Balance(_Record):
    asset: str
    wallet: WalletType
    free: Decimal | None = None
    locked: Decimal | None = None
    total: Decimal | None = None
    usd_value: Decimal | None = None
    provenance: Provenance = Provenance.EXCHANGE_REPORTED
    usd_value_provenance: Provenance | None = None


class AccountTotals(_Record):
    """Account-level figures for one wallet, as the exchange reports them (None when not reported)."""
    wallet: WalletType
    equity: Decimal | None = None
    wallet_balance: Decimal | None = None
    available_balance: Decimal | None = None
    initial_margin: Decimal | None = None
    maintenance_margin: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    margin_ratio: Decimal | None = None
    quote_asset: str = "USD"
    provenance: Provenance = Provenance.EXCHANGE_REPORTED


class Position(_Record):
    symbol: str
    market: Literal["spot", "usdm_perpetual", "coinm_perpetual", "futures", "option", "stock"]
    side: Literal["long", "short"]
    quantity: Decimal
    entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    leverage: Decimal | None = None
    margin_mode: Literal["cross", "isolated"] | None = None
    liquidation_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    notional: Decimal | None = None
    currency: str | None = None  # None: the account's USD-like quote; others are not converted (DATA-6)
    provenance: Provenance = Provenance.EXCHANGE_REPORTED


class Order(_Record):
    order_id: str
    client_order_id: str | None = None
    symbol: str
    market: str
    side: Literal["buy", "sell"]
    type: str
    status: OrderStatus
    price: Decimal | None = None
    stop_price: Decimal | None = None
    quantity: Decimal | None = None
    filled_quantity: Decimal | None = None
    average_price: Decimal | None = None
    reduce_only: bool | None = None
    created_ms: int | None = None
    updated_ms: int | None = None
    provenance: Provenance = Provenance.EXCHANGE_REPORTED


class Fill(_Record):
    trade_id: str
    order_id: str
    symbol: str
    market: str
    side: Literal["buy", "sell"]
    price: Decimal
    quantity: Decimal
    fee: Decimal | None = None
    fee_asset: str | None = None
    realized_pnl: Decimal | None = None
    is_maker: bool | None = None
    time_ms: int


class CashflowKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    INTERNAL_TRANSFER = "internal_transfer"
    FUNDING_FEE = "funding_fee"
    COMMISSION = "commission"
    REALIZED_PNL = "realized_pnl"
    OTHER = "other"


class Cashflow(_Record):
    """History only: the app never creates deposits, withdrawals or transfers."""
    kind: CashflowKind
    asset: str
    amount: Decimal
    time_ms: int
    status: str | None = None
    reference: str | None = None
    symbol: str | None = None


class KeyPermissions(_Record):
    """What an API key may do, as reported by the exchange. None means the exchange did not say."""
    read: bool | None = None
    spot_trade: bool | None = None
    derivatives_trade: bool | None = None
    margin_trade: bool | None = None
    withdraw: bool | None = None
    transfer: bool | None = None
    ip_restricted: bool | None = None
    expires_ms: int | None = None
    source: str = "exchange"

    @property
    def can_trade(self) -> bool | None:
        flags = (self.spot_trade, self.derivatives_trade, self.margin_trade)
        if any(f is True for f in flags):
            return True
        return False if all(f is False for f in flags) else None

    @property
    def can_move_funds(self) -> bool | None:
        flags = (self.withdraw, self.transfer)
        if any(f is True for f in flags):
            return True
        return False if all(f is False for f in flags) else None


class AccountSnapshot(_Record):
    connection_id: str
    exchange: str
    environment: Environment
    as_of_ms: int
    totals: list[AccountTotals] = Field(default_factory=list)
    balances: list[Balance] = Field(default_factory=list)
    positions: list[Position] = Field(default_factory=list)
    open_orders: list[Order] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)  # sections the exchange/key/region did not provide
    warnings: list[str] = Field(default_factory=list)
