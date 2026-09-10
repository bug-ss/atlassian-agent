"""Per-user credential isolation, encryption, and single-use sign-in nonces."""

import json
import os
import stat

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from atlassian_agent.teams.store import (
    ScopedTokenStorage,
    UserTokenStore,
    generate_encryption_key,
)

ALICE = "aad:t1:alice"
BOB = "aad:t1:bob"


@pytest.fixture
def store(tmp_path):
    return UserTokenStore(tmp_path / "users.json", generate_encryption_key())


def token(access_token="tok", **kw):
    return OAuthToken(access_token=access_token, expires_in=kw.pop("expires_in", 3600), **kw)


async def test_each_user_gets_their_own_grant(store):
    alice, bob = ScopedTokenStorage(store, ALICE), ScopedTokenStorage(store, BOB)
    await alice.set_tokens(token("alice-token"))
    await bob.set_tokens(token("bob-token"))

    assert (await alice.get_tokens()).access_token == "alice-token"
    assert (await bob.get_tokens()).access_token == "bob-token"


async def test_an_unconnected_user_has_nothing(store):
    assert await ScopedTokenStorage(store, "aad:t1:nobody").get_tokens() is None


async def test_tokens_are_encrypted_at_rest(store):
    await ScopedTokenStorage(store, ALICE).set_tokens(token("super-secret-token"))
    raw = store.path.read_text()

    assert "super-secret-token" not in raw
    assert json.loads(raw)["users"][ALICE]  # present, just unreadable


async def test_store_is_not_world_readable(store):
    await ScopedTokenStorage(store, ALICE).set_tokens(token())
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


async def test_a_rotated_key_asks_for_reconnect_rather_than_crashing(tmp_path):
    path = tmp_path / "users.json"
    await ScopedTokenStorage(UserTokenStore(path, generate_encryption_key()), ALICE).set_tokens(
        token()
    )
    # Same file, different key - as if TEAMS_TOKEN_ENCRYPTION_KEY was rotated.
    rotated = ScopedTokenStorage(UserTokenStore(path, generate_encryption_key()), ALICE)
    assert await rotated.get_tokens() is None


async def test_client_registration_is_shared_across_users(store):
    """DCR identifies the app, not a person - one registration serves everyone."""
    info = OAuthClientInformationFull(
        client_id="app-client", redirect_uris=["https://bot.example.com/oauth/atlassian/callback"]
    )
    await ScopedTokenStorage(store, ALICE).set_client_info(info)

    from_bob = await ScopedTokenStorage(store, BOB).get_client_info()
    assert from_bob is not None and from_bob.client_id == "app-client"


async def test_expiry_is_persisted_for_silent_refresh(store):
    alice = ScopedTokenStorage(store, ALICE)
    await alice.set_tokens(token(expires_in=3600))
    assert await alice.get_token_expiry() is not None


async def test_disconnect_removes_only_that_user(store):
    await ScopedTokenStorage(store, ALICE).set_tokens(token())
    await ScopedTokenStorage(store, BOB).set_tokens(token())

    assert await store.delete_grant(ALICE) is True
    assert await ScopedTokenStorage(store, ALICE).get_tokens() is None
    assert await ScopedTokenStorage(store, BOB).get_tokens() is not None


async def test_disconnecting_twice_is_harmless(store):
    assert await store.delete_grant(ALICE) is False


async def test_a_sign_in_nonce_works_exactly_once(store):
    assert await store.consume_nonce("nonce-1", expires_at=9e9) is True
    assert await store.consume_nonce("nonce-1", expires_at=9e9) is False


async def test_expired_nonces_are_swept(store):
    await store.consume_nonce("old", expires_at=1.0)
    await store.consume_nonce("new", expires_at=9e9)
    assert "old" not in json.loads(store.path.read_text())["nonces"]


async def test_a_corrupt_store_is_never_silently_overwritten(tmp_path):
    """Losing every user's grant to a parse error would be worse than failing."""
    path = tmp_path / "users.json"
    path.write_text("{ not json", encoding="utf-8")
    store = UserTokenStore(path, generate_encryption_key())

    with pytest.raises(json.JSONDecodeError):
        await ScopedTokenStorage(store, ALICE).get_tokens()


def test_a_bad_encryption_key_is_explained(tmp_path):
    with pytest.raises(RuntimeError, match="TEAMS_TOKEN_ENCRYPTION_KEY"):
        UserTokenStore(tmp_path / "u.json", "not-a-fernet-key")
