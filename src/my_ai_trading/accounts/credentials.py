"""Exchange credential lifecycle: session-only by default, optional OS-vault persistence, enforced expiry.

Secrets never touch SQLite, files, logs or API responses. SQLite holds only metadata (exchange, label,
environment, expiry, permissions, the last 4 characters of the key). A persisted secret lives in the OS
credential vault (Secret Service, macOS Keychain, Windows Credential Manager) through `keyring`; plaintext or
null keyring backends are refused rather than used. Expiry is checked before every use and expired or
session-only entries are purged when the app starts. Deleting locally does not revoke the key at the exchange.
"""
from __future__ import annotations

import json
import secrets as _secrets
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict

from .models import Environment, KeyPermissions

VAULT_SERVICE = "my-ai-trading/exchange-credentials"


class Lifetime(StrEnum):
    SESSION = "session"
    HOUR_1 = "1h"
    HOURS_24 = "24h"
    DAYS_7 = "7d"
    DAYS_30 = "30d"
    UNTIL_REMOVED = "until_removed"

    @property
    def seconds(self) -> int | None:
        return {"1h": 3_600, "24h": 86_400, "7d": 7 * 86_400, "30d": 30 * 86_400}.get(self.value)

    @property
    def persistent(self) -> bool:
        return self is not Lifetime.SESSION


class CredentialError(Exception):
    """Safe to show: never contains secret material."""


class CredentialExpired(CredentialError):
    pass


class CredentialNotFound(CredentialError):
    pass


class VaultUnavailable(CredentialError):
    pass


class Secret:
    """A string that will not print itself: repr/str/format show a placeholder, never the value."""
    __slots__ = ("_value",)

    def __init__(self, value: str):
        # Printable ASCII only: exchange keys are ASCII, and anything else would fail inside the HTTP layer with an
        # error that echoes part of the value.
        if not isinstance(value, str) or not value or len(value) > 4096 or not all("!" <= c <= "~" for c in value):
            raise CredentialError("credential fields must be printable ASCII without spaces")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self):  # pickling would write the value somewhere it should not go
        raise TypeError("Secret cannot be pickled")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and _secrets.compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        return hash(("Secret", len(self._value)))


@dataclass(frozen=True, repr=False)
class Credential:
    api_key: Secret
    api_secret: Secret
    passphrase: Secret | None = None  # OKX and others

    def __repr__(self) -> str:
        return f"Credential(api_key=<redacted, ends {self.key_hint}>)"

    @property
    def key_hint(self) -> str:
        return self.api_key.reveal()[-4:]

    def _to_vault(self) -> str:
        return json.dumps({"k": self.api_key.reveal(), "s": self.api_secret.reveal(),
                           "p": self.passphrase.reveal() if self.passphrase else None})

    @classmethod
    def _from_vault(cls, raw: str) -> "Credential":
        d = json.loads(raw)
        return cls(Secret(d["k"]), Secret(d["s"]), Secret(d["p"]) if d.get("p") else None)


class ConnectionMeta(BaseModel):
    """Everything about a connection except the secret. Safe to store and to return to clients."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    connection_id: str
    user_id: str
    exchange: str
    environment: Environment
    label: str
    lifetime: Lifetime
    created_ms: int
    expires_ms: int | None
    key_hint: str
    permissions: KeyPermissions
    trading_enabled: bool = False
    purge_pending: bool = False  # the secret could not be deleted from the vault yet; retried on later starts

    def expired(self, now_ms: int) -> bool:
        return self.expires_ms is not None and now_ms >= self.expires_ms


class Vault(Protocol):
    def put(self, key: str, value: str) -> None: ...
    def get(self, key: str) -> str | None: ...
    def delete(self, key: str) -> None: ...


class MemoryVault:
    """Process memory only: gone when the app exits. Used for session-only credentials (and tests)."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = value

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class KeyringVault:
    """The OS credential vault via `keyring`. Refuses backends that would store secrets in plaintext."""

    # Only the operating systems' own vaults. File-based backends (keyrings.alt, cryptfile), chainers and null/fail
    # backends are refused, so a secret can never silently end up in a file.
    _ALLOWED = ("keyring.backends.SecretService.", "keyring.backends.libsecret.", "keyring.backends.kwallet.",
                "keyring.backends.macOS.", "keyring.backends.Windows.")

    def __init__(self, service: str = VAULT_SERVICE):
        try:
            import keyring  # optional dependency: pip install '.[accounts]'
        except ImportError as exc:
            raise VaultUnavailable("OS credential vault support is not installed (pip install '.[accounts]')") from exc
        backend = keyring.get_keyring()
        name = f"{type(backend).__module__}.{type(backend).__name__}"
        if not name.startswith(self._ALLOWED) or getattr(backend, "priority", 0) <= 0:
            raise VaultUnavailable("no secure OS credential vault is available; use a session-only connection")
        self._keyring, self._service = keyring, service

    def _call(self, fn, *args):
        try:
            return fn(self._service, *args)
        except self._keyring.errors.PasswordDeleteError:
            return None  # already gone
        except self._keyring.errors.KeyringError as exc:  # locked vault, D-Bus failure, ...
            raise VaultUnavailable(f"the OS credential vault is not available ({type(exc).__name__})") from None

    def put(self, key: str, value: str) -> None:
        self._call(self._keyring.set_password, key, value)

    def get(self, key: str) -> str | None:
        return self._call(self._keyring.get_password, key)

    def delete(self, key: str) -> None:
        self._call(self._keyring.delete_password, key)


class MetaStore(Protocol):
    def save_connection(self, meta: ConnectionMeta) -> None: ...
    def connection(self, connection_id: str, user: str) -> ConnectionMeta | None: ...
    def connections(self, user: str) -> list[ConnectionMeta]: ...
    def all_connections(self) -> list[ConnectionMeta]: ...
    def delete_connection(self, connection_id: str) -> None: ...


class CredentialStore:
    """Owns where secrets live and when they stop being usable."""

    def __init__(self, meta: MetaStore, *, persistent_vault: Callable[[], Vault] | None = None,
                 clock: Callable[[], float] = time.time):
        self._meta, self._clock = meta, clock
        self._memory = MemoryVault()
        self._persistent_factory = persistent_vault or KeyringVault
        self._persistent: Vault | None = None
        self._lock = threading.RLock()

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _vault(self, lifetime: Lifetime) -> Vault:
        if not lifetime.persistent:
            return self._memory
        if self._persistent is None:
            self._persistent = self._persistent_factory()  # raises VaultUnavailable: never a weaker fallback
        return self._persistent

    def save(self, *, user: str, exchange: str, environment: Environment, label: str, lifetime: Lifetime,
             credential: Credential, permissions: KeyPermissions) -> ConnectionMeta:
        now = self._now_ms()
        seconds = lifetime.seconds
        meta = ConnectionMeta(connection_id=_secrets.token_hex(8), user_id=user, exchange=exchange,
                              environment=environment, label=label[:60], lifetime=lifetime, created_ms=now,
                              expires_ms=now + seconds * 1000 if seconds else None, key_hint=credential.key_hint,
                              permissions=permissions)
        with self._lock:
            vault = self._vault(lifetime)
            vault.put(meta.connection_id, credential._to_vault())
            try:
                self._meta.save_connection(meta)
            except Exception:
                vault.delete(meta.connection_id)  # never leave an orphaned secret behind
                raise
        return meta

    def get(self, connection_id: str, *, user: str) -> tuple[ConnectionMeta, Credential]:
        """The only way to read a secret. Expiry is enforced here, on every use."""
        with self._lock:
            meta = self._meta.connection(connection_id, user)
            if meta is None or meta.purge_pending:
                raise CredentialNotFound("connection not found")
            if meta.expired(self._now_ms()):
                self._remove(meta)
                raise CredentialExpired("this connection expired and its stored key was deleted; reconnect to continue")
            raw = self._vault(meta.lifetime).get(connection_id)
            if raw is None:
                self._meta.delete_connection(connection_id)  # session ended or the vault entry was removed
                raise CredentialNotFound("the key for this connection is no longer stored; reconnect to continue")
            return meta, Credential._from_vault(raw)

    def update_meta(self, meta: ConnectionMeta) -> None:
        self._meta.save_connection(meta)

    def list(self, *, user: str) -> list[ConnectionMeta]:
        now = self._now_ms()
        out = []
        for meta in self._meta.connections(user):
            if meta.purge_pending or meta.expired(now):
                if not meta.purge_pending:
                    self._remove(meta)
            else:
                out.append(meta)
        return out

    def delete(self, connection_id: str, *, user: str) -> bool:
        with self._lock:
            meta = self._meta.connection(connection_id, user)
            if meta is None:
                return False
            if not self._remove(meta):
                raise VaultUnavailable("the OS credential vault is not available right now, so the key could not be "
                                       "deleted yet; the app will delete it the next time it starts")
            return True

    def purge_on_start(self) -> int:
        """Run when the app starts: session-only entries ended with the last run; expired ones and earlier failed
        deletions are removed. Trading Mode is switched off again, so its consent is asked for again."""
        removed = 0
        now = self._now_ms()
        for meta in self._meta.all_connections():
            if meta.lifetime is Lifetime.SESSION or meta.expired(now) or meta.purge_pending:
                if self._remove(meta):
                    removed += 1
            elif meta.trading_enabled:
                self._meta.save_connection(meta.model_copy(update={"trading_enabled": False}))
        return removed

    def _remove(self, meta: ConnectionMeta) -> bool:
        """Delete the secret, then the metadata. If the vault cannot delete now, keep the metadata marked
        purge_pending so a later start retries; otherwise the secret would stay in the vault with nothing
        pointing to it."""
        try:
            self._vault(meta.lifetime).delete(meta.connection_id)
        except VaultUnavailable:
            if meta.lifetime.persistent:
                self._meta.save_connection(meta.model_copy(update={"purge_pending": True, "trading_enabled": False}))
                return False
        self._meta.delete_connection(meta.connection_id)
        return True


def parse_credential(fields: dict[str, Any]) -> Credential:
    """Build a Credential from request fields; error messages never include the values."""
    try:
        passphrase = fields.get("passphrase")
        return Credential(Secret(fields["api_key"]), Secret(fields["api_secret"]),
                          Secret(passphrase) if passphrase else None)
    except KeyError as exc:
        raise CredentialError(f"missing field {exc.args[0]}") from None
