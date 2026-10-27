"""The one interface every exchange or broker adapter implements, plus the shared signed HTTP client.

Adding an exchange means one new module that subclasses `ExchangeAdapter` and calls `register()`. Hosts are
fixed per environment inside each adapter; nothing a user types can change where a request goes.
Order methods exist only on adapters that declare a testnet; `TradingService` (not the adapter) enforces
Trading Mode, confirmation and the testnet-only rule.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, ClassVar, Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..credentials import Credential
from ..models import AccountSnapshot, Cashflow, Environment, Fill, KeyPermissions, Order, OrderStatus

USER_AGENT = "MyAITrading/0.3"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ExchangeError(Exception):
    """Safe to show. Never contains credentials or signed query strings."""


class AuthError(ExchangeError):
    """Key rejected, wrong permissions, IP not allowed, or signature/timestamp problem."""


class RateLimited(ExchangeError):
    def __init__(self, message: str, retry_after_s: float):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ExchangeUnavailable(ExchangeError):
    pass


class OutcomeUnknown(ExchangeError):
    """The request may or may not have taken effect (timeout, connection reset after sending). For orders this
    means: reconcile by client order ID; never send again blindly."""


class NotSupported(ExchangeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError as exc:
            raise ExchangeUnavailable("exchange returned a response that is not JSON") from exc


Transport = Callable[[str, str, dict[str, str], bytes | None, float], HttpResponse]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a redirect could carry signed headers to another host


# No redirects, and proxies from the environment are ignored so signed requests go straight to the exchange.
_OPENER = build_opener(_NoRedirect, ProxyHandler({}))


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     timeout_s: float) -> HttpResponse:
    req = Request(url, data=body, method=method, headers={"User-Agent": USER_AGENT, **headers})
    sent = False
    try:
        with _OPENER.open(req, timeout=timeout_s) as resp:
            sent = True
            data = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ExchangeUnavailable("exchange response too large")
            return HttpResponse(resp.status, data, {k.lower(): v for k, v in resp.headers.items()})
    except HTTPError as exc:
        return HttpResponse(exc.code, exc.read(65_536) if exc.fp else b"",
                            {k.lower(): v for k, v in (exc.headers or {}).items()})
    except TimeoutError as exc:
        raise OutcomeUnknown("exchange did not answer in time; the request may or may not have taken effect") from exc
    except (URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if sent or isinstance(reason, TimeoutError):
            raise OutcomeUnknown("connection lost after sending; the request may or may not have taken effect") from exc
        raise ExchangeUnavailable("could not reach the exchange") from exc


@dataclass(frozen=True)
class OrderRequest:
    """A fully specified order, built only by TradingService after preview and confirmation."""
    symbol: str
    market: Literal["spot", "usdm_perpetual", "stock"]
    side: Literal["buy", "sell"]
    type: Literal["market", "limit"]
    quantity: Decimal
    client_order_id: str
    price: Decimal | None = None
    time_in_force: Literal["GTC", "IOC", "FOK"] | None = None
    reduce_only: bool = False


@dataclass(frozen=True)
class SymbolRules:
    """Exchange trading rules used for order previews. None = not reported by the exchange."""
    symbol: str
    market: str
    base_asset: str
    quote_asset: str
    status: str
    price_tick: Decimal | None = None
    quantity_step: Decimal | None = None
    min_quantity: Decimal | None = None
    max_quantity: Decimal | None = None
    min_notional: Decimal | None = None
    order_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExchangeInfo:
    """Public description of an adapter, shown in the Exchange Connections screen."""
    id: str
    name: str
    kind: Literal["crypto_exchange", "broker"]
    environments: tuple[Environment, ...]
    needs_passphrase: bool
    markets: tuple[str, ...]
    key_setup_url: str
    oauth: Literal["blocked_by_provider_approval", "not_offered"] = "blocked_by_provider_approval"
    key_label: str = "API key"  # what the connect form calls each credential field for this provider
    secret_label: str = "API secret"
    notes: tuple[str, ...] = field(default_factory=tuple)


class ExchangeAdapter:
    """Base class. Subclasses set INFO and implement what their exchange supports; the rest raise NotSupported."""
    INFO: ClassVar[ExchangeInfo]

    def __init__(self, environment: Environment, *, transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.time):
        if environment not in self.INFO.environments:
            raise NotSupported(f"{self.INFO.name} has no {environment} environment in this app")
        self.environment, self.transport, self.clock = environment, transport, clock

    # ---- read-only account data ----
    def permissions(self, cred: Credential) -> KeyPermissions:
        raise NotSupported(f"{self.INFO.name}: key permission check is not implemented")

    def snapshot(self, cred: Credential, connection_id: str) -> AccountSnapshot:
        raise NotSupported(f"{self.INFO.name}: account snapshot is not implemented")

    def order_history(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Order]:
        raise NotSupported(f"{self.INFO.name}: order history is not implemented")

    def fills(self, cred: Credential, symbol: str, market: str, *, limit: int = 100) -> list[Fill]:
        raise NotSupported(f"{self.INFO.name}: trade history is not implemented")

    def cashflows(self, cred: Credential, *, limit: int = 100) -> list[Cashflow]:
        raise NotSupported(f"{self.INFO.name}: cash-flow history is not implemented")

    # ---- public rules ----
    def symbol_rules(self, symbol: str, market: str) -> SymbolRules:
        raise NotSupported(f"{self.INFO.name}: symbol rules are not implemented")

    def reference_price(self, symbol: str, market: str) -> Decimal:
        raise NotSupported(f"{self.INFO.name}: reference price is not implemented")

    def fee_rates(self, cred: Credential, symbol: str, market: str) -> tuple[Decimal, Decimal] | None:
        """(maker, taker) as fractions, e.g. 0.001; None when the exchange does not report them."""
        return None

    def leverage(self, cred: Credential, symbol: str, market: str) -> Decimal | None:
        return None

    # ---- testnet-only order management (TradingService enforces the policy) ----
    def place_order(self, cred: Credential, order: OrderRequest) -> Order:
        raise NotSupported(f"{self.INFO.name}: order placement is not implemented")

    def query_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str) -> Order | None:
        raise NotSupported(f"{self.INFO.name}: order lookup is not implemented")

    def cancel_order(self, cred: Credential, symbol: str, market: str, *, client_order_id: str) -> Order:
        raise NotSupported(f"{self.INFO.name}: order cancellation is not implemented")

    # ---- shared helpers ----
    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def check(self, resp: HttpResponse) -> Any:
        """Map HTTP status to typed errors. Exchange messages are short codes, never our secrets."""
        if resp.status in (429, 418):
            retry = resp.headers.get("retry-after", "")
            raise RateLimited(f"{self.INFO.name} rate limit reached", float(retry) if retry.isdigit() else 10.0)
        if resp.status in (401, 403):
            raise AuthError(f"{self.INFO.name} rejected the key ({_short(resp)})")
        if resp.status >= 500 or 300 <= resp.status < 400:  # redirects are never followed, so 3xx is a failure
            raise ExchangeUnavailable(f"{self.INFO.name} is unavailable (HTTP {resp.status})")
        if resp.status >= 400:
            raise ExchangeError(f"{self.INFO.name} refused the request ({_short(resp)})")
        return resp.json()


def _short(resp: HttpResponse) -> str:
    try:
        data = json.loads(resp.body or b"{}")
    except ValueError:
        return f"HTTP {resp.status}"
    if isinstance(data, dict):
        code = data.get("code", data.get("retCode", data.get("sCode", "")))
        msg = str(data.get("msg", data.get("retMsg", data.get("sMsg", ""))))[:160]
        return f"code {code}: {msg}" if code != "" or msg else f"HTTP {resp.status}"
    return f"HTTP {resp.status}"


def plain(d: Decimal) -> str:
    """Decimal as plain text for APIs and display: '60', '0.001' (never '6E+1')."""
    text = format(d.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except ArithmeticError:
        return None
    return d if d.is_finite() else None


STATUS_MAP: dict[str, OrderStatus] = {
    "NEW": OrderStatus.NEW, "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED, "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED, "CANCELLED": OrderStatus.CANCELED, "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED, "EXPIRED_IN_MATCH": OrderStatus.EXPIRED, "PENDING_CANCEL": OrderStatus.PENDING_CANCEL,
}

REGISTRY: dict[str, type[ExchangeAdapter]] = {}


def register(cls: type[ExchangeAdapter]) -> type[ExchangeAdapter]:
    REGISTRY[cls.INFO.id] = cls
    return cls


def adapter_for(exchange: str, environment: Environment, **kw: Any) -> ExchangeAdapter:
    cls = REGISTRY.get(exchange)
    if cls is None:
        raise NotSupported(f"unknown exchange {exchange!r}")
    return cls(environment, **kw)
