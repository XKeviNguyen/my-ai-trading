"""Command line: offline demo, public-data analysis, monitor, local API server."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .compiler import compile_prompts
from .engine import analyze
from .fixtures import demo_series
from .methodology import PROFILES


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="my-ai-trading", description="Read-only market research (no trading)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="offline analysis of SYNTHETIC data; no network")
    sub.add_parser("methodologies", help="list the 20 methodology profiles and their status")
    a = sub.add_parser("analyze", help="multi-timeframe analysis from public Binance data")
    a.add_argument("--symbol", default="SANDUSDT")
    a.add_argument("--market", default="usdm_perpetual", choices=["usdm_perpetual", "spot"])
    a.add_argument("--timeframes", default="1h,4h,1d,1w")
    c = sub.add_parser("compile", help="compile natural-language rule prompts (preview only)")
    c.add_argument("prompts", nargs="+")
    c.add_argument("--timeframe", default=None, help="chart timeframe to assume when a prompt omits one")
    m = sub.add_parser("monitor", help="run the Auto Analyst for the saved watchlist (Ctrl+C to stop)")
    m.add_argument("--db", default=None)
    s = sub.add_parser("serve", help="run the local API on 127.0.0.1 (for the browser extension)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-monitor", action="store_true", help="do not run the background monitor")
    s.add_argument("--no-streams", action="store_true",
                   help="do not use Binance kline WebSocket streams to wake the monitor (REST schedule only)")
    s.add_argument("--db", default=None)
    s.add_argument("--docs", action="store_true", help="enable /docs (loads Swagger UI scripts from a CDN)")
    sub.add_parser("token", help="print the path of the local API token file (not the token)")
    args = p.parse_args(argv)

    if args.cmd == "demo":
        s = demo_series("1d")
        print(json.dumps({"fixture": "SYNTHETIC (not market data)",
                          "analysis": analyze(s, as_of_ms=s.candles[-1].close_time_ms + 1)}, indent=2))
        return 0
    if args.cmd == "methodologies":
        for prof in PROFILES:
            tf = "/".join(prof.documented_timeframes) or "UNVERIFIED"
            print(f"{prof.methodology_id:14} {prof.implementation_maturity:13} native={tf:8} {prof.name}")
        return 0
    if args.cmd == "compile":
        print(json.dumps(compile_prompts(args.prompts, default_timeframe=args.timeframe).as_dict(), indent=2))
        return 0
    if args.cmd == "token":
        from .api import load_or_create_token, token_path
        load_or_create_token()
        print(f"API token stored at {token_path()} (mode 0600). Paste its contents into the extension settings.")
        return 0

    from .market import MarketDataService
    from .providers.binance import BinancePublic, MarketDataError
    service = MarketDataService(BinancePublic())
    if args.cmd == "analyze":
        try:
            inst = service.resolve(args.symbol, args.market)
            snap = service.snapshot(inst, tuple(t.strip() for t in args.timeframes.split(",")))
        except (MarketDataError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        snap.pop("_series", None)
        print(json.dumps(snap, indent=2, default=str))
        return 0

    from .store import Store, default_db_path
    store = Store(args.db or default_db_path())
    if args.cmd == "monitor":
        from .monitor import Monitor, dumps
        mon = Monitor(service, store, on_alert=lambda a: print(dumps(a), flush=True))
        try:
            asyncio.run(mon.run_forever())
        except KeyboardInterrupt:
            pass
        return 0
    if args.cmd == "serve":
        import uvicorn
        from .api import create_app
        if not 1024 <= args.port <= 65535:
            p.error("port must be 1024..65535")
        app = create_app(service=service, store=store, port=args.port, run_monitor=not args.no_monitor, run_streams=not args.no_streams,
                         enable_docs=args.docs)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")  # never 0.0.0.0
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
