import json
import os
import stat
import time

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from atlassian_agent.config import Settings
from atlassian_agent.oauth import build_token_storage, registration_fingerprint, token_status


def make_token(**overrides) -> OAuthToken:
    payload = {
        "access_token": "at-123",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "rt-456",
        "scope": "read:me offline_access",
    }
    payload.update(overrides)
    return OAuthToken(**payload)


def make_client_info() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="client-abc",
        redirect_uris=["http://localhost:8901/oauth/callback"],
    )


async def test_round_trips_tokens_and_client_info(settings):
    storage = build_token_storage(settings)
    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None

    await storage.set_tokens(make_token())
    await storage.set_client_info(make_client_info())

    reloaded = build_token_storage(settings)
    tokens = await reloaded.get_tokens()
    assert tokens is not None
    assert tokens.access_token == "at-123"
    assert tokens.refresh_token == "rt-456"
    client_info = await reloaded.get_client_info()
    assert client_info is not None and client_info.client_id == "client-abc"


async def test_token_file_is_not_readable_by_others(settings):
    storage = build_token_storage(settings)
    await storage.set_tokens(make_token())
    mode = stat.S_IMODE(os.stat(storage.path).st_mode)
    assert mode == 0o600, f"token cache is mode {mode:o}"


async def test_expiry_is_persisted_so_a_restart_can_refresh_silently(settings):
    storage = build_token_storage(settings)
    before = time.time()
    await storage.set_tokens(make_token(expires_in=3600))

    expiry = await build_token_storage(settings).get_token_expiry()
    assert expiry is not None
    assert before + 3599 <= expiry <= time.time() + 3601


async def test_missing_expires_in_yields_no_expiry(settings):
    storage = build_token_storage(settings)
    await storage.set_tokens(make_token(expires_in=None))
    assert await storage.get_token_expiry() is None


async def test_changed_scopes_invalidate_the_cached_login(settings):
    await build_token_storage(settings).set_tokens(make_token())

    narrowed = Settings(
        token_cache_path=settings.token_cache_path,
        callback_port=settings.callback_port,
        scopes=("read:me",),
    )
    assert registration_fingerprint(narrowed) != registration_fingerprint(settings)
    # The stale entry is ignored rather than replayed against a registration
    # that no longer covers these scopes.
    assert await build_token_storage(narrowed).get_tokens() is None
    # ...and the original settings still see their own entry.
    assert await build_token_storage(settings).get_tokens() is not None


async def test_changed_redirect_port_invalidates_the_cached_login(settings):
    await build_token_storage(settings).set_tokens(make_token())
    moved = Settings(
        token_cache_path=settings.token_cache_path,
        callback_port=settings.callback_port + 1,
    )
    assert await build_token_storage(moved).get_tokens() is None


async def test_two_servers_coexist_in_one_cache_file(settings):
    other = Settings(
        token_cache_path=settings.token_cache_path,
        callback_port=settings.callback_port,
        mcp_url="https://mcp.atlassian.com/v1/mcp",
    )
    await build_token_storage(settings).set_tokens(make_token(access_token="v2"))
    await build_token_storage(other).set_tokens(make_token(access_token="v1"))

    assert (await build_token_storage(settings).get_tokens()).access_token == "v2"
    assert (await build_token_storage(other).get_tokens()).access_token == "v1"


async def test_clear_removes_only_the_targeted_server(settings):
    other = Settings(
        token_cache_path=settings.token_cache_path,
        callback_port=settings.callback_port,
        mcp_url="https://mcp.atlassian.com/v1/mcp",
    )
    await build_token_storage(settings).set_tokens(make_token())
    await build_token_storage(other).set_tokens(make_token())

    build_token_storage(settings).clear()

    assert await build_token_storage(settings).get_tokens() is None
    assert await build_token_storage(other).get_tokens() is not None


async def test_clear_on_an_empty_cache_is_a_no_op(settings):
    build_token_storage(settings).clear()  # must not raise


@pytest.mark.parametrize("content", ["not json at all", '{"unexpected": true}', ""])
async def test_corrupt_cache_costs_one_login_not_a_crash(settings, content):
    settings.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.token_cache_path.write_text(content, encoding="utf-8")

    storage = build_token_storage(settings)
    assert await storage.get_tokens() is None

    # And it recovers: writing works over the top of the damaged file.
    await storage.set_tokens(make_token())
    assert (await build_token_storage(settings).get_tokens()).access_token == "at-123"


async def test_malformed_token_entry_is_discarded(settings):
    storage = build_token_storage(settings)
    await storage.set_tokens(make_token())
    data = json.loads(settings.token_cache_path.read_text())
    data["servers"][settings.mcp_url]["tokens"] = {"token_type": "Bearer"}  # no access_token
    settings.token_cache_path.write_text(json.dumps(data), encoding="utf-8")

    assert await build_token_storage(settings).get_tokens() is None


async def test_token_status_reports_what_the_cli_prints(settings):
    assert token_status(settings) == {"authorized": False}

    await build_token_storage(settings).set_tokens(make_token())
    await build_token_storage(settings).set_client_info(make_client_info())

    status = token_status(settings)
    assert status["authorized"] is True
    assert status["client_id"] == "client-abc"
    assert status["has_refresh_token"] is True
    assert 3500 < status["expires_in_seconds"] <= 3600
