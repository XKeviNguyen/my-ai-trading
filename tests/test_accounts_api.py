"""R8 M1: private account routes. Secrets never come back; web pages cannot reach private routes."""
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fakes import make_exchange
from my_ai_trading.accounts.adapters.base import AuthError, ExchangeAdapter, ExchangeInfo
from my_ai_trading.accounts.credentials import CredentialStore, MemoryVault
from my_ai_trading.accounts.models import AccountSnapshot, AccountTotals, Balance, Environment, KeyPermissions, WalletType
from my_ai_trading.accounts.service import AccountService
from my_ai_trading.api import create_app
from my_ai_trading.copilot import Copilot
from my_ai_trading.store import Store

TOKEN = "t" * 40
H = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "super-secret-api-secret-value-0123456789"
BODY = {"exchange": "fakeex", "environment": "testnet", "api_key": "FAKEKEY-abcd", "api_secret": SECRET}


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


class FakeExchange(ExchangeAdapter):
    INFO = ExchangeInfo(id="fakeex", name="FakeEx", kind="crypto_exchange",
                        environments=(Environment.LIVE, Environment.TESTNET), needs_passphrase=False,
                        markets=("spot",), key_setup_url="https://example.invalid/keys")
    perms = KeyPermissions(read=True, spot_trade=True, derivatives_trade=False, margin_trade=False,
                           withdraw=False, transfer=False)
    calls = 0

    def permissions(self, cred):
        if cred.api_key.reveal() == "BADKEY-xxxx":
            raise AuthError("FakeEx rejected the key (code -2015: invalid key)")
        return self.perms

    def snapshot(self, cred, connection_id):
        FakeExchange.calls += 1
        return AccountSnapshot(connection_id=connection_id, exchange="fakeex", environment=self.environment,
                               as_of_ms=self.now_ms(),
                               totals=[AccountTotals(wallet=WalletType.SPOT, equity=Decimal("1234.5"))],
                               balances=[Balance(asset="USDT", wallet=WalletType.SPOT, free=Decimal("1000"),
                                                 locked=Decimal("0"), total=Decimal("1000"))])


@pytest.fixture()
def env():
    clock = Clock()
    store = Store()
    accounts = AccountService(CredentialStore(store, persistent_vault=MemoryVault, clock=clock),
                              adapter_factory=lambda ex, e: FakeExchange(e, clock=clock), clock=clock)
    _, svc = make_exchange()
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN, accounts=accounts)
    return TestClient(app, base_url="http://127.0.0.1:8765"), clock


def test_connect_list_snapshot_and_disconnect_never_return_the_secret(env):
    c, _ = env
    r = c.post("/api/v1/accounts/connections", json=BODY, headers=H)
    assert r.status_code == 201, r.text
    cid = r.json()["connection"]["connection_id"]
    assert r.json()["connection"]["key_hint"] == "abcd" and r.json()["connection"]["lifetime"] == "session"
    texts = [r.text, c.get("/api/v1/accounts/connections", headers=H).text]
    snap = c.get(f"/api/v1/accounts/connections/{cid}/snapshot", headers=H)
    assert snap.status_code == 200 and snap.json()["balances"][0]["asset"] == "USDT"
    assert snap.json()["totals"][0]["provenance"] == "exchange_reported"
    texts.append(snap.text)
    for t in texts:
        assert SECRET not in t and "FAKEKEY" not in t
    d = c.delete(f"/api/v1/accounts/connections/{cid}", headers=H)
    assert d.status_code == 200 and d.json()["revoked_at_exchange"] is False
    assert c.get(f"/api/v1/accounts/connections/{cid}/snapshot", headers=H).status_code == 404


def test_snapshots_are_cached_briefly_in_memory(env):
    c, clock = env
    cid = c.post("/api/v1/accounts/connections", json=BODY, headers=H).json()["connection"]["connection_id"]
    before = FakeExchange.calls
    c.get(f"/api/v1/accounts/connections/{cid}/snapshot", headers=H)
    c.get(f"/api/v1/accounts/connections/{cid}/snapshot", headers=H)
    assert FakeExchange.calls == before + 1
    c.get(f"/api/v1/accounts/connections/{cid}/snapshot?fresh=true", headers=H)
    assert FakeExchange.calls == before + 2


def test_expired_connection_is_refused_with_410(env):
    c, clock = env
    cid = c.post("/api/v1/accounts/connections", json={**BODY, "lifetime": "1h"},
                 headers=H).json()["connection"]["connection_id"]
    clock.t += 3601
    r = c.get(f"/api/v1/accounts/connections/{cid}/snapshot", headers=H)
    assert r.status_code == 410 and "expired" in r.json()["detail"]
    assert c.get("/api/v1/accounts/connections", headers=H).json()["connections"] == []


def test_live_key_with_trading_permission_is_refused(env):
    c, _ = env
    r = c.post("/api/v1/accounts/connections", json={**BODY, "environment": "live"}, headers=H)
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    assert c.get("/api/v1/accounts/connections", headers=H).json()["connections"] == []


def test_exchange_key_rejection_is_424_not_401(env):
    c, _ = env
    r = c.post("/api/v1/accounts/connections", json={**BODY, "api_key": "BADKEY-xxxx"}, headers=H)
    assert r.status_code == 424 and "-2015" in r.json()["detail"]


def test_validation_errors_never_echo_submitted_values(env):
    c, _ = env
    for bad in ({**BODY, "lifetime": SECRET}, {**BODY, "api_secret": SECRET * 200},
                {**BODY, "unexpected": SECRET}, {**BODY, "environment": ["x", SECRET]}):
        r = c.post("/api/v1/accounts/connections", json=bad, headers=H)
        assert r.status_code == 422
        assert SECRET[:20] not in r.text
    r = c.get("/api/v1/analysis?symbol=" + "bad!" * 3, headers=H)  # the global handler covers every route
    assert r.status_code == 422 and "input" not in json.dumps(r.json())


def test_web_pages_cannot_reach_private_routes_even_with_the_token(env):
    c, _ = env
    for origin in ("https://evil.example", "null", "http://127.0.0.1:8765", "chrome-extension://ZZZZ"):
        r = c.get("/api/v1/accounts/connections", headers={**H, "Origin": origin})
        assert r.status_code == 403, origin
    ext = "chrome-extension://" + "a" * 32
    assert c.get("/api/v1/accounts/connections", headers={**H, "Origin": ext}).status_code == 200
    assert c.get("/api/v1/accounts/connections", headers=H).status_code == 200  # desktop app: no Origin header
    assert c.get("/api/v1/accounts/connections").status_code == 401


def test_private_routes_have_their_own_rate_limit(env):
    c, _ = env
    codes = [c.get("/api/v1/accounts/exchanges", headers=H).status_code for _ in range(40)]
    assert codes[:30] == [200] * 30 and 429 in codes[30:]
    assert c.get("/api/v1/methodologies", headers=H).status_code == 200  # public routes unaffected


def test_exchange_catalog_lists_lifetimes_and_marks_oauth_blocked(env):
    c, _ = env
    r = c.get("/api/v1/accounts/exchanges", headers=H).json()
    assert r["default_lifetime"] == "session"
    assert r["lifetimes"] == ["session", "1h", "24h", "7d", "30d", "until_removed"]
    assert all(e["oauth"] in ("blocked_by_provider_approval", "not_offered") for e in r["exchanges"])


def test_portfolio_aggregates_connections_and_reports_failures_without_crashing(env):
    c, clock = env
    c.post("/api/v1/accounts/connections", json=BODY, headers=H)
    c.post("/api/v1/accounts/connections", json={**BODY, "lifetime": "1h", "label": "short"}, headers=H)
    out = c.get("/api/v1/accounts/portfolio", headers=H).json()
    assert out["total_value_usd"]["value"] == "2469.00" and out["errors"] == []  # 2 x 1234.5 reported equity
    clock.t += 3601  # the 1-hour connection expires and is dropped; the session one keeps working
    out = c.get("/api/v1/accounts/portfolio", headers=H).json()
    assert out["total_value_usd"]["value"] == "1234.50" and len(out["connections"]) == 1
    g = c.get("/api/v1/settings", headers=H)
    s = g.json()
    s["risk_limits"]["max_asset_concentration_pct"] = 75
    assert c.put("/api/v1/settings", json=s, headers={**H, "If-Match": g.headers["etag"]}).status_code == 200
    assert c.get("/api/v1/accounts/portfolio", headers=H).json()["limits"]["max_asset_concentration_pct"] == 75


def test_portfolio_never_adds_live_and_testnet_funds_together(env):
    c, _ = env
    c.post("/api/v1/accounts/connections", json=BODY, headers=H)  # testnet
    accounts = c.app.state.accounts
    from my_ai_trading.accounts.credentials import Credential, Lifetime, Secret
    accounts.credentials.save(user="local", exchange="fakeex", environment=Environment.LIVE, label="live",
                              lifetime=Lifetime.SESSION, permissions=FakeExchange.perms,
                              credential=Credential(Secret("LIVEKEY-0000"), Secret("live-secret-000")))
    live = c.get("/api/v1/accounts/portfolio", headers=H).json()
    assert live["environment"] == "live" and live["available_environments"] == ["live", "testnet"]
    assert live["total_value_usd"]["value"] == "1234.50" and len(live["connections"]) == 1
    test = c.get("/api/v1/accounts/portfolio?environment=testnet", headers=H).json()
    assert test["environment"] == "testnet" and test["total_value_usd"]["value"] == "1234.50"


def test_a_secret_sent_as_a_json_field_name_is_not_echoed(env):
    c, _ = env
    r = c.post("/api/v1/accounts/connections", json={**BODY, SECRET: "x"}, headers=H)
    assert r.status_code == 422 and SECRET not in r.text and "<field>" in r.text
    r = c.post("/api/v1/accounts/connections", json={**BODY, "api_secret": "has space inside"}, headers=H)
    assert r.status_code == 422 and "has space" not in r.text


def test_an_optional_extension_id_pin_rejects_other_extensions(monkeypatch):
    monkeypatch.setenv("MY_AI_TRADING_EXTENSION_ID", "b" * 32)
    store = Store()
    _, svc = make_exchange()
    app = create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/v1/accounts/connections", headers={**H, "Origin": "chrome-extension://" + "b" * 32}).status_code == 200
    assert c.get("/api/v1/accounts/connections", headers={**H, "Origin": "chrome-extension://" + "a" * 32}).status_code == 403
    monkeypatch.setenv("MY_AI_TRADING_EXTENSION_ID", "not-an-id")
    with pytest.raises(ValueError):
        create_app(service=svc, store=store, copilot=Copilot(store, None), api_token=TOKEN)


def test_adapter_completeness_warnings_reach_the_portfolio(env, monkeypatch):
    c, _ = env
    c.post("/api/v1/accounts/connections", json=BODY, headers=H)
    original = FakeExchange.snapshot

    def with_gap(self, cred, connection_id):
        snap = original(self, cred, connection_id)
        return snap.model_copy(update={"warnings": ["BTC-USD-SWAP (inverse) not included in positions"],
                                       "unavailable": ["margin: not enabled for this key"]})
    monkeypatch.setattr(FakeExchange, "snapshot", with_gap)
    out = c.get("/api/v1/accounts/portfolio?environment=testnet", headers=H).json()
    assert out["completeness_warnings"] == ["fakeex: BTC-USD-SWAP (inverse) not included in positions",
                                            "fakeex: margin: not enabled for this key"]
    assert out["connections"][0]["warnings"] == ["BTC-USD-SWAP (inverse) not included in positions"]


def test_the_vault_is_checked_before_the_key_is_sent_to_the_exchange():
    from my_ai_trading.accounts.credentials import Credential, Lifetime, Secret, VaultUnavailable
    clock, sent = Clock(), []

    class Recording(FakeExchange):
        def permissions(self, cred):
            sent.append(cred.key_hint)
            return self.perms

    def no_vault():
        raise VaultUnavailable("no vault")
    accounts = AccountService(CredentialStore(Store(), persistent_vault=no_vault, clock=clock),
                              adapter_factory=lambda ex, e: Recording(e, clock=clock), clock=clock)
    with pytest.raises(VaultUnavailable):  # ENV-4: the key used to reach the exchange first
        accounts.connect(user="local", exchange="fakeex", environment=Environment.TESTNET,
                         credential=Credential(Secret("FAKEKEY-abcd"), Secret(SECRET)), lifetime=Lifetime.DAYS_7,
                         label="x")
    assert sent == []


def test_a_snapshot_overlapping_a_disconnect_is_not_cached():
    import threading
    from my_ai_trading.accounts.credentials import Credential, Lifetime, Secret
    clock = Clock()
    started, release = threading.Event(), threading.Event()

    class Slow(FakeExchange):
        def snapshot(self, cred, connection_id):
            started.set()
            release.wait(5)
            return super().snapshot(cred, connection_id)

    accounts = AccountService(CredentialStore(Store(), persistent_vault=MemoryVault, clock=clock),
                              adapter_factory=lambda ex, e: Slow(e, clock=clock), clock=clock)
    meta = accounts.connect(user="local", exchange="fakeex", environment=Environment.TESTNET,
                            credential=Credential(Secret("FAKEKEY-abcd"), Secret(SECRET)),
                            lifetime=Lifetime.SESSION, label="x")
    t = threading.Thread(target=accounts.snapshot, args=(meta.connection_id,), kwargs={"user": "local"})
    t.start()
    assert started.wait(5)
    assert accounts.disconnect(meta.connection_id, user="local")
    release.set()
    t.join(5)
    assert meta.connection_id not in accounts._cache  # FLOW-10: private data stayed cached after disconnect
