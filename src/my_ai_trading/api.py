"""Local HTTP API for the browser extension and other clients (PRD F11). Binds to 127.0.0.1 only.

Security: Host-header allowlist (blocks DNS rebinding), bearer token on every endpoint except
/health, CORS only for browser-extension origins, rate limit on provider-backed endpoints, bounded
request sizes. Upstream data failures map to 502/503, never to client-error codes.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import re

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .backtest import BacktestBusyError, BacktestConfig, run_backtest, walk_forward
from .accounts.api import account_router
from .accounts.credentials import CredentialStore
from .accounts.service import AccountService
from .accounts.trading import TradingService
from .accounts.trading_api import trading_router
from .compiler import compile_prompts
from .copilot import Copilot, CopilotBusyError, MAX_IMAGE_BYTES, default_provider
from .domain import Timeframe
from .market import DEFAULT_TIMEFRAMES, MarketDataService
from .methodology import PROFILES
from .monitor import Monitor
from .perspectives import evaluate_perspectives
from .providers.binance import BinancePublic, MarketDataError, RateLimitedError
from .reports import to_markdown
from .rules import StrategyRule, evaluate, preview
from .store import LOCAL_USER, SettingsConflict, Store, UserSettings, default_db_path, xdg_dir

EXTENSION_ORIGIN_RE = r"^chrome-extension://[a-p]{32}$"
Market = Literal["usdm_perpetual", "spot"]


def token_path() -> Path:
    return xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config") / "my-ai-trading" / "api_token"


def load_or_create_token(path: Path | None = None) -> str:
    """Read the token without following symlinks; refuse a file owned by someone else (SEC-3)."""
    path = path or token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fd = None
    if fd is not None:
        with os.fdopen(fd) as f:
            st = os.fstat(f.fileno())
            if st.st_uid != os.getuid():
                raise PermissionError(f"{path} is owned by another user")
            if st.st_mode & 0o077:
                os.fchmod(f.fileno(), 0o600)
            tok = f.read().strip()
        if len(tok) >= 32:
            return tok
    tok = secrets.token_urlsafe(32)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    os.replace(tmp, path)
    return tok


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int):
        self.rate, self.burst = rate_per_s, burst
        self.tokens, self.updated = float(burst), time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


MAX_JSON_BYTES = 64 * 1024
SPOOL_BYTES = 1024 * 1024
REPLAY_CHUNK = 64 * 1024
SHUTDOWN_WAIT_S = 5.0
MAX_UPLOAD_BYTES = MAX_IMAGE_BYTES + 64 * 1024
PUBLIC_PATHS = frozenset({"/api/v1/health"})
UPLOAD_PATHS = frozenset({"/api/v1/copilot/vision"})
# Private routes: account data and (testnet) trading. Browser pages may never reach them, even with a token.
PRIVATE_PREFIXES = ("/api/v1/accounts", "/api/v1/trading")
_EXTENSION_ORIGIN = re.compile(EXTENSION_ORIGIN_RE.encode())


def _model_fields() -> frozenset[str]:
    from .accounts.api import ConnectRequest
    from .accounts.trading import OrderDraft
    from .accounts.trading_api import CancelRequest, ModeRequest, PreviewRequest, SubmitRequest
    names = {"body", "query", "path", "header"}
    for m in (ConnectRequest, OrderDraft, ModeRequest, PreviewRequest, SubmitRequest, CancelRequest):
        names.update(m.model_fields)
    return frozenset(names | {"symbol", "market", "limit", "fresh", "environment", "cid", "coid"})


SAFE_LOC_NAMES = _model_fields()


def _settings_fields() -> frozenset[str]:
    """Field names of UserSettings and the models nested in it, for 422 locations."""
    import typing
    names: set[str] = set()
    todo = [UserSettings]
    while todo:
        m = todo.pop()
        for f in m.model_fields.values():
            stack = [f.annotation]
            while stack:
                t = stack.pop()
                if isinstance(t, type) and issubclass(t, BaseModel) and t not in todo:
                    todo.append(t)
                stack.extend(typing.get_args(t))
        names.update(m.model_fields)
    return frozenset(names)


SETTINGS_FIELDS = _settings_fields()


class RequestGuard:
    """Outer ASGI guard that runs BEFORE any body is read or parsed: Host allowlist (DNS rebinding),
    bearer token for every API route except health, and a hard body cap.

    The cap counts the bytes actually received, not only the declared Content-Length, and chunked bodies are
    refused (SEC-1). Bodies are small (64 KiB JSON, one image), so the guard reads them before the app does."""

    def __init__(self, app, *, allowed_hosts: set[str], token: str, extension_id: str | None = None):
        self.app, self.allowed_hosts, self.token = app, allowed_hosts, token.encode()
        # Optional pin: when set, private routes accept only this extension's origin, not any extension.
        self.extension_origin = f"chrome-extension://{extension_id}".encode() if extension_id else None
        self.buckets = {"/api/v1/accounts": TokenBucket(rate_per_s=2.0, burst=30),
                        "/api/v1/trading": TokenBucket(rate_per_s=1.0, burst=20)}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        names = [k.lower() for k, _ in scope.get("headers", [])]
        if any(names.count(n) > 1 for n in _SINGLE_HEADERS):  # the guard and the app must read the same value
            return await _reject(send, 400, "duplicate request header")
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if headers.get(b"host", b"").decode("latin-1") not in self.allowed_hosts:
            return await _reject(send, 421, "host not allowed")
        path, method = scope.get("path", ""), scope.get("method", "GET")
        if method != "OPTIONS" and path.startswith("/api/") and path not in PUBLIC_PATHS:
            auth = headers.get(b"authorization", b"")
            supplied = auth.removeprefix(b"Bearer ").strip() if auth.startswith(b"Bearer ") else b""  # SEC-5
            if not supplied or not hmac.compare_digest(supplied, self.token):
                return await _reject(send, 401, "missing or invalid API token")
        if path.startswith(PRIVATE_PREFIXES):
            origin = headers.get(b"origin")
            allowed = (origin == self.extension_origin) if self.extension_origin else bool(_EXTENSION_ORIGIN.fullmatch(origin or b""))
            if origin is not None and not allowed:
                return await _reject(send, 403, "private routes accept only the extension or local apps")
            prefix = next(p for p in PRIVATE_PREFIXES if path.startswith(p))
            if method != "OPTIONS" and not self.buckets[prefix].take():
                return await _reject(send, 429, "too many private requests; slow down")
        if b"transfer-encoding" in headers:
            return await _reject(send, 411, "chunked bodies are not accepted; send Content-Length")
        if method in ("POST", "PUT", "PATCH"):
            raw = headers.get(b"content-length")
            if raw is None or not raw.isdigit():
                return await _reject(send, 411, "Content-Length required")
            limit = MAX_UPLOAD_BYTES if path in UPLOAD_PATHS else MAX_JSON_BYTES
            if int(raw) > limit:
                return await _reject(send, 413, "request body too large")
            # Spooled: JSON bodies stay in memory, an image upload goes to a temporary file above 1 MiB.
            with tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES) as body:
                size, more = 0, True
                while more:
                    msg = await receive()
                    if msg["type"] != "http.request":
                        return  # the client went away
                    chunk = msg.get("body", b"")
                    size += len(chunk)
                    if size > min(int(raw), limit):
                        return await _reject(send, 413, "request body too large")
                    body.write(chunk)
                    more = msg.get("more_body", False)
                body.seek(0)
                if _json_type(headers) and _invalid_unicode(body.read()):
                    return await _reject(send, 422, "text must be valid Unicode")
                body.seek(0)
                done = False

                async def replay():
                    nonlocal done
                    if done:
                        return await receive()
                    chunk = body.read(REPLAY_CHUNK)
                    done = body.tell() >= size
                    return {"type": "http.request", "body": chunk, "more_body": not done}
                return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)


_SINGLE_HEADERS = (b"host", b"authorization", b"origin", b"content-type", b"content-length", b"transfer-encoding")
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")


def _json_type(headers: dict[bytes, bytes]) -> bool:
    ctype = headers.get(b"content-type", b"").split(b";")[0].strip().lower()
    return ctype == b"application/json" or ctype.endswith(b"+json")


def _invalid_unicode(body: bytes) -> bool:
    """JSON text must be UTF-8, and JSON can still encode a lone surrogate (\\ud800) that no UTF-8 store or
    response can hold (ENV-6). Both are refused before parsing."""
    try:
        body.decode("utf-8")  # strict: also refuses raw surrogate bytes, which json.loads would let through
    except UnicodeDecodeError:
        return True
    if not _SURROGATE_ESCAPE.search(body):
        return False
    try:
        json.dumps(json.loads(body), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return True
    except ValueError:
        return False  # not JSON: the route reports that
    return False


async def _reject(send, status: int, detail: str) -> None:
    body = ('{"detail": "%s"}' % detail).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class CompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompts: list[str] = Field(min_length=1, max_length=10)
    name: str = Field(default="Custom strategy", max_length=80)
    default_timeframe: str | None = None
    use_llm_fallback: bool = False


class RuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule: StrategyRule
    prompts: list[str] = Field(default_factory=list, max_length=10)
    kind: Literal["CUSTOM", "ADAPTED"] = "CUSTOM"
    parent_methodology: str | None = None


class DryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule: StrategyRule
    symbol: str = Field(pattern=r"^[A-Z0-9]{3,20}$")
    market: Market = "usdm_perpetual"
    replay_bars: int = Field(default=200, ge=1, le=1000)


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_hash: str = Field(pattern=r"^[0-9a-f]{16}$")


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]{3,20}$")
    market: Market = "usdm_perpetual"
    question: str = Field(min_length=1, max_length=4000)
    deep: bool = False


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]{3,20}$")
    market: Market = "usdm_perpetual"
    thesis: str = Field(min_length=1, max_length=4000)
    focus_timeframe: str | None = None
    deep: bool = False


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]{3,20}$")
    market: Market = "usdm_perpetual"
    analysis_timeframe: str | None = Field(default=None, max_length=4)


class BacktestConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hold_bars: int = Field(ge=1, le=500)
    direction: Literal["long", "short"] = "long"
    fee_bps: float = Field(default=5.0, ge=0, le=100)
    slippage_bps: float = Field(default=2.0, ge=0, le=100)
    funding_rate_per_8h: float = Field(default=0.0, ge=-0.01, le=0.01)
    stop_atr_mult: float | None = Field(default=None, gt=0, le=20)
    exit_on_rule_false: bool = False


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]{3,20}$")
    market: Market = "usdm_perpetual"
    rule: StrategyRule | None = None
    event_type: str | None = Field(default=None, max_length=40)
    timeframe: str = Field(default="1d", max_length=4)
    bars: int = Field(default=1000, ge=100, le=1499)
    config: BacktestConfigBody
    walk_forward: bool = False
    n_folds: int = Field(default=4, ge=1, le=10)


def create_app(*, service: MarketDataService | None = None, store: Store | None = None,
               copilot: Copilot | None = None, api_token: str | None = None, port: int = 8765,
               run_monitor: bool = False, enable_docs: bool = False, run_streams: bool = False,
               accounts: AccountService | None = None) -> FastAPI:
    service = service or MarketDataService(BinancePublic())
    store = store or Store(default_db_path())
    if accounts is None:
        accounts = AccountService(CredentialStore(store))
    accounts.credentials.purge_on_start()  # session-only and expired keys end with the previous run
    copilot = copilot or Copilot(store, default_provider())
    token = api_token or load_or_create_token()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    provider_bucket = TokenBucket(rate_per_s=2.0, burst=20)
    monitor = Monitor(service, store)
    streams = None
    if run_monitor and run_streams:
        try:
            import websockets  # noqa: F401  (optional: pip install '.[accounts]')
            from .streams import KlineStreams
            streams = KlineStreams(on_closed=monitor.wake)
            monitor.on_subscriptions = streams.set_subscriptions
        except ImportError:
            streams = None  # REST schedule only

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run_forever(stop=stop)) if run_monitor else None
        stream_task = asyncio.create_task(streams.run(stop)) if streams else None
        yield
        stop.set()
        running = {t for t in (task, stream_task) if t}
        if running:  # a provider call can hang: shutdown waits a bounded time, then cancels (FLOW-11)
            _, pending = await asyncio.wait(running, timeout=SHUTDOWN_WAIT_S)
            for t in pending:
                t.cancel()

    # Interactive docs load third-party scripts, so they are opt-in (`serve --docs`).
    app = FastAPI(title="My AI Trading (local)", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if enable_docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if enable_docs else None)
    app.state.monitor = monitor
    extension_id = os.environ.get("MY_AI_TRADING_EXTENSION_ID", "").strip() or None
    if extension_id and not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("MY_AI_TRADING_EXTENSION_ID must be a 32-letter Chrome extension ID (a-p)")
    app.add_middleware(RequestGuard, allowed_hosts=allowed_hosts, token=token, extension_id=extension_id)
    app.add_middleware(CORSMiddleware, allow_origin_regex=EXTENSION_ORIGIN_RE,
                       allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "If-Match"], expose_headers=["ETag"],
                       allow_credentials=False)

    def auth(authorization: str = Header(default="")) -> None:
        supplied = authorization.removeprefix("Bearer ").strip()
        if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "missing or invalid API token")

    def limited() -> None:
        if not provider_bucket.take():
            raise HTTPException(429, "too many requests; slow down", headers={"Retry-After": "1"})

    def upstream(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except RateLimitedError as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": str(int(exc.retry_after_s))}) from exc
        except MarketDataError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def snapshot_for(symbol: str, market: str, timeframes: tuple[str, ...]):
        inst = upstream(service.resolve, symbol, market)
        return inst, upstream(service.snapshot, inst, timeframes)

    def public(snap: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in snap.items() if not k.startswith("_")}

    def tfs(raw: str | None) -> tuple[str, ...]:
        if not raw:
            return tuple(store.settings().timeframes) or DEFAULT_TIMEFRAMES
        codes = tuple(dict.fromkeys(c.strip() for c in raw.split(",") if c.strip()))
        if not 1 <= len(codes) <= 8:
            raise HTTPException(422, "1..8 timeframes")
        for c in codes:
            upstream(Timeframe, c)
        return codes

    A = [Depends(auth)]
    L = [Depends(auth), Depends(limited)]

    @app.exception_handler(BacktestBusyError)
    @app.exception_handler(CopilotBusyError)
    async def busy(_request: Request, exc: Exception) -> JSONResponse:
        # Bounded concurrency (PERF-1, PERF-2): fail fast instead of queueing work behind a stalled API.
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "2"})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI echoes submitted values by default; a mistyped API secret must never come back in a response.
        private = _request.url.path.startswith(PRIVATE_PREFIXES)

        def loc(parts):  # a JSON key could itself be a pasted secret: keep only known field names and indexes
            return [p if isinstance(p, int) or not private or p in SAFE_LOC_NAMES else "<field>" for p in parts]
        safe = [{"type": e.get("type"), "loc": loc(e.get("loc", ())), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse({"detail": safe}, status_code=422)

    app.state.accounts = accounts
    app.state.trading = TradingService(accounts, store)
    app.include_router(account_router(accounts, A, store))
    app.include_router(trading_router(app.state.trading, A))

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "read_only": True}

    @app.get("/api/v1/methodologies", dependencies=A)
    def methodologies() -> dict[str, Any]:
        return {"profiles": [p.as_dict() for p in PROFILES], "count": len(PROFILES),
                "note": "Research profiles inspired by public methodologies; not reproductions or endorsements."}

    @app.get("/api/v1/snapshot", dependencies=L)
    def snapshot(symbol: str = Query(pattern=r"^[A-Z0-9]{3,20}$"), market: Market = "usdm_perpetual",
                 timeframes: str | None = Query(default=None, max_length=64)) -> dict[str, Any]:
        _, snap = snapshot_for(symbol, market, tfs(timeframes))
        return public(snap)

    def build_analysis(symbol: str, market: str, analysis_timeframe: str | None,
                       timeframes: str | None) -> tuple[Any, dict[str, Any]]:
        settings = store.settings()
        atf = analysis_timeframe or settings.analysis_timeframe
        upstream(Timeframe, atf)
        codes = tuple(dict.fromkeys((*tfs(timeframes), atf)))
        inst, snap = snapshot_for(symbol, market, codes)
        series_cache = dict(snap["_series"])

        def get(tf: str):
            if tf not in series_cache:
                series_cache[tf] = upstream(service.series, inst, tf)
            return series_cache[tf]
        selections = [s.model_dump() for s in settings.selected_methodologies]
        persp = upstream(evaluate_perspectives, inst, atf, selections, get, as_of_ms=snap["as_of_ms"])
        return inst, {"snapshot": public(snap), "perspectives": persp, "selected_count": len(selections)}

    @app.get("/api/v1/analysis", dependencies=L)
    def analysis(symbol: str = Query(pattern=r"^[A-Z0-9]{3,20}$"), market: Market = "usdm_perpetual",
                 analysis_timeframe: str | None = Query(default=None, max_length=4),
                 timeframes: str | None = Query(default=None, max_length=64)) -> dict[str, Any]:
        return build_analysis(symbol, market, analysis_timeframe, timeframes)[1]

    @app.post("/api/v1/reports", dependencies=L)
    def save_report(body: ReportRequest) -> dict[str, Any]:
        inst, result = build_analysis(body.symbol, body.market, body.analysis_timeframe, None)
        title = f"{inst.symbol} {inst.market_type} analysis {result['snapshot']['as_of_utc']}"
        return {"id": store.save_report(result, title=title, instrument_key=inst.key), "title": title}

    @app.get("/api/v1/reports", dependencies=A)
    def list_reports() -> dict[str, Any]:
        return {"reports": store.reports()}

    @app.get("/api/v1/reports/{rid}", dependencies=A)
    def get_report(rid: str = PathParam(pattern=r"^[0-9a-f]{12}$"), format: Literal["json", "markdown"] = "json"):
        saved = store.report(rid)
        if saved is None:
            raise HTTPException(404, "report not found")
        if format == "markdown":
            return PlainTextResponse(to_markdown(saved), media_type="text/markdown; charset=utf-8",
                                     headers={"Content-Disposition": f'attachment; filename="report-{rid}.md"'})
        return saved

    @app.get("/api/v1/settings", dependencies=A)
    def get_settings(response: Response) -> dict[str, Any]:
        response.headers["ETag"] = store.settings_etag()
        return store.settings().model_dump()

    @app.put("/api/v1/settings", dependencies=A)
    def put_settings(body: dict[str, Any], response: Response,
                     if_match: str | None = Header(default=None)) -> dict[str, Any]:
        if if_match is None:  # every save is compare-and-set, so a stale tab can never overwrite newer settings
            raise HTTPException(428, "settings saves need If-Match with the ETag from GET /api/v1/settings")
        try:
            s = UserSettings.model_validate(body)
        except ValidationError as exc:  # never echo submitted values or unknown key names (audit SEC-6)
            raise HTTPException(422, [{"type": e.get("type"),
                                       "loc": [p if isinstance(p, int) or p in SETTINGS_FIELDS else "<field>"
                                               for p in e.get("loc", ())],
                                       "msg": "invalid value" if e.get("type") == "value_error" else e.get("msg")}
                                      for e in exc.errors()]) from None
        except ValueError:
            raise HTTPException(422, "invalid settings") from None
        try:
            response.headers["ETag"] = store.save_settings(s, if_match=if_match)
        except SettingsConflict as exc:
            raise HTTPException(412, str(exc)) from None
        return s.model_dump()

    @app.post("/api/v1/strategies/compile", dependencies=L)
    def compile_strategy(body: CompileRequest) -> dict[str, Any]:
        if body.default_timeframe:
            upstream(Timeframe, body.default_timeframe)
        proposal = compile_prompts(body.prompts, name=body.name or "Custom strategy",
                                   default_timeframe=body.default_timeframe)
        out = proposal.as_dict()
        if proposal.status == "UNRESOLVED" and body.use_llm_fallback:
            out["llm_proposal"] = copilot.propose_rule(body.prompts)
        return out

    @app.post("/api/v1/strategies/dry-run", dependencies=L)
    def dry_run(body: DryRunRequest) -> dict[str, Any]:
        inst = upstream(service.resolve, body.symbol, body.market)
        cache = {tf: upstream(service.series, inst, tf) for tf in body.rule.timeframes()}
        trig = cache[body.rule.trigger_timeframe]
        now = service.clock.now_ms()
        current = evaluate(body.rule, cache.__getitem__, as_of_ms=now)
        matches, counts = [], {"MATCH": 0, "NO_MATCH": 0, "INSUFFICIENT_DATA": 0}
        for c in trig.candles[-body.replay_bars:]:
            r = evaluate(body.rule, cache.__getitem__, as_of_ms=c.close_time_ms + 1)
            counts[r["result"]] += 1
            if r["result"] == "MATCH":
                matches.append(r["bar_close_times"].get(body.rule.trigger_timeframe))
        return {"preview": preview(body.rule), "current": current,
                "replay": {"bars": min(body.replay_bars, len(trig.candles)), "counts": counts,
                           "recent_matches": matches[-20:],
                           "note": "Historical rule matches on closed bars; not performance. Use the backtester "
                                   "with costs for outcomes."}}

    @app.get("/api/v1/strategies", dependencies=A)
    def list_strategies() -> dict[str, Any]:
        out = store.strategies()
        for strat in out:  # approve-from-list must show exactly what is being approved
            for v in strat["versions"]:
                v["preview"] = preview(StrategyRule.model_validate(v["rule"]))
        return {"strategies": out}

    @app.post("/api/v1/strategies", dependencies=A)
    def create_strategy(body: RuleBody) -> dict[str, Any]:
        try:
            sid, v = store.create_strategy(body.rule, body.prompts, kind=body.kind,
                                           parent_methodology=body.parent_methodology)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"strategy_id": sid, "version": v, "status": "DRAFT", "preview": preview(body.rule),
                "next": "approve this version with its rule_hash to activate monitoring"}

    @app.put("/api/v1/strategies/{sid}", dependencies=A)
    def revise_strategy(sid: str, body: RuleBody) -> dict[str, Any]:
        try:
            v = store.revise_strategy(sid, body.rule, body.prompts)
        except KeyError as exc:
            raise HTTPException(404, "strategy not found") from exc
        return {"strategy_id": sid, "version": v, "preview": preview(body.rule)}

    @app.post("/api/v1/strategies/{sid}/versions/{version}/approve", dependencies=A)
    def approve(sid: str, version: int, body: ApproveRequest) -> dict[str, Any]:
        try:
            store.approve(sid, version, body.rule_hash)
        except KeyError as exc:
            raise HTTPException(404, "strategy or version not found") from exc
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"strategy_id": sid, "version": version, "status": "ACTIVE"}

    @app.post("/api/v1/strategies/{sid}/deactivate", dependencies=A)
    def deactivate(sid: str) -> dict[str, Any]:
        try:
            store.deactivate(sid)
        except KeyError as exc:
            raise HTTPException(404, "strategy not found") from exc
        return {"strategy_id": sid, "status": "INACTIVE"}

    @app.get("/api/v1/alerts", dependencies=A)
    def alerts(after_seq: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        rows = store.alerts(after_seq=after_seq, limit=limit)
        return {"alerts": rows, "next_after_seq": max([after_seq, *(r["seq"] for r in rows)]),
                "order": "newest_first" if after_seq == 0 else "oldest_first",
                "has_more": len(rows) == limit}

    @app.get("/api/v1/monitor/status", dependencies=A)
    def monitor_status() -> dict[str, Any]:
        return {"running": run_monitor, "subscriptions": monitor.status(),
                "streams": streams.snapshot() if streams else [],
                "streams_note": "WebSocket streams only wake the monitor early; analysis always uses validated REST bars."}

    @app.get("/api/v1/usage", dependencies=A)
    def usage() -> dict[str, Any]:
        s = store.settings()
        return {**store.usage_summary(), "monthly_ai_token_budget": s.monthly_ai_token_budget,
                "monthly_ai_budget_usd": s.monthly_ai_budget_usd,
                "note": "Costs are estimates from token counts and configured prices; verify against invoices."}

    @app.post("/api/v1/copilot/ask", dependencies=L)
    def ask(body: AskRequest) -> dict[str, Any]:
        settings = store.settings()
        codes = tuple(dict.fromkeys((*settings.timeframes, settings.analysis_timeframe)))
        inst, snap = snapshot_for(body.symbol, body.market, codes)
        cache = dict(snap["_series"])

        def get(tf: str):
            if tf not in cache:
                cache[tf] = upstream(service.series, inst, tf)
            return cache[tf]
        persp = evaluate_perspectives(inst, settings.analysis_timeframe,
                                      [s.model_dump() for s in settings.selected_methodologies], get,
                                      as_of_ms=snap["as_of_ms"])
        return copilot.ask(body.question, snap, perspectives=persp, deep=body.deep)

    @app.post("/api/v1/copilot/challenge", dependencies=L)
    def challenge_thesis(body: ChallengeRequest) -> dict[str, Any]:
        _, snap = snapshot_for(body.symbol, body.market, tuple(store.settings().timeframes))
        return copilot.challenge_thesis(body.thesis, snap, focus_timeframe=body.focus_timeframe, deep=body.deep)

    @app.post("/api/v1/backtest", dependencies=L)
    def backtest(body: BacktestRequest) -> dict[str, Any]:
        if (body.rule is None) == (body.event_type is None):
            raise HTTPException(422, "provide exactly one of rule or event_type")
        inst = upstream(service.resolve, body.symbol, body.market)
        try:
            cfg = BacktestConfig(**body.config.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.rule is not None:
            source: Any = body.rule
            tf = body.rule.trigger_timeframe
            others = {t: upstream(service.series, inst, t, bars=body.bars) for t in body.rule.timeframes() - {tf}}
        else:
            source, tf, others = body.event_type, body.timeframe, {}
            upstream(Timeframe, tf)
        series = upstream(service.series, inst, tf, bars=body.bars)
        try:
            if body.walk_forward:
                return walk_forward(source, series, cfg, n_folds=body.n_folds, other_series=others)
            return run_backtest(source, series, cfg, other_series=others)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/copilot/vision", dependencies=L)
    async def vision(image: UploadFile = File(...), question: str = Form(default="", max_length=4000),
                     symbol: str | None = Form(default=None, pattern=r"^[A-Z0-9]{3,20}$"),
                     market: Market = Form(default="usdm_perpetual")) -> dict[str, Any]:
        data = await image.read(MAX_IMAGE_BYTES + 1)
        snap = None
        if symbol:
            _, snap = await asyncio.to_thread(snapshot_for, symbol, market, ("1h", "1d"))
        try:
            return await asyncio.to_thread(copilot.analyze_chart_image, data, question, snap, expected_symbol=symbol)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return app

