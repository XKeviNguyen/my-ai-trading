"""Private account routes (/api/v1/accounts). Credentials go in once and are never returned.

Status codes: exchange key problems are 424 (not 401, which means the app's own token is wrong), policy
refusals 403, expired connections 410, unsupported features 501, unknown outcomes 504.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from ..store import LOCAL_USER, Store
from .adapters.base import AuthError, ExchangeError, ExchangeUnavailable, NotSupported, OutcomeUnknown, RateLimited
from .credentials import CredentialError, CredentialExpired, CredentialNotFound, Lifetime, VaultUnavailable, parse_credential
from .models import Environment
from .service import AccountService, PolicyRefused

CONN_ID = r"^[0-9a-f]{16}$"
SYMBOL = r"^[A-Z0-9][A-Z0-9.]{0,29}$"


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    exchange: str = Field(pattern=r"^[a-z0-9_]{2,20}$")
    environment: Environment
    lifetime: Lifetime = Lifetime.SESSION
    label: str = Field(default="", max_length=60)
    api_key: str = Field(min_length=3, max_length=512, pattern=r"^[!-~]+$")  # printable ASCII, no spaces
    api_secret: str = Field(min_length=3, max_length=4096, pattern=r"^[!-~]+$")  # IBKR paper usernames can be short
    passphrase: str | None = Field(default=None, max_length=256, pattern=r"^[!-~]+$")


@contextmanager
def account_errors():
    try:
        yield
    except PolicyRefused as exc:
        raise HTTPException(403, str(exc)) from None
    except CredentialExpired as exc:
        raise HTTPException(410, str(exc)) from None
    except CredentialNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except VaultUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    except CredentialError as exc:
        raise HTTPException(422, str(exc)) from None
    except NotSupported as exc:
        raise HTTPException(501, str(exc)) from None
    except AuthError as exc:
        raise HTTPException(424, str(exc)) from None
    except RateLimited as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": str(int(exc.retry_after_s))}) from None
    except OutcomeUnknown as exc:
        raise HTTPException(504, str(exc)) from None
    except ExchangeUnavailable as exc:
        raise HTTPException(502, str(exc)) from None
    except ExchangeError as exc:
        raise HTTPException(502, str(exc)) from None


def account_router(accounts: AccountService, dependencies: list[Any], store: Store) -> APIRouter:
    r = APIRouter(prefix="/api/v1/accounts", dependencies=dependencies)

    @r.get("/portfolio")
    def portfolio(environment: Environment | None = None) -> dict[str, Any]:
        with account_errors():
            return accounts.portfolio(user=LOCAL_USER, limits=store.settings().risk_limits, environment=environment)

    @r.get("/connections/{cid}/performance")
    def performance(cid: str = PathParam(pattern=CONN_ID), symbol: str = Query(pattern=SYMBOL),
                    market: Literal["spot", "usdm_perpetual", "stock"] = "spot") -> dict[str, Any]:
        with account_errors():
            return accounts.performance(cid, user=LOCAL_USER, symbol=symbol, market=market,
                                        limits=store.settings().risk_limits)

    @r.get("/exchanges")
    def exchanges() -> dict[str, Any]:
        return {"exchanges": AccountService.exchanges(),
                "lifetimes": [str(x) for x in Lifetime], "default_lifetime": str(Lifetime.SESSION)}

    @r.get("/connections")
    def connections() -> dict[str, Any]:
        return {"connections": [m.model_dump(mode="json") for m in accounts.connections(user=LOCAL_USER)]}

    @r.post("/connections", status_code=201)
    def connect(body: ConnectRequest) -> dict[str, Any]:
        with account_errors():
            cred = parse_credential(body.model_dump(include={"api_key", "api_secret", "passphrase"}))
            meta = accounts.connect(user=LOCAL_USER, exchange=body.exchange, environment=body.environment,
                                    credential=cred, lifetime=body.lifetime, label=body.label)
        return {"connection": meta.model_dump(mode="json"),
                "note": "Disconnecting deletes the key from this computer only. To revoke it, delete the key in "
                        "your exchange account's API management page."}

    @r.delete("/connections/{cid}")
    def disconnect(cid: str = PathParam(pattern=CONN_ID)) -> dict[str, Any]:
        with account_errors():
            found = accounts.disconnect(cid, user=LOCAL_USER)
        if not found:
            raise HTTPException(404, "connection not found")
        return {"deleted": cid, "revoked_at_exchange": False,
                "note": "The key was deleted from this computer. It still works at the exchange until you delete it there."}

    @r.get("/connections/{cid}/snapshot")
    def snapshot(cid: str = PathParam(pattern=CONN_ID), fresh: bool = False) -> dict[str, Any]:
        with account_errors():
            snap = accounts.snapshot(cid, user=LOCAL_USER, max_age_s=0.0 if fresh else 15.0)
        return snap.model_dump(mode="json")

    @r.get("/connections/{cid}/orders")
    def orders(cid: str = PathParam(pattern=CONN_ID), symbol: str = Query(pattern=SYMBOL),
               market: Literal["spot", "usdm_perpetual", "stock"] = "spot", limit: int = Query(default=100, ge=1, le=500)):
        with account_errors():
            items = accounts.order_history(cid, user=LOCAL_USER, symbol=symbol, market=market, limit=limit)
        return {"orders": [o.model_dump(mode="json") for o in items]}

    @r.get("/connections/{cid}/fills")
    def fills(cid: str = PathParam(pattern=CONN_ID), symbol: str = Query(pattern=SYMBOL),
              market: Literal["spot", "usdm_perpetual", "stock"] = "spot", limit: int = Query(default=100, ge=1, le=500)):
        with account_errors():
            items = accounts.fills(cid, user=LOCAL_USER, symbol=symbol, market=market, limit=limit)
        return {"fills": [f.model_dump(mode="json") for f in items]}

    @r.get("/connections/{cid}/cashflows")
    def cashflows(cid: str = PathParam(pattern=CONN_ID), limit: int = Query(default=100, ge=1, le=500)):
        with account_errors():
            items = accounts.cashflows(cid, user=LOCAL_USER, limit=limit)
        return {"cashflows": [c.model_dump(mode="json") for c in items],
                "note": "History only. This app never creates deposits, withdrawals or transfers."}

    @r.post("/cache/clear")
    def clear_cache() -> dict[str, Any]:
        accounts.clear_cache()
        return {"cleared": True, "retention": "Account data is cached in memory for up to 15 seconds and never "
                                              "written to disk."}

    return r
