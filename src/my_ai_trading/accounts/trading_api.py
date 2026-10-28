"""Testnet trading routes (/api/v1/trading). Separate from market and account routes; stricter rate limit.

Refusals before sending are 409 (with the reason); everything else follows accounts/api.py.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from ..store import LOCAL_USER
from .api import CONN_ID, account_errors
from .trading import CONSENT_TEXT, OrderDraft, TradingRefused, TradingService

COID = r"^mat[0-9a-f]{16}$"


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str = Field(pattern=CONN_ID)
    enabled: bool
    consent: str | None = Field(default=None, max_length=600)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str = Field(pattern=CONN_ID)
    draft: OrderDraft


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    preview_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    confirm_token: str = Field(min_length=20, max_length=64)


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool


def trading_router(trading: TradingService, dependencies: list[Any]) -> APIRouter:
    r = APIRouter(prefix="/api/v1/trading", dependencies=dependencies)

    def refused(fn, *args, **kw):
        try:
            with account_errors():
                return fn(*args, **kw)
        except TradingRefused as exc:
            raise HTTPException(409, str(exc)) from None

    @r.get("/consent")
    def consent() -> dict[str, Any]:
        return {"consent_text": CONSENT_TEXT, "environment": "testnet only",
                "live_orders": "disabled in this app (AGENTS.md §2)"}

    @r.put("/mode")
    def mode(body: ModeRequest) -> dict[str, Any]:
        meta = refused(trading.set_mode, body.connection_id, user=LOCAL_USER, enabled=body.enabled, consent=body.consent)
        return {"connection": meta.model_dump(mode="json")}

    @r.post("/preview")
    def preview(body: PreviewRequest) -> dict[str, Any]:
        p, token = refused(trading.preview, body.connection_id, body.draft, user=LOCAL_USER)
        return {**p.public(), "confirm_token": token}

    @r.post("/orders")
    def submit(body: SubmitRequest) -> dict[str, Any]:
        return refused(trading.submit, body.preview_id, body.confirm_token, user=LOCAL_USER)

    @r.get("/orders")
    def journal(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        rows = trading.journal.journal(LOCAL_USER, limit=limit)
        return {"orders": rows, "needs_reconcile": [x["client_order_id"] for x in trading.needs_reconcile(user=LOCAL_USER)]}

    @r.post("/orders/{coid}/reconcile")
    def reconcile(coid: str = PathParam(pattern=COID)) -> dict[str, Any]:
        return refused(trading.reconcile, coid, user=LOCAL_USER)

    @r.post("/orders/{coid}/resolve")
    def resolve(body: CancelRequest, coid: str = PathParam(pattern=COID)) -> dict[str, Any]:
        """For an order the exchange never acknowledged: the user checked the exchange and stops tracking it."""
        if not body.confirm:
            raise HTTPException(409, "marking an order resolved needs explicit confirmation")
        return refused(trading.resolve, coid, user=LOCAL_USER)

    @r.post("/orders/{coid}/cancel")
    def cancel(body: CancelRequest, coid: str = PathParam(pattern=COID)) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(409, "cancelling an order needs explicit confirmation")
        return refused(trading.cancel, coid, user=LOCAL_USER)

    return r
