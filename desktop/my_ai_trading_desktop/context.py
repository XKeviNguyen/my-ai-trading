"""Shared state for every tab: API client, background runner, active instrument and settings cache."""
from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from .client import DEFAULT_PORT, ApiClient, ApiError, UnauthorizedError, read_token_file, token_path
from .widgets import confirm, plain_label, status_label
from .worker import TaskRunner


class TokenSource:
    """Token from the engine's token file, or a pasted token kept in memory for this session only."""

    def __init__(self) -> None:
        self._pasted: str | None = None

    def set_pasted(self, token: str | None) -> None:
        self._pasted = token or None

    @property
    def has_pasted(self) -> bool:
        return self._pasted is not None

    def __call__(self) -> str | None:
        return self._pasted or read_token_file()

    def describe(self) -> str:
        if self._pasted:
            return "Using a pasted token (kept in memory for this session only; hidden)."
        if read_token_file():
            return f"Using the token file {token_path()} (contents hidden)."
        return f"No token found. Expected the token file at {token_path()}, or paste a token."

    def __repr__(self) -> str:  # never reveal the token in logs or tracebacks
        return "TokenSource(<hidden>)"


class AppContext(QObject):
    instrument_changed = Signal()
    settings_changed = Signal()

    def __init__(self, *, port: int = DEFAULT_PORT, settings: QSettings | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.tokens = TokenSource()
        self.client = ApiClient(self.tokens, port=port)
        self.runner = TaskRunner(self)
        self.qsettings = settings or QSettings("my-ai-trading", "desktop")
        self.symbol: str | None = None
        self.market = "usdm_perpetual"
        self.analysis_tf = "1h"
        self.settings_cache: dict[str, Any] | None = None
        self.profiles_cache: list[dict[str, Any]] | None = None
        self.select_tab: Callable[[str], None] = lambda _name: None
        # Replaceable in tests so no modal dialog blocks the event loop.
        self.confirm: Callable[..., bool] = confirm

    # ---- instrument ----
    def set_instrument(self, symbol: str | None, market: str, analysis_tf: str) -> None:
        changed = (symbol, market, analysis_tf) != (self.symbol, self.market, self.analysis_tf)
        self.symbol, self.market, self.analysis_tf = symbol, market, analysis_tf
        if symbol:
            self.qsettings.setValue("instrument/symbol", symbol)
        self.qsettings.setValue("instrument/market", market)
        self.qsettings.setValue("instrument/analysis_tf", analysis_tf)
        if changed:
            self.instrument_changed.emit()

    def instrument(self) -> tuple[str, str] | None:
        return (self.symbol, self.market) if self.symbol else None

    # ---- settings ----
    def run(self, fn: Callable[[], Any], on_ok: Callable[[Any], None],
            on_err: Callable[[ApiError], None] | None = None) -> int:
        return self.runner.run(fn, on_ok, on_err)

    def update_settings_async(self, mutate: Callable[[dict[str, Any]], None], on_ok: Callable[[dict], None],
                              on_err: Callable[[ApiError], None]) -> None:
        """GET current settings in the worker, mutate a copy, PUT the full object back."""
        def job() -> dict[str, Any]:
            current = copy.deepcopy(self.client.get_settings())
            mutate(current)
            return self.client.put_settings(current)

        def done(saved: dict[str, Any]) -> None:
            self.settings_cache = saved
            self.settings_changed.emit()
            on_ok(saved)
        self.run(job, done, on_err)

    # ---- errors ----
    def error_widget(self, err: ApiError | Exception) -> QWidget:
        msg = str(err) if isinstance(err, ApiError) else "Unexpected error in the desktop client."
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(status_label("error", msg), 1)
        if isinstance(err, UnauthorizedError):
            b = QPushButton("Open Settings")
            b.clicked.connect(lambda: self.select_tab("settings"))
            lay.addWidget(b)
        return w

    @staticmethod
    def note(text: str) -> QWidget:
        return plain_label(text, muted=True)
