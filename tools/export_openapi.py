#!/usr/bin/env python3
"""Write the local API's OpenAPI spec to docs/api/openapi.json (or check it with --check).

The app is built offline: an in-memory store, an in-memory credential vault, no AI
provider, and market and exchange adapters that refuse any network call. Nothing is
started and no request is sent. Run from the repository root with the core package
and its `api` extra installed (or with PYTHONPATH=src).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "docs" / "api" / "openapi.json"


def _no_network(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("export_openapi builds the app offline; network access is not allowed")


def build_spec() -> dict[str, Any]:
    from my_ai_trading.accounts.credentials import CredentialStore, MemoryVault
    from my_ai_trading.accounts.service import AccountService
    from my_ai_trading.api import create_app
    from my_ai_trading.copilot import Copilot
    from my_ai_trading.market import MarketDataService
    from my_ai_trading.providers.binance import BinancePublic
    from my_ai_trading.store import Store

    store = Store()
    try:
        accounts = AccountService(CredentialStore(store, persistent_vault=MemoryVault), adapter_factory=_no_network)
        app = create_app(service=MarketDataService(BinancePublic(fetch=_no_network)), store=store,
                         copilot=Copilot(store, None), api_token="x" * 43, accounts=accounts,
                         enable_docs=True)
        return app.openapi()
    finally:
        store.close()


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=1, sort_keys=True, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed spec is out of date")
    args = parser.parse_args()
    text = render(build_spec())
    if args.check:
        current = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.exists() else ""
        if current.rstrip("\n") != text:
            print(f"{SPEC_PATH.relative_to(ROOT)} is out of date; run: python tools/export_openapi.py", file=sys.stderr)
            return 1
        print("OpenAPI spec is up to date")
        return 0
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {SPEC_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
