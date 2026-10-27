"""Importing this module registers every built-in adapter. Add new exchanges here (one line each)."""
from __future__ import annotations

import importlib

BUILTIN = ("binance", "bybit", "okx", "ibkr")

for _name in BUILTIN:
    try:
        importlib.import_module(f"{__package__}.adapters.{_name}")
    except ModuleNotFoundError as exc:  # adapters land milestone by milestone; a missing one is simply not offered
        if exc.name != f"{__package__}.adapters.{_name}":
            raise
