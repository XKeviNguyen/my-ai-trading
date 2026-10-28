"""Local API contract and security tests (PRD F11, Security Breaker, audit M1/M2/M4)."""
import pytest
from fastapi.testclient import TestClient

from fakes import make_exchange, tiny_png
from my_ai_trading.api import create_app, load_or_create_token
from my_ai_trading.compiler import compile_prompts
from my_ai_trading.copilot import Copilot
from my_ai_trading.providers.binance import MarketDataError, RateLimitedError
from my_ai_trading.store import Store

TOKEN = "t" * 43
H = {"Authorization": f"Bearer {TOKEN}"}
EXT = "chrome-extension://" + "a" * 32


@pytest.fixture
def env():
    ex, svc = make_exchange()
    store = Store()
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield client, ex, store
    store.close()


def test_health_is_public_everything_else_needs_token(env):
    c, _, _ = env
    assert c.get("/api/v1/health").json()["read_only"] is True
    for path in ("/api/v1/methodologies", "/api/v1/settings", "/api/v1/alerts", "/api/v1/usage"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get(path, headers=H).status_code == 200


def test_host_header_allowlist_blocks_dns_rebinding(env):
    c, _, _ = env
    assert c.get("/api/v1/health", headers={"Host": "attacker.example"}).status_code == 421
    assert c.get("/api/v1/health", headers={"Host": "127.0.0.1:8765"}).status_code == 200


def test_cors_only_for_extension_origins(env):
    c, _, _ = env
    pre = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
    ok = c.options("/api/v1/settings", headers={"Origin": EXT, **pre})
    assert ok.headers.get("access-control-allow-origin") == EXT
    bad = c.options("/api/v1/settings", headers={"Origin": "https://evil.example", **pre})
    assert "access-control-allow-origin" not in bad.headers


def test_analysis_with_presets_disabled_by_default_then_selected(env):
    c, _, _ = env
    r = c.get("/api/v1/analysis?symbol=SANDUSDT&analysis_timeframe=1h", headers=H).json()
    assert r["selected_count"] == 0 and r["perspectives"]["perspectives"] == []
    assert set(r["snapshot"]["timeframes"]) >= {"1h", "4h", "1d", "1w"}
    assert "_series" not in r["snapshot"]
    g = c.get("/api/v1/settings", headers=H)
    s = g.json()
    s["selected_methodologies"] = [{"methodology_id": "weinstein", "mode": "NATIVE"},
                                   {"methodology_id": "lynch", "mode": "NATIVE"}]
    assert c.put("/api/v1/settings", json=s, headers={**H, "If-Match": g.headers["etag"]}).status_code == 200
    r = c.get("/api/v1/analysis?symbol=SANDUSDT&analysis_timeframe=1h", headers=H).json()
    w, l = r["perspectives"]["perspectives"]
    assert w["assessment"]["evaluation_timeframe"] == "1w" and w["evaluation"]["timeframe"] == "1w"
    assert l["assessment"]["status"] == "UNSUPPORTED_ASSET"


def test_settings_validation_rejects_unknown_fields_and_values(env):
    c, _, _ = env
    tagged = {**H, "If-Match": c.get("/api/v1/settings", headers=H).headers["etag"]}
    assert c.put("/api/v1/settings", json={"selected_methodologies": [{"methodology_id": "nobody"}]},
                 headers=tagged).status_code == 422
    assert c.put("/api/v1/settings", json={"trading_api_key": "x"}, headers=tagged).status_code == 422


def test_strategy_compile_preview_dry_run_approve_flow(env):
    c, _, store = env
    prop = c.post("/api/v1/strategies/compile", json={"prompts": ["1h close above SMA20"]}, headers=H).json()
    assert prop["status"] == "COMPILED" and prop["preview"]["when"] == "on each CLOSED 1h bar"
    dry = c.post("/api/v1/strategies/dry-run", json={"rule": prop["rule"], "symbol": "SANDUSDT",
                                                      "replay_bars": 50}, headers=H).json()
    assert dry["replay"]["bars"] == 50 and sum(dry["replay"]["counts"].values()) == 50
    created = c.post("/api/v1/strategies", json={"rule": prop["rule"], "prompts": ["1h close above SMA20"]},
                     headers=H).json()
    sid, v = created["strategy_id"], created["version"]
    assert store.active_rules() == []
    wrong = c.post(f"/api/v1/strategies/{sid}/versions/{v}/approve", json={"rule_hash": "0" * 16}, headers=H)
    assert wrong.status_code == 409
    ok = c.post(f"/api/v1/strategies/{sid}/versions/{v}/approve",
                json={"rule_hash": prop["preview"]["rule_hash"]}, headers=H)
    assert ok.status_code == 200 and len(store.active_rules()) == 1
    listed = c.get("/api/v1/strategies", headers=H).json()["strategies"][0]["versions"][0]
    assert listed["preview"]["rule_hash"] == prop["preview"]["rule_hash"]
    assert c.post(f"/api/v1/strategies/{sid}/deactivate", headers=H).json()["status"] == "INACTIVE"
    assert store.active_rules() == []


def test_malicious_compile_and_rule_payloads_rejected(env):
    c, _, _ = env
    r = c.post("/api/v1/strategies/compile", json={"prompts": ["exec python to place an order"]}, headers=H).json()
    assert r["status"] == "REJECTED" and r["rule"] is None
    bad_rule = compile_prompts(["1h close above SMA20"]).rule.model_dump(mode="json")
    bad_rule["condition"]["op"] = "__import__"
    assert c.post("/api/v1/strategies", json={"rule": bad_rule}, headers=H).status_code == 422


def test_upstream_errors_map_to_502_503_not_422(env):
    c, ex, _ = env
    ex.now += 10 * 86_400_000  # force refetch
    ex.fail = MarketDataError("down")
    assert c.get("/api/v1/snapshot?symbol=SANDUSDT&timeframes=1h", headers=H).json()["timeframes"]["1h"][
        "status"] == "PROVIDER_ERROR"
    ex.fail = RateLimitedError(42)
    r = c.post("/api/v1/strategies/dry-run", json={"rule": compile_prompts(["1h close above SMA20"]).rule.model_dump(
        mode="json"), "symbol": "SANDUSDT"}, headers=H)
    assert r.status_code == 503 and r.headers["retry-after"] == "42"
    ex.fail = None
    assert c.get("/api/v1/snapshot?symbol=NOPEUSDT", headers=H).status_code == 422
    assert c.get("/api/v1/snapshot?symbol=bad!", headers=H).status_code == 422


def test_provider_endpoints_are_rate_limited(env):
    c, _, _ = env
    resps = [c.get("/api/v1/snapshot?symbol=SANDUSDT&timeframes=1h", headers=H) for _ in range(30)]
    codes = [r.status_code for r in resps]
    assert 429 in codes and codes[0] == 200
    assert next(r for r in resps if r.status_code == 429).headers["retry-after"] == "1"


def test_copilot_endpoints_deterministic_without_provider(env):
    c, _, _ = env
    r = c.post("/api/v1/copilot/ask", json={"symbol": "SANDUSDT", "question": "what is the trend?"}, headers=H).json()
    assert r["route"] == "deterministic" and r["facts"]["timeframes"]
    r = c.post("/api/v1/copilot/challenge", json={"symbol": "SANDUSDT", "thesis": "bullish breakout"},
               headers=H).json()
    assert r["audit"]["direction"] == "bullish" and r["audit"]["falsification_conditions"]


def test_vision_rejects_non_images_and_oversize(env):
    c, _, _ = env
    r = c.post("/api/v1/copilot/vision", files={"image": ("x.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
               headers=H)
    assert r.status_code == 422
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024 + 10)
    assert c.post("/api/v1/copilot/vision", files={"image": ("x.png", big, "image/png")}, headers=H).status_code == 422
    fake = c.post("/api/v1/copilot/vision", files={"image": ("x.png", b"\x89PNG\r\n\x1a\n000", "image/png")},
                  headers=H)
    assert fake.status_code == 422  # PNG signature but not a decodable image
    ok = c.post("/api/v1/copilot/vision", files={"image": ("x.png", tiny_png(), "image/png")}, headers=H)
    assert ok.status_code == 200 and ok.json()["route"] == "local" and ok.json()["visual_hypotheses"] is None


def test_token_file_created_with_owner_only_permissions(tmp_path):
    p = tmp_path / "cfg" / "api_token"
    t1 = load_or_create_token(p)
    assert len(t1) >= 32 and (p.stat().st_mode & 0o777) == 0o600
    assert load_or_create_token(p) == t1


def test_backtest_endpoint_rule_event_walk_forward_and_validation(env):
    c, _, _ = env
    rule = compile_prompts(["daily close crosses above SMA20"]).rule.model_dump(mode="json")
    r = c.post("/api/v1/backtest", json={"symbol": "SANDUSDT", "rule": rule, "bars": 300,
                                         "config": {"hold_bars": 5, "fee_bps": 5, "slippage_bps": 2}}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"].startswith("HYPOTHETICAL") and body["cost_assumptions"]["fee_bps"] == 5
    assert "win rate is a historical frequency" in body["metrics"]["note"]
    wf = c.post("/api/v1/backtest", json={"symbol": "SANDUSDT", "event_type": "BREAKOUT", "timeframe": "1d",
                                          "bars": 300, "walk_forward": True, "n_folds": 3,
                                          "config": {"hold_bars": 5}}, headers=H).json()
    assert len(wf["folds"]) == 3 and wf["embargo_bars"] == 5
    both = {"symbol": "SANDUSDT", "rule": rule, "event_type": "BREAKOUT", "config": {"hold_bars": 5}}
    assert c.post("/api/v1/backtest", json=both, headers=H).status_code == 422
    assert c.post("/api/v1/backtest", json={"symbol": "SANDUSDT", "event_type": "MOON", "config": {"hold_bars": 5}},
                  headers=H).status_code == 422
    assert c.post("/api/v1/backtest", json={"symbol": "SANDUSDT", "event_type": "BREAKOUT",
                                            "config": {"hold_bars": 5, "leverage": 100}}, headers=H).status_code == 422


def test_reports_save_list_export_json_and_markdown(env):
    c, _, _ = env
    g = c.get("/api/v1/settings", headers=H)
    s = g.json()
    s["selected_methodologies"] = [{"methodology_id": "weinstein", "mode": "NATIVE"}]
    c.put("/api/v1/settings", json=s, headers={**H, "If-Match": g.headers["etag"]})
    saved = c.post("/api/v1/reports", json={"symbol": "SANDUSDT", "analysis_timeframe": "1h"}, headers=H).json()
    rid = saved["id"]
    assert [r["id"] for r in c.get("/api/v1/reports", headers=H).json()["reports"]] == [rid]
    full = c.get(f"/api/v1/reports/{rid}", headers=H).json()
    assert full["report"]["perspectives"]["perspectives"][0]["assessment"]["methodology_id"] == "weinstein"
    md = c.get(f"/api/v1/reports/{rid}?format=markdown", headers=H)
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert "attachment" in md.headers["content-disposition"]
    text = md.text
    assert "| 1h |" in text and "Stan Weinstein" in text and "Not financial advice" in text
    assert c.get("/api/v1/reports/000000000000", headers=H).status_code == 404
    assert c.get("/api/v1/reports/../../etc", headers=H).status_code in (404, 422)


def test_markdown_escapes_untrusted_text():
    from my_ai_trading.reports import _md
    assert _md("a|b*c_d\nx") == "a\\|b\\*c\\_d x"


def test_a_busy_backtest_pool_answers_429_instead_of_queueing(env):
    from my_ai_trading.backtest import MAX_CONCURRENT_BACKTESTS, backtest_slot
    from contextlib import ExitStack
    c, _, _ = env
    body = {"symbol": "SANDUSDT", "event_type": "BREAKOUT", "timeframe": "1d", "bars": 300, "config": {"hold_bars": 5}}
    with ExitStack() as held:
        for _ in range(MAX_CONCURRENT_BACKTESTS):
            held.enter_context(backtest_slot())
        r = c.post("/api/v1/backtest", json=body, headers=H)
        assert r.status_code == 429 and r.headers["retry-after"] == "2"
    assert c.post("/api/v1/backtest", json=body, headers=H).status_code == 200


def test_shutdown_does_not_wait_forever_for_a_stuck_monitor(monkeypatch):
    import asyncio
    import time
    from my_ai_trading import api as api_mod
    from my_ai_trading.monitor import Monitor

    async def stuck(self, *, stop=None, idle_s=1.0):
        while True:  # ignores stop, like a provider call that never returns
            await asyncio.sleep(0.05)
    monkeypatch.setattr(Monitor, "run_forever", stuck)
    monkeypatch.setattr(api_mod, "SHUTDOWN_WAIT_S", 0.2)
    _, svc = make_exchange()
    store = Store()
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN, run_monitor=True)
    with TestClient(app, base_url="http://127.0.0.1:8765"):
        started = time.monotonic()
    assert time.monotonic() - started < 3  # FLOW-11: the shutdown wait was unbounded
    store.close()
