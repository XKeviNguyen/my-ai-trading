"""Account connections and read-only account data across exchanges (R8, AGENTS.md §2).

Policy enforced here, at connection time:
- the key must be able to read the account;
- a LIVE key must be confirmed read-only: if it can trade, withdraw or transfer, or the exchange does not say,
  the connection is refused;
- testnet/demo keys may trade (Trading Mode still has to be switched on, see trading.py).
Private account data is cached in memory only, never written to disk, and cleared on disconnect.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .adapters import base
from .adapters.base import ExchangeAdapter, ExchangeError, plain
from .analytics import RiskLimits, closed_trades, portfolio, trade_stats
from .credentials import ConnectionMeta, Credential, CredentialError, CredentialStore, Lifetime
from .models import AccountSnapshot, Cashflow, Environment, Fill, KeyPermissions, Order

SNAPSHOT_TTL_S = 15.0


class PolicyRefused(Exception):
    """The key or request is outside the R8 boundary. Message is safe to show and says how to fix it."""


def check_key_policy(environment: Environment, perms: KeyPermissions) -> None:
    if perms.read is False:
        raise PolicyRefused("this key cannot read the account; enable read access for it at the exchange")
    if environment is Environment.LIVE:
        if perms.can_move_funds is not False:
            raise PolicyRefused("live keys must not allow withdrawals or transfers (or the exchange could not confirm "
                                "they do not). Create a read-only key and connect that instead.")
        if perms.can_trade is not False:
            raise PolicyRefused("live keys must be read-only: trading permission is on (or could not be confirmed off). "
                                "This app trades only on testnets. Create a read-only key and connect that instead.")


class AccountService:
    def __init__(self, credentials: CredentialStore, *,
                 adapter_factory: Callable[[str, Environment], ExchangeAdapter] | None = None,
                 clock: Callable[[], float] = time.time):
        self.credentials = credentials
        self._factory = adapter_factory or (lambda ex, env: base.adapter_for(ex, env))
        self._clock = clock
        self._cache: dict[str, tuple[float, AccountSnapshot]] = {}
        self._cache_epoch = 0
        self._lock = threading.Lock()

    @staticmethod
    def exchanges() -> list[dict[str, Any]]:
        from . import adapters_all  # noqa: F401  (registers every adapter)
        return [{"id": i.id, "name": i.name, "kind": i.kind, "environments": [str(e) for e in i.environments],
                 "needs_passphrase": i.needs_passphrase, "markets": list(i.markets), "key_setup_url": i.key_setup_url,
                 "oauth": i.oauth, "notes": list(i.notes), "key_label": i.key_label, "secret_label": i.secret_label}
                for i in (cls.INFO for cls in base.REGISTRY.values())]

    def adapter(self, exchange: str, environment: Environment) -> ExchangeAdapter:
        from . import adapters_all  # noqa: F401
        return self._factory(exchange, environment)

    def connect(self, *, user: str, exchange: str, environment: Environment, credential: Credential,
                lifetime: Lifetime, label: str) -> ConnectionMeta:
        adapter = self.adapter(exchange, environment)
        if adapter.INFO.needs_passphrase and credential.passphrase is None:
            raise PolicyRefused(f"{adapter.INFO.name} keys need their passphrase")
        self.credentials.require_vault(lifetime)
        perms = adapter.permissions(credential)  # also proves the key works (signed request)
        check_key_policy(environment, perms)
        return self.credentials.save(user=user, exchange=exchange, environment=environment,
                                     label=label or f"{adapter.INFO.name} {environment}", lifetime=lifetime,
                                     credential=credential, permissions=perms)

    def connections(self, *, user: str) -> list[ConnectionMeta]:
        return self.credentials.list(user=user)

    def disconnect(self, connection_id: str, *, user: str) -> bool:
        try:
            return self.credentials.delete(connection_id, user=user)
        finally:
            self.clear_cache(connection_id)  # after the delete, so an overlapping snapshot cannot re-cache (FLOW-10)

    def clear_cache(self, connection_id: str | None = None) -> None:
        with self._lock:
            self._cache_epoch += 1
            if connection_id is None:
                self._cache.clear()
            else:
                self._cache.pop(connection_id, None)

    def open_connection(self, connection_id: str, user: str) -> tuple[ConnectionMeta, Credential, ExchangeAdapter]:
        meta, cred = self.credentials.get(connection_id, user=user)  # expiry enforced on every use
        return meta, cred, self.adapter(meta.exchange, meta.environment)

    def snapshot(self, connection_id: str, *, user: str, max_age_s: float = SNAPSHOT_TTL_S) -> AccountSnapshot:
        with self._lock:
            epoch = self._cache_epoch  # read before the connection is opened: see disconnect()
        meta, cred, adapter = self.open_connection(connection_id, user)  # check expiry even when a cached copy exists
        with self._lock:
            hit = self._cache.get(connection_id)
        if hit and max_age_s > 0 and self._clock() - hit[0] <= max_age_s:  # max_age_s=0 always refetches
            return hit[1]
        snap = adapter.snapshot(cred, meta.connection_id)
        with self._lock:
            if epoch == self._cache_epoch:  # a disconnect during the fetch must not leave private data cached
                self._cache[connection_id] = (self._clock(), snap)
        return snap

    def order_history(self, connection_id: str, *, user: str, symbol: str, market: str, limit: int = 100) -> list[Order]:
        _, cred, adapter = self.open_connection(connection_id, user)
        return adapter.order_history(cred, symbol, market, limit=limit)

    def fills(self, connection_id: str, *, user: str, symbol: str, market: str, limit: int = 100) -> list[Fill]:
        _, cred, adapter = self.open_connection(connection_id, user)
        return adapter.fills(cred, symbol, market, limit=limit)

    def cashflows(self, connection_id: str, *, user: str, limit: int = 100) -> list[Cashflow]:
        _, cred, adapter = self.open_connection(connection_id, user)
        return adapter.cashflows(cred, limit=limit)

    def portfolio(self, *, user: str, limits: RiskLimits | None = None,
                  environment: Environment | None = None) -> dict[str, Any]:
        """One environment at a time: live and testnet money are never added together.

        Default: live if any live connection exists, else testnet. One failing exchange is reported, not fatal."""
        conns = self.connections(user=user)
        available = sorted({str(m.environment) for m in conns})
        env = environment or (Environment.LIVE if "live" in available else Environment.TESTNET)
        snaps, errors = [], []
        for meta in (m for m in conns if m.environment is env):
            try:
                snaps.append(self.snapshot(meta.connection_id, user=user))
            except (ExchangeError, CredentialError, PolicyRefused) as exc:
                errors.append({"connection_id": meta.connection_id, "exchange": meta.exchange, "error": str(exc)})
        prices: dict[tuple[str, str], Any] = {}

        def usd_price(asset: str):
            for snap in snaps:  # public price from an exchange in the SAME environment as the balance
                key = (snap.exchange, asset)
                if key not in prices:
                    try:
                        prices[key] = self.adapter(snap.exchange, env).reference_price(f"{asset}USDT", "spot")
                    except ExchangeError:
                        prices[key] = None
                if prices[key] is not None:
                    return prices[key]
            return None
        out = portfolio(snaps, usd_price, limits)
        out["environment"] = str(env)
        out["available_environments"] = available
        out["connections"] = [{"connection_id": s.connection_id, "exchange": s.exchange,
                               "environment": str(s.environment), "as_of_ms": s.as_of_ms,
                               "unavailable": s.unavailable, "warnings": s.warnings} for s in snaps]
        # What the adapters could not include (e.g. an OKX inverse swap, IBKR asset classes): exposure and
        # positions above are incomplete by exactly these items, even when the reported equity includes them.
        out["completeness_warnings"] = [f"{s.exchange}: {w}" for s in snaps for w in (*s.warnings, *s.unavailable)]
        out["errors"] = errors
        return out

    def performance(self, connection_id: str, *, user: str, symbol: str, market: str,
                    limits: RiskLimits | None = None) -> dict[str, Any]:
        history = closed_trades(self.fills(connection_id, user=user, symbol=symbol, market=market, limit=1000))
        trades = history.trades
        return {"symbol": symbol, "market": market, **trade_stats(trades, limits=limits),
                "unmatched_sells": history.unmatched_sells,
                "closed_trades": [{"side": t.side, "quantity": plain(t.quantity), "pnl": plain(t.pnl), "fees": plain(t.fees),
                                   "closed_ms": t.closed_ms, "provenance": t.provenance} for t in trades[-200:]]}
