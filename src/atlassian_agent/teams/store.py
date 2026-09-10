"""Per-user Atlassian credentials for a multi-tenant bot.

Two things are stored, with different lifetimes and blast radii:

* **One client registration** for the whole bot. Dynamic client registration
  identifies *the app*, not a person, so every user shares it.
* **One grant per user**, encrypted at rest, found again by the stable key
  `identity.py` derives from the sender.

`ScopedTokenStorage` glues those to the `TokenStorage` protocol the MCP SDK
expects, so the SDK persists refreshed tokens into the right user's row without
knowing anything about Teams.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from mcp.client.auth import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.shared.auth_utils import calculate_token_expiry

logger = logging.getLogger(__name__)

CACHE_VERSION = 1


def generate_encryption_key() -> str:
    """A fresh key for `TEAMS_TOKEN_ENCRYPTION_KEY`."""
    return Fernet.generate_key().decode()


class UserTokenStore:
    """Encrypted JSON store of per-user grants, plus the shared registration.

    Deliberately a single file so the reference bot runs with no database. The
    interface is narrow on purpose - swap the four `_read`/`_write` calls for
    Postgres, Azure Table or a secrets manager and nothing above changes.
    """

    def __init__(self, path: Path, encryption_key: str) -> None:
        self.path = Path(path).expanduser()
        try:
            self._fernet = Fernet(encryption_key.encode())
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "TEAMS_TOKEN_ENCRYPTION_KEY is not a valid Fernet key. Generate one with:\n"
                '  python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from exc
        self._lock = asyncio.Lock()

    # -- file plumbing ------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {"version": CACHE_VERSION, "users": {}, "nonces": {}}
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(
                "Token store %s is unreadable (%s); refusing to overwrite it.", self.path, exc
            )
            raise
        data.setdefault("users", {})
        data.setdefault("nonces", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _encrypt(self, value: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(value).encode()).decode()

    def _decrypt(self, blob: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._fernet.decrypt(blob.encode()))
        except (InvalidToken, ValueError):
            # A rotated key makes old rows unreadable; treat as "not connected"
            # rather than crashing everyone's next message.
            logger.warning(
                "Could not decrypt a stored grant - the encryption key may have "
                "changed. That user will be asked to reconnect."
            )
            return None

    # -- per-user grants ----------------------------------------------------

    async def get_grant(self, user_key: str) -> dict[str, Any] | None:
        async with self._lock:
            row = self._read()["users"].get(user_key)
        return self._decrypt(row) if isinstance(row, str) else None

    async def set_grant(self, user_key: str, grant: dict[str, Any]) -> None:
        async with self._lock:
            data = self._read()
            data["users"][user_key] = self._encrypt(grant)
            self._write(data)

    async def delete_grant(self, user_key: str) -> bool:
        async with self._lock:
            data = self._read()
            existed = data["users"].pop(user_key, None) is not None
            if existed:
                self._write(data)
            return existed

    # -- shared client registration ----------------------------------------

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        async with self._lock:
            raw = self._read().get("client_info")
        if not isinstance(raw, str):
            return None
        decrypted = self._decrypt(raw)
        if decrypted is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(decrypted)
        except ValueError:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        async with self._lock:
            data = self._read()
            data["client_info"] = self._encrypt(
                client_info.model_dump(mode="json", exclude_none=True)
            )
            self._write(data)

    # -- single-use sign-in nonces -----------------------------------------

    async def consume_nonce(self, nonce: str, *, expires_at: float) -> bool:
        """Record a nonce as spent. False if it was already used.

        This is what makes a sign-in link single-use: a captured link cannot be
        replayed even inside its validity window.
        """
        async with self._lock:
            data = self._read()
            now = time.time()
            nonces = {n: exp for n, exp in data["nonces"].items() if float(exp) > now}
            if nonce in nonces:
                data["nonces"] = nonces
                self._write(data)
                return False
            nonces[nonce] = expires_at
            data["nonces"] = nonces
            self._write(data)
            return True


class ScopedTokenStorage(TokenStorage):
    """The MCP `TokenStorage` protocol, scoped to one Teams user.

    Tokens go to that user's row; the client registration is shared across all
    users of the bot. The SDK writes through this on the initial exchange and
    on every refresh, so nothing else has to persist anything.
    """

    def __init__(self, store: UserTokenStore, user_key: str) -> None:
        self.store = store
        self.user_key = user_key

    async def get_tokens(self) -> OAuthToken | None:
        grant = await self.store.get_grant(self.user_key)
        if not grant or "tokens" not in grant:
            return None
        try:
            return OAuthToken.model_validate(grant["tokens"])
        except ValueError:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        grant = await self.store.get_grant(self.user_key) or {}
        grant["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        grant["expires_at"] = calculate_token_expiry(tokens.expires_in)
        grant["updated_at"] = time.time()
        await self.store.set_grant(self.user_key, grant)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return await self.store.get_client_info()

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self.store.set_client_info(client_info)

    async def get_token_expiry(self) -> float | None:
        """Lets `AtlassianOAuthProvider` refresh instead of re-prompting."""
        grant = await self.store.get_grant(self.user_key)
        expires_at = (grant or {}).get("expires_at")
        return float(expires_at) if isinstance(expires_at, (int, float)) else None
