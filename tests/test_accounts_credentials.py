"""R8 M1: credential lifecycle and key policy. No test touches the real OS keyring."""
import pickle

import pytest

from my_ai_trading.accounts import credentials as C
from my_ai_trading.accounts.credentials import (
    Credential, CredentialExpired, CredentialNotFound, CredentialStore, Lifetime, MemoryVault, Secret, VaultUnavailable,
)
from my_ai_trading.accounts.models import Environment, KeyPermissions
from my_ai_trading.accounts.service import PolicyRefused, check_key_policy
from my_ai_trading.store import Store

KEY, SECRET, PASS = "AKIA-test-key-0001", "very-secret-value-do-not-leak-9f8e7d", "pass-phrase-123"
READ_ONLY = KeyPermissions(read=True, spot_trade=False, derivatives_trade=False, margin_trade=False,
                           withdraw=False, transfer=False)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def cred():
    return Credential(Secret(KEY), Secret(SECRET), Secret(PASS))


def make(store=None, vault=None, clock=None):
    vault = vault if vault is not None else MemoryVault()
    clock = clock or Clock()
    return CredentialStore(store or Store(), persistent_vault=lambda: vault, clock=clock), vault, clock


def save(cs, lifetime=Lifetime.SESSION, user="local"):
    return cs.save(user=user, exchange="binance", environment=Environment.TESTNET, label="t", lifetime=lifetime,
                   credential=cred(), permissions=READ_ONLY)


def test_secret_never_prints_or_pickles():
    c = cred()
    for text in (repr(c), str(c), f"{c}", repr(c.api_secret), str(c.api_secret), f"{c.api_secret}",
                 f"{c.api_secret:>40}", repr([c])):
        assert SECRET not in text and KEY[:-4] not in text
    with pytest.raises(TypeError):
        pickle.dumps(c.api_secret)
    with pytest.raises(C.CredentialError):
        Secret("two\nlines")
    assert c.key_hint == KEY[-4:]


def test_session_credentials_stay_in_memory_and_persistent_ones_use_the_vault():
    cs, vault, _ = make()
    session = save(cs)
    assert vault.get(session.connection_id) is None  # never written to the persistent vault
    persisted = save(cs, Lifetime.DAYS_7)
    assert vault.get(persisted.connection_id) is not None
    for meta in (session, persisted):
        _, got = cs.get(meta.connection_id, user="local")
        assert got.api_secret.reveal() == SECRET and got.passphrase.reveal() == PASS


@pytest.mark.parametrize("lifetime,seconds", [(Lifetime.HOUR_1, 3600), (Lifetime.HOURS_24, 86400),
                                              (Lifetime.DAYS_7, 604800), (Lifetime.DAYS_30, 2592000)])
def test_expiry_is_enforced_on_use_and_deletes_the_key(lifetime, seconds):
    cs, vault, clock = make()
    meta = save(cs, lifetime)
    assert meta.expires_ms == meta.created_ms + seconds * 1000
    clock.t += seconds - 1
    cs.get(meta.connection_id, user="local")  # still valid one second before expiry
    clock.t += 1
    with pytest.raises(CredentialExpired):
        cs.get(meta.connection_id, user="local")
    assert vault.get(meta.connection_id) is None  # the secret is gone, not just hidden
    with pytest.raises(CredentialNotFound):
        cs.get(meta.connection_id, user="local")
    assert cs.list(user="local") == []


def test_until_removed_never_expires_but_can_be_deleted():
    cs, vault, clock = make()
    meta = save(cs, Lifetime.UNTIL_REMOVED)
    assert meta.expires_ms is None
    clock.t += 10 * 365 * 86400
    cs.get(meta.connection_id, user="local")
    assert cs.delete(meta.connection_id, user="local") is True
    assert vault.get(meta.connection_id) is None and cs.list(user="local") == []


def test_purge_on_start_removes_session_and_expired_keys_from_a_previous_run():
    store, vault, clock = Store(), MemoryVault(), Clock()
    first_run, _, _ = make(store, vault, clock)
    session = save(first_run)
    short = save(first_run, Lifetime.HOUR_1)
    keep = save(first_run, Lifetime.DAYS_30)
    clock.t += 7200  # the app was closed for two hours
    second_run, _, _ = make(store, vault, clock)
    assert second_run.purge_on_start() == 2
    ids = [m.connection_id for m in second_run.list(user="local")]
    assert ids == [keep.connection_id]
    assert vault.get(short.connection_id) is None
    with pytest.raises(CredentialNotFound):
        second_run.get(session.connection_id, user="local")


def test_the_database_file_never_contains_the_secret(tmp_path):
    db = tmp_path / "app.db"
    store = Store(db)
    cs, _, _ = make(store)
    save(cs, Lifetime.DAYS_7)
    save(cs)
    store.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("app.db"))
    assert raw  # the metadata really was written
    for needle in (SECRET, PASS, KEY):
        assert needle.encode() not in raw
    assert KEY[-4:].encode() in raw  # only the 4-character hint


def test_no_vault_means_no_persistence_and_no_fallback():
    def unavailable():
        raise VaultUnavailable("no vault")
    store = Store()
    cs = CredentialStore(store, persistent_vault=unavailable, clock=Clock())
    with pytest.raises(VaultUnavailable):
        save(cs, Lifetime.DAYS_7)
    assert store.all_connections() == []
    save(cs)  # session-only still works


def test_keyring_vault_refuses_plaintext_and_null_backends(monkeypatch):
    keyring = pytest.importorskip("keyring")
    for name, priority in (("PlaintextKeyring", 1), ("Keyring", 0)):
        fake = type(name, (), {"priority": priority})
        monkeypatch.setattr(keyring, "get_keyring", lambda fake=fake: fake())
        with pytest.raises(VaultUnavailable):
            C.KeyringVault()


def test_a_failed_metadata_write_leaves_no_orphaned_secret():
    class BrokenStore(Store):
        def save_connection(self, meta):
            raise RuntimeError("disk full")
    vault = MemoryVault()
    cs = CredentialStore(BrokenStore(), persistent_vault=lambda: vault, clock=Clock())
    with pytest.raises(RuntimeError):
        save(cs, Lifetime.DAYS_7)
    assert vault._data == {}


def test_connections_are_isolated_per_user():
    cs, _, _ = make()
    meta = save(cs, user="alice")
    with pytest.raises(CredentialNotFound):
        cs.get(meta.connection_id, user="bob")
    assert cs.list(user="bob") == [] and cs.delete(meta.connection_id, user="bob") is False


@pytest.mark.parametrize("env,perms,ok", [
    (Environment.LIVE, READ_ONLY, True),
    (Environment.LIVE, READ_ONLY.model_copy(update={"spot_trade": True}), False),
    (Environment.LIVE, READ_ONLY.model_copy(update={"derivatives_trade": None}), False),  # unknown = refused
    (Environment.LIVE, READ_ONLY.model_copy(update={"withdraw": True}), False),
    (Environment.LIVE, READ_ONLY.model_copy(update={"transfer": None}), False),
    (Environment.LIVE, KeyPermissions(read=True), False),  # exchange reported nothing about trading
    (Environment.TESTNET, READ_ONLY.model_copy(update={"spot_trade": True, "derivatives_trade": True}), True),
    (Environment.TESTNET, READ_ONLY.model_copy(update={"read": False}), False),
])
def test_live_keys_must_be_confirmed_read_only(env, perms, ok):
    if ok:
        check_key_policy(env, perms)
    else:
        with pytest.raises(PolicyRefused):
            check_key_policy(env, perms)


class FlakyVault(MemoryVault):
    """A persistent vault that can be 'down' (e.g. locked keyring, D-Bus failure)."""
    down = False

    def delete(self, key):
        if self.down:
            raise VaultUnavailable("down")
        super().delete(key)


def test_a_failed_vault_delete_is_retried_on_a_later_start_and_never_orphans_the_secret():
    store, vault, clock = Store(), FlakyVault(), Clock()
    cs, _, _ = make(store, vault, clock)
    meta = save(cs, Lifetime.HOUR_1)
    clock.t += 7200
    vault.down = True
    restart, _, _ = make(store, vault, clock)
    assert restart.purge_on_start() == 0
    assert vault.get(meta.connection_id) is not None  # still there, but tracked:
    assert store.all_connections()[0].purge_pending is True
    assert restart.list(user="local") == []  # hidden and unusable meanwhile
    with pytest.raises(CredentialNotFound):
        restart.get(meta.connection_id, user="local")
    vault.down = False
    again, _, _ = make(store, vault, clock)
    assert again.purge_on_start() == 1
    assert vault.get(meta.connection_id) is None and store.all_connections() == []


def test_disconnect_while_the_vault_is_down_says_so_instead_of_claiming_deletion():
    store, vault, clock = Store(), FlakyVault(), Clock()
    cs, _, _ = make(store, vault, clock)
    meta = save(cs, Lifetime.DAYS_7)
    vault.down = True
    with pytest.raises(VaultUnavailable, match="next time it starts"):
        cs.delete(meta.connection_id, user="local")
    assert store.all_connections()[0].purge_pending is True


def test_only_operating_system_vault_backends_are_accepted(monkeypatch):
    keyring = pytest.importorskip("keyring")

    def backend(module, name="Keyring", priority=5):
        cls = type(name, (), {"priority": priority})
        cls.__module__ = module
        return cls()
    for module in ("keyrings.alt.file", "keyrings.cryptfile.cryptfile", "keyring.backends.chainer",
                   "keyring.backends.fail", "keyring.backends.null"):
        monkeypatch.setattr(keyring, "get_keyring", lambda m=module: backend(m))
        with pytest.raises(VaultUnavailable):
            C.KeyringVault()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend("keyring.backends.SecretService"))
    C.KeyringVault()  # accepted


def test_keyring_errors_become_vault_unavailable(monkeypatch):
    keyring = pytest.importorskip("keyring")
    ok = type("Keyring", (), {"priority": 5})
    ok.__module__ = "keyring.backends.SecretService"
    monkeypatch.setattr(keyring, "get_keyring", lambda: ok())

    def locked(*a):
        raise keyring.errors.KeyringLocked("locked")
    monkeypatch.setattr(keyring, "set_password", locked)
    with pytest.raises(VaultUnavailable, match="KeyringLocked"):
        C.KeyringVault().put("x", "y")


def test_credentials_must_be_printable_ascii():
    for bad in ("snow☃man-key", "with space", "tab\there", "é-key-1234"):
        with pytest.raises(C.CredentialError) as exc:
            Secret(bad)
        assert bad not in str(exc.value)


def test_trading_mode_is_switched_off_at_every_start():
    store, vault, clock = Store(), MemoryVault(), Clock()
    cs, _, _ = make(store, vault, clock)
    meta = save(cs, Lifetime.DAYS_7)
    cs.update_meta(meta.connection_id, user="local", mutate=lambda m: m.model_copy(update={"trading_enabled": True}))
    restart, _, _ = make(store, vault, clock)
    restart.purge_on_start()
    assert restart.list(user="local")[0].trading_enabled is False


def test_any_vault_backend_failure_becomes_vault_unavailable_and_start_still_works(monkeypatch):
    keyring = pytest.importorskip("keyring")
    ok = type("Keyring", (), {"priority": 5})
    ok.__module__ = "keyring.backends.SecretService"
    monkeypatch.setattr(keyring, "get_keyring", lambda: ok())

    def dbus_down(*a):
        raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")
    monkeypatch.setattr(keyring, "delete_password", dbus_down)
    monkeypatch.setattr(keyring, "get_password", dbus_down)
    with pytest.raises(VaultUnavailable, match="RuntimeError"):  # SEC-4 / ENV-5: was an unhandled RuntimeError
        C.KeyringVault().get("x")
    store, clock = Store(), Clock()
    first_run = CredentialStore(store, persistent_vault=MemoryVault, clock=clock)
    meta = first_run.save(user="local", exchange="binance", environment=Environment.TESTNET, label="x",
                          lifetime=Lifetime.HOUR_1, credential=cred(), permissions=KeyPermissions())
    clock.t += 7_200  # expired; the next start must delete it from the vault, which is down
    restart = CredentialStore(store, clock=clock)  # the real KeyringVault, now failing with RuntimeError
    assert restart.purge_on_start() == 0  # starts instead of crashing
    assert store.connection(meta.connection_id, "local").purge_pending  # retried on a later start


def test_get_keyring_failure_is_vault_unavailable(monkeypatch):
    keyring = pytest.importorskip("keyring")

    def broken():
        raise RuntimeError("no D-Bus session")
    monkeypatch.setattr(keyring, "get_keyring", broken)
    with pytest.raises(VaultUnavailable):
        C.KeyringVault()
