"""Audit FLOW-8 / SEC-6: settings edits are compare-and-set; validation errors never echo submitted values."""
from fastapi.testclient import TestClient

from fakes import make_exchange
from my_ai_trading.api import create_app
from my_ai_trading.copilot import Copilot
from my_ai_trading.store import Store

TOKEN = "e" * 40
H = {"Authorization": f"Bearer {TOKEN}"}


def client():
    s = Store()
    return TestClient(create_app(service=make_exchange()[1], store=s, copilot=Copilot(s, None), api_token=TOKEN),
                      base_url="http://127.0.0.1:8765")


def test_a_stale_tab_cannot_restore_a_revoked_consent():
    c = client()
    first = c.get("/api/v1/settings", headers=H)
    etag = first.headers["ETag"]
    tab_a = {**first.json(), "llm_data_consent": True}
    r = c.put("/api/v1/settings", json=tab_a, headers={**H, "If-Match": etag})
    assert r.status_code == 200 and r.headers["ETag"] != etag
    # tab B still holds the version from before tab A's save
    tab_b = {**first.json(), "watchlist": [{"symbol": "BTCUSDT", "market": "spot"}]}
    assert c.put("/api/v1/settings", json=tab_b, headers={**H, "If-Match": etag}).status_code == 412
    assert c.get("/api/v1/settings", headers=H).json()["llm_data_consent"] is True


def test_cors_exposes_the_version_tag_to_the_extension():
    c = client()
    ext = "chrome-extension://" + "a" * 32
    r = c.get("/api/v1/settings", headers={**H, "Origin": ext})
    assert "etag" in r.headers.get("access-control-expose-headers", "").lower()
    pre = c.options("/api/v1/settings", headers={"Origin": ext, "Access-Control-Request-Method": "PUT",
                                                 "Access-Control-Request-Headers": "if-match,content-type,authorization"})
    assert pre.status_code == 200 and "if-match" in pre.headers["access-control-allow-headers"].lower()


def test_settings_validation_errors_do_not_echo_values():
    c = client()
    g = c.get("/api/v1/settings", headers=H)
    s = g.json()
    secret = "my-private-note-0123456789"
    r = c.put("/api/v1/settings", json={**s, "analysis_timeframe": secret}, headers={**H, "If-Match": g.headers["etag"]})
    assert r.status_code == 422 and secret not in r.text


def test_a_save_without_if_match_is_refused_and_unknown_key_names_are_not_echoed():
    c = client()
    g = c.get("/api/v1/settings", headers=H)
    r = c.put("/api/v1/settings", json=g.json(), headers=H)
    assert r.status_code == 428  # re-audit: a PUT without If-Match was still an unconditional overwrite
    pasted = "pasted-into-a-key-name-0123456789"
    r = c.put("/api/v1/settings", json={**g.json(), pasted: 1, "risk_limits": {pasted: 2}},
              headers={**H, "If-Match": g.headers["etag"]})
    assert r.status_code == 422 and pasted not in r.text
