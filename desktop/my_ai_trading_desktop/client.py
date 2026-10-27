"""Typed client for the local My AI Trading engine API. No Qt imports: unit-testable on its own.

Security rules:
- Requests only go to http://127.0.0.1:<port> (port defaults to 8765; tests may pass another one).
- The bearer token is sent only there; proxies are disabled and redirects are refused.
- Error messages never contain the token.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_PORT = 8765
HOST = "127.0.0.1"
TIMEFRAMES = ("1h", "4h", "1d", "1w", "1M")
MARKETS = ("usdm_perpetual", "spot")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")
# The engine writes secrets.token_urlsafe(32) (43 chars) and accepts any file content of 32+ chars.
TOKEN_RE = re.compile(r"^[\x21-\x7e]{32,512}$")
PATH_RE = re.compile(r"^/api/v1(?:/[A-Za-z0-9_-]+)*$")
RULE_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
STRATEGY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REPORT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
DEFAULT_TIMEOUT_S = 30.0
LLM_TIMEOUT_S = 180.0
HEALTH_TIMEOUT_S = 5.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DETAIL_CHARS = 600
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {"image/png": (".png",), "image/jpeg": (".jpg", ".jpeg"), "image/webp": (".webp",)}


# ---------------------------------------------------------------- errors
class ApiError(Exception):
    """Base error. `str(err)` is a user-facing message that never contains the token."""

    kind = "http"

    def __init__(self, status: int = 0, detail: str = "", retry_after_s: int | None = None):
        self.status, self.detail, self.retry_after_s = status, detail, retry_after_s
        super().__init__(self.user_message())

    def _retry(self) -> str:
        return f" Retry after {self.retry_after_s} s." if self.retry_after_s is not None else \
            " Wait a few seconds and retry."

    def user_message(self) -> str:
        return "Unexpected API error" + (f" (HTTP {self.status})" if self.status else "") + \
            (f": {self.detail}" if self.detail else ".")


class UnauthorizedError(ApiError):
    kind = "unauthorized"

    def user_message(self) -> str:
        return ("API token missing or invalid. Open Settings and load the token file or paste the token "
                "(run `my-ai-trading token` to see where the file is).")


class EngineUnavailableError(ApiError):
    kind = "engine_unavailable"

    def user_message(self) -> str:
        if self.status == 421:
            return ("The program on 127.0.0.1 rejected this client's Host header (HTTP 421). Make sure the "
                    "engine was started with `my-ai-trading serve` on the expected port.")
        return "Local engine not running or not reachable: run `my-ai-trading serve`."


class RateLimitedError(ApiError):
    kind = "rate_limited"

    def user_message(self) -> str:
        return "Too many requests to the local engine." + self._retry()


class UpstreamUnavailableError(ApiError):
    kind = "upstream_unavailable"

    def user_message(self) -> str:
        return "Market data provider is rate limiting or unavailable." + self._retry() + \
            (f" ({self.detail})" if self.detail else "")


class UpstreamError(ApiError):
    kind = "upstream_error"

    def user_message(self) -> str:
        return "Market data provider error" + (f": {self.detail}" if self.detail else ".")


class ValidationError(ApiError):
    kind = "validation"

    def user_message(self) -> str:
        return "Request rejected: " + (self.detail or "invalid input")


class NotFoundError(ApiError):
    kind = "not_found"

    def user_message(self) -> str:
        return "Not found" + (f": {self.detail}" if self.detail else ".")


class ConflictError(ApiError):
    kind = "conflict"

    def user_message(self) -> str:
        return "Conflict: " + (self.detail or "state changed; reload and review")


class ApiTimeoutError(ApiError):
    kind = "timeout"

    def user_message(self) -> str:
        return "The local engine did not answer in time."


class RedirectRefusedError(ApiError):
    kind = "redirect_refused"

    def user_message(self) -> str:
        return "The local engine answered with a redirect; it was refused so the token is not sent elsewhere."


class InternalError(ApiError):
    kind = "internal"

    def user_message(self) -> str:
        return "Unexpected error in the desktop client."


_STATUS_ERRORS: dict[int, type[ApiError]] = {
    400: ValidationError, 401: UnauthorizedError, 403: UnauthorizedError, 404: NotFoundError,
    409: ConflictError, 421: EngineUnavailableError, 422: ValidationError, 429: RateLimitedError,
    502: UpstreamError, 503: UpstreamUnavailableError,
}


def error_for_status(status: int, detail: str, retry_after_s: int | None = None) -> ApiError:
    cls = _STATUS_ERRORS.get(status, ApiError)
    if cls not in (RateLimitedError, UpstreamUnavailableError):
        retry_after_s = None
    return cls(status, detail, retry_after_s)


# ---------------------------------------------------------------- helpers
def token_path() -> Path:
    """Same location the engine uses (respects XDG_CONFIG_HOME)."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "my-ai-trading" / "api_token"


def is_valid_token(token: str | None) -> bool:
    return isinstance(token, str) and bool(TOKEN_RE.fullmatch(token))


def read_token_file(path: Path | None = None) -> str | None:
    """Return the token from the engine's token file, or None if missing/unreadable/invalid."""
    path = path or token_path()
    try:
        with open(path, encoding="utf-8") as f:
            tok = f.read(1024).strip()
    except (OSError, UnicodeDecodeError):
        return None
    return tok if is_valid_token(tok) else None


def scrub(text: str, token: str | None) -> str:
    out = text
    if token and len(token) >= 8:
        out = out.replace(token, "[redacted]")
    return out if len(out) <= MAX_DETAIL_CHARS else out[:MAX_DETAIL_CHARS] + "…"


def extract_detail(body: Any) -> str:
    """Turn a FastAPI error body (`detail` string or validation list) into one readable line."""
    if not isinstance(body, dict):
        return ""
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for d in detail:
            if not isinstance(d, dict):
                parts.append(str(d))
                continue
            loc = ".".join(str(p) for p in d.get("loc", []) if p != "body") if isinstance(d.get("loc"), list) else ""
            msg = d.get("msg") if isinstance(d.get("msg"), str) else "invalid"
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(parts)
    return ""


def parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        n = float(value.strip())
    except ValueError:
        return None
    return int(-(-n // 1)) if n >= 0 and n == n and n != float("inf") else None


def require_symbol(symbol: str, market: str) -> None:
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise ValidationError(0, "symbol must match ^[A-Z0-9]{3,20}$")
    if market not in MARKETS:
        raise ValidationError(0, "market must be usdm_perpetual or spot")


def encode_multipart(fields: dict[str, str], file_field: str, filename: str, content_type: str,
                     data: bytes) -> tuple[bytes, str]:
    boundary = "----myaitrading" + secrets.token_hex(16)
    safe_name = re.sub(r'[^A-Za-z0-9._-]', "_", filename)[:100] or "image"
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n").encode()
        out += value.encode("utf-8") + b"\r\n"
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{safe_name}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n").encode()
    out += data + b"\r\n" + f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        fp.close()
        raise RedirectRefusedError(code, "")


# ---------------------------------------------------------------- client
class ApiClient:
    def __init__(self, get_token: Callable[[], str | None], *, port: int = DEFAULT_PORT,
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer 1..65535")
        self._get_token = get_token
        self.base_url = f"http://{HOST}:{port}"
        self.timeout_s = timeout_s
        # No ProxyHandler entries: a configured http_proxy must never receive the bearer token.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())

    def build_url(self, path: str, query: dict[str, Any] | None = None) -> str:
        if not PATH_RE.fullmatch(path):
            raise ValidationError(0, "invalid API path")
        url = self.base_url + path
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != HOST or f"{parsed.scheme}://{parsed.netloc}" != self.base_url:
            raise ValidationError(0, "refusing non-local API host")
        q = {k: str(v).lower() if isinstance(v, bool) else str(v)
             for k, v in (query or {}).items() if v is not None and v != ""}
        return url + ("?" + urllib.parse.urlencode(q) if q else "")

    def _send(self, method: str, path: str, *, query: dict[str, Any] | None, json_body: Any, body: bytes | None,
              content_type: str | None, auth: bool, timeout_s: float | None,
              accept: str) -> tuple[int, bytes, str | None]:
        url = self.build_url(path, query)
        headers = {"Accept": accept}
        token: str | None = None
        if auth:
            token = (self._get_token() or "").strip()
            if not token:
                raise UnauthorizedError(0, "no API token")
            if not is_valid_token(token):
                raise UnauthorizedError(0, "token is malformed")
            headers["Authorization"] = f"Bearer {token}"
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = body
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif method in ("POST", "PUT"):
            data = b""
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        status, raw, retry = 0, b"", None
        try:
            with self._opener.open(req, timeout=timeout_s or self.timeout_s) as resp:
                status = resp.status
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except RedirectRefusedError:
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except OSError:
                raw = b""
            retry = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            exc.close()
        except (TimeoutError, socket.timeout):
            raise ApiTimeoutError() from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ApiTimeoutError() from None
            raise EngineUnavailableError(0, "connection failed") from None
        except (ConnectionError, OSError):
            raise EngineUnavailableError(0, "connection failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ApiError(status, "response too large")
        if not 200 <= status < 300:
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, ValueError):
                parsed = None
            raise error_for_status(status, scrub(extract_detail(parsed), token), retry)
        return status, raw, token

    def request(self, method: str, path: str, *, query: dict[str, Any] | None = None, json_body: Any = None,
                body: bytes | None = None, content_type: str | None = None, auth: bool = True,
                timeout_s: float | None = None) -> dict[str, Any]:
        status, raw, _ = self._send(method, path, query=query, json_body=json_body, body=body,
                                    content_type=content_type, auth=auth, timeout_s=timeout_s,
                                    accept="application/json")
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            parsed = None
        if not isinstance(parsed, dict):
            raise ApiError(status, "response was not a JSON object")
        return parsed

    def request_text(self, path: str, *, query: dict[str, Any] | None = None) -> str:
        """GET a text response (e.g. a Markdown report)."""
        status, raw, _ = self._send("GET", path, query=query, json_body=None, body=None, content_type=None,
                                    auth=True, timeout_s=None, accept="text/markdown, text/plain")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError(status, "response was not UTF-8 text") from None

    # ---- endpoints ----
    def health(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/health", auth=False, timeout_s=HEALTH_TIMEOUT_S)

    def methodologies(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/methodologies")

    def get_settings(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/settings")

    def put_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", "/api/v1/settings", json_body=settings)

    def update_settings(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Fetch current settings, apply `mutate` to them and PUT the full object back."""
        current = self.get_settings()
        mutate(current)
        return self.put_settings(current)

    def analysis(self, symbol: str, market: str, analysis_timeframe: str | None = None) -> dict[str, Any]:
        require_symbol(symbol, market)
        return self.request("GET", "/api/v1/analysis",
                            query={"symbol": symbol, "market": market, "analysis_timeframe": analysis_timeframe})

    def ask(self, symbol: str, market: str, question: str, deep: bool) -> dict[str, Any]:
        require_symbol(symbol, market)
        return self.request("POST", "/api/v1/copilot/ask", timeout_s=LLM_TIMEOUT_S,
                            json_body={"symbol": symbol, "market": market, "question": question, "deep": deep})

    def challenge(self, symbol: str, market: str, thesis: str, focus_timeframe: str | None,
                  deep: bool) -> dict[str, Any]:
        require_symbol(symbol, market)
        return self.request("POST", "/api/v1/copilot/challenge", timeout_s=LLM_TIMEOUT_S,
                            json_body={"symbol": symbol, "market": market, "thesis": thesis,
                                       "focus_timeframe": focus_timeframe, "deep": deep})

    def vision(self, image: bytes, filename: str, content_type: str, question: str = "",
               symbol: str | None = None, market: str = "usdm_perpetual") -> dict[str, Any]:
        if content_type not in IMAGE_TYPES:
            raise ValidationError(0, "only PNG, JPEG or WEBP screenshots are accepted")
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise ValidationError(0, "image must be 1 byte to 5 MB")
        fields = {"question": question}
        if symbol:
            require_symbol(symbol, market)
            fields.update(symbol=symbol, market=market)
        body, ctype = encode_multipart(fields, "image", filename, content_type, image)
        return self.request("POST", "/api/v1/copilot/vision", body=body, content_type=ctype,
                            timeout_s=LLM_TIMEOUT_S)

    def compile(self, prompts: list[str], name: str, default_timeframe: str | None,
                use_llm: bool) -> dict[str, Any]:
        return self.request("POST", "/api/v1/strategies/compile",
                            timeout_s=LLM_TIMEOUT_S if use_llm else None,
                            json_body={"prompts": prompts, "name": name, "default_timeframe": default_timeframe,
                                       "use_llm_fallback": use_llm})

    def dry_run(self, rule: dict[str, Any], symbol: str, market: str, replay_bars: int = 200) -> dict[str, Any]:
        require_symbol(symbol, market)
        return self.request("POST", "/api/v1/strategies/dry-run",
                            json_body={"rule": rule, "symbol": symbol, "market": market, "replay_bars": replay_bars})

    def strategies(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/strategies")

    def create_strategy(self, rule: dict[str, Any], prompts: list[str]) -> dict[str, Any]:
        return self.request("POST", "/api/v1/strategies", json_body={"rule": rule, "prompts": prompts, "kind": "CUSTOM"})

    def approve(self, strategy_id: str, version: int, rule_hash: str) -> dict[str, Any]:
        if not STRATEGY_ID_RE.fullmatch(strategy_id) or not isinstance(version, int) or version < 1:
            raise ValidationError(0, "invalid strategy id or version")
        if not RULE_HASH_RE.fullmatch(rule_hash):
            raise ValidationError(0, "invalid rule_hash")
        return self.request("POST", f"/api/v1/strategies/{strategy_id}/versions/{version}/approve",
                            json_body={"rule_hash": rule_hash})

    def deactivate(self, strategy_id: str) -> dict[str, Any]:
        if not STRATEGY_ID_RE.fullmatch(strategy_id):
            raise ValidationError(0, "invalid strategy id")
        return self.request("POST", f"/api/v1/strategies/{strategy_id}/deactivate")

    def alerts(self, after_seq: int, limit: int = 100) -> dict[str, Any]:
        after = after_seq if isinstance(after_seq, int) and after_seq >= 0 else 0
        return self.request("GET", "/api/v1/alerts", query={"after_seq": after, "limit": limit})

    def usage(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/usage")

    def save_report(self, symbol: str, market: str, analysis_timeframe: str | None = None) -> dict[str, Any]:
        require_symbol(symbol, market)
        return self.request("POST", "/api/v1/reports",
                            json_body={"symbol": symbol, "market": market, "analysis_timeframe": analysis_timeframe})

    def reports(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/reports")

    def report_json(self, report_id: str) -> dict[str, Any]:
        if not REPORT_ID_RE.fullmatch(report_id or ""):
            raise ValidationError(0, "invalid report id")
        return self.request("GET", f"/api/v1/reports/{report_id}", query={"format": "json"})

    def report_markdown(self, report_id: str) -> str:
        if not REPORT_ID_RE.fullmatch(report_id or ""):
            raise ValidationError(0, "invalid report id")
        return self.request_text(f"/api/v1/reports/{report_id}", query={"format": "markdown"})
