import asyncio

import httpx
import pytest
from tests.test_token_storage import make_token

from atlassian_agent.config import Settings
from atlassian_agent.oauth import (
    AtlassianOAuthProvider,
    LoopbackOAuthFlow,
    ManualPasteFlow,
    OAuthCallbackError,
    PinnedScopeClientMetadata,
    build_client_metadata,
    build_oauth_provider,
    build_token_storage,
    parse_redirect_response,
)


@pytest.fixture
def flow(settings):
    return LoopbackOAuthFlow(settings, open_browser=False)


async def get(url: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=5) as client:
        return await client.get(url, **kwargs)


async def test_callback_delivers_code_and_state(settings, flow):
    await flow.redirect_handler("https://auth.atlassian.com/authorize?whatever")

    async def visit():
        return await get(f"{settings.redirect_uri}?code=the-code&state=the-state")

    response, (code, state) = await asyncio.gather(visit(), flow.callback_handler())

    assert response.status_code == 200
    assert "You're connected" in response.text
    assert (code, state) == ("the-code", "the-state")


async def test_listener_is_released_once_the_code_arrives(settings, flow):
    await flow.redirect_handler("https://auth.atlassian.com/authorize")
    await asyncio.gather(get(f"{settings.redirect_uri}?code=c&state=s"), flow.callback_handler())

    # Nothing should still be listening on the callback port.
    with pytest.raises((ConnectionRefusedError, OSError)):
        _, writer = await asyncio.open_connection("127.0.0.1", settings.callback_port)
        writer.close()


async def test_authorization_error_is_surfaced_with_the_server_reason(settings, flow):
    await flow.redirect_handler("https://auth.atlassian.com/authorize")

    async def visit():
        return await get(
            f"{settings.redirect_uri}?error=invalid_scope"
            "&error_description=Unknown+scope+read%3Anope"
        )

    with pytest.raises(OAuthCallbackError, match="invalid_scope"):
        _, _ = await asyncio.gather(visit(), flow.callback_handler())


async def test_callback_without_a_code_is_an_error(settings, flow):
    await flow.redirect_handler("https://auth.atlassian.com/authorize")

    with pytest.raises(OAuthCallbackError, match="without an authorization code"):
        await asyncio.gather(get(f"{settings.redirect_uri}?state=s"), flow.callback_handler())


async def test_stray_browser_requests_do_not_resolve_the_login(settings, flow):
    """A favicon fetch must not be mistaken for the authorization callback."""
    await flow.redirect_handler("https://auth.atlassian.com/authorize")

    stray = await get(f"http://localhost:{settings.callback_port}/favicon.ico")
    assert stray.status_code == 404

    response, (code, _) = await asyncio.gather(
        get(f"{settings.redirect_uri}?code=real&state=s"), flow.callback_handler()
    )
    assert response.status_code == 200
    assert code == "real"


async def test_timeout_explains_how_to_extend_it(settings):
    from dataclasses import replace

    flow = LoopbackOAuthFlow(replace(settings, auth_timeout=0.25), open_browser=False)
    await flow.redirect_handler("https://auth.atlassian.com/authorize")

    with pytest.raises(OAuthCallbackError, match="ATLASSIAN_OAUTH_TIMEOUT"):
        await flow.callback_handler()


async def test_busy_port_names_the_setting_to_change(settings, flow):
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", settings.callback_port)
    try:
        with pytest.raises(OAuthCallbackError, match="ATLASSIAN_OAUTH_CALLBACK_PORT"):
            await flow.redirect_handler("https://auth.atlassian.com/authorize")
    finally:
        blocker.close()
        await blocker.wait_closed()


async def test_provider_restores_expiry_so_a_stale_token_refreshes(settings):
    """Without this the SDK treats any cached token as valid until a 401."""
    storage = build_token_storage(settings)
    await storage.set_tokens(make_token(expires_in=-10))  # already expired
    await storage.set_client_info(
        __import__("mcp.shared.auth", fromlist=["x"]).OAuthClientInformationFull(
            client_id="c", redirect_uris=[settings.redirect_uri]
        )
    )

    provider = build_oauth_provider(settings, open_browser=False)
    await provider._initialize()

    assert isinstance(provider, AtlassianOAuthProvider)
    assert provider.context.token_expiry_time is not None
    assert not provider.context.is_token_valid()
    # A refresh token is present, so the SDK takes the silent refresh path.
    assert provider.context.can_refresh_token()


async def test_provider_marks_a_fresh_token_valid(settings):
    storage = build_token_storage(settings)
    await storage.set_tokens(make_token(expires_in=3600))

    provider = build_oauth_provider(settings, open_browser=False)
    await provider._initialize()

    assert provider.context.is_token_valid()


async def test_provider_is_configured_as_a_pkce_public_client(settings):
    provider = build_oauth_provider(settings, open_browser=False)
    metadata = provider.context.client_metadata

    assert metadata.token_endpoint_auth_method == "none"
    assert "refresh_token" in metadata.grant_types
    assert str(metadata.redirect_uris[0]) == settings.redirect_uri
    assert "offline_access" in (metadata.scope or "")


# -- scope pinning ----------------------------------------------------------


def test_configured_scopes_survive_the_sdk_scope_selection_step(settings):
    """The SDK replaces client_metadata.scope during the 401 flow.

    Left alone it would swap our least-privilege list for every scope the
    Atlassian server advertises - including delete:jira and manage:jira - and
    that widened value is what reaches both registration and the consent
    screen. Pinning keeps the configured set.
    """
    metadata = build_client_metadata(settings)
    assert isinstance(metadata, PinnedScopeClientMetadata)
    original = metadata.scope

    # Exactly what mcp.client.auth.oauth2 does at "Step 3: Apply scope
    # selection strategy".
    metadata.scope = " ".join(
        ["read:me", "delete:jira:agent-interface", "manage:jira:agent-interface"]
    )

    assert metadata.scope == original
    assert "delete:jira:agent-interface" not in metadata.scope
    assert metadata.model_dump(exclude_none=True)["scope"] == original


def test_auto_scopes_defer_to_the_server(tmp_path):
    settings = Settings(token_cache_path=tmp_path / "t.json", scopes=())
    metadata = build_client_metadata(settings)

    assert not isinstance(metadata, PinnedScopeClientMetadata)
    assert metadata.scope is None  # the SDK fills this in from server metadata


def test_other_metadata_fields_are_still_writable(settings):
    metadata = build_client_metadata(settings)
    metadata.client_name = "renamed"
    assert metadata.client_name == "renamed"


# -- custom hosting of the browser step --------------------------------------


async def test_custom_handlers_replace_the_loopback_flow(settings):
    async def redirect(url: str) -> None:
        return None

    async def callback() -> tuple[str, str | None]:
        return ("code", "state")

    provider = build_oauth_provider(settings, redirect_handler=redirect, callback_handler=callback)
    assert provider.context.redirect_handler is redirect
    assert provider.context.callback_handler is callback


def test_half_a_custom_flow_is_rejected(settings):
    async def redirect(url: str) -> None:
        return None

    with pytest.raises(ValueError, match="must be supplied together"):
        build_oauth_provider(settings, redirect_handler=redirect)


# -- paste-back flow (no listening socket) -----------------------------------


@pytest.mark.parametrize(
    "pasted",
    [
        "http://127.0.0.1:8901/oauth/callback?code=abc&state=xyz",
        "http://localhost:8901/oauth/callback?code=abc&state=xyz#",
        "code=abc&state=xyz",
    ],
)
def test_parse_accepts_the_shapes_people_actually_paste(pasted):
    assert parse_redirect_response(pasted) == ("abc", "xyz")


def test_parse_reports_an_authorization_error():
    with pytest.raises(OAuthCallbackError, match="invalid_scope: Unknown scope"):
        parse_redirect_response(
            "http://127.0.0.1:8901/cb?error=invalid_scope&error_description=Unknown+scope"
        )


def test_parse_rejects_a_bare_code_without_state():
    """Dropping state would defeat the CSRF check the SDK performs."""
    with pytest.raises(OAuthCallbackError, match="no `state`"):
        parse_redirect_response("http://127.0.0.1:8901/cb?code=abc")


@pytest.mark.parametrize("pasted", ["", "   ", "not a url"])
def test_parse_rejects_junk_with_instructions(pasted):
    with pytest.raises(OAuthCallbackError):
        parse_redirect_response(pasted)


async def test_paste_flow_needs_no_socket(settings, capsys):
    """The whole point: authorize with nothing listening on the callback port."""
    flow = ManualPasteFlow(
        settings, reader=lambda _: "http://127.0.0.1:8901/oauth/callback?code=c1&state=s1"
    )

    await flow.redirect_handler("https://auth.atlassian.com/authorize?x=1")
    assert "https://auth.atlassian.com/authorize?x=1" in capsys.readouterr().out

    assert await flow.callback_handler() == ("c1", "s1")

    # Nothing was ever bound.
    with pytest.raises((ConnectionRefusedError, OSError)):
        _, writer = await asyncio.open_connection("127.0.0.1", settings.callback_port)
        writer.close()


def test_manual_paste_selects_the_paste_flow(settings):
    provider = build_oauth_provider(settings, manual_paste=True)
    assert provider.context.callback_handler.__self__.__class__ is ManualPasteFlow


def test_loopback_is_still_the_default(settings):
    provider = build_oauth_provider(settings, open_browser=False)
    assert provider.context.callback_handler.__self__.__class__ is LoopbackOAuthFlow
