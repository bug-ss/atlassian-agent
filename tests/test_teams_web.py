"""The bot's HTTP surface: sign-in start, OAuth callback, messaging endpoint.

Needs the `teams` extra (Python 3.12+); skipped otherwise.
"""

import os

import pytest

# Guard on every package the app imports, not just one - a partial install
# should skip these tests rather than fail them.
pytest.importorskip("microsoft_agents.hosting.aiohttp", reason="requires the 'teams' extra")
pytest.importorskip("microsoft_agents.authentication.msal", reason="requires the 'teams' extra")
pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from atlassian_agent.teams.config import TeamsSettings  # noqa: E402
from atlassian_agent.teams.identity import TeamsUser, issue_sign_in_state  # noqa: E402
from atlassian_agent.teams.store import generate_encryption_key  # noqa: E402

ALICE = TeamsUser(key="aad:t1:alice", display_name="Alice", channel_user_id="29:a")


@pytest.fixture
def teams_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMS_PUBLIC_BASE_URL", "https://bot.example.com")
    monkeypatch.setenv("TEAMS_STATE_SECRET", "test-signing-secret")
    monkeypatch.setenv("TEAMS_TOKEN_ENCRYPTION_KEY", generate_encryption_key())
    monkeypatch.setenv("TEAMS_TOKEN_STORE_PATH", str(tmp_path / "users.json"))
    # Bot Framework credentials the SDK reads for itself.
    prefix = "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__"
    for suffix, value in {
        "AUTHTYPE": "ClientSecret",
        "CLIENTID": "00000000-0000-0000-0000-000000000001",
        "CLIENTSECRET": "dummy",
        "TENANTID": "00000000-0000-0000-0000-000000000002",
    }.items():
        monkeypatch.setenv(prefix + suffix, value)
    return TeamsSettings.from_env()


@pytest.fixture
async def client(teams_settings):
    from atlassian_agent.teams.app import build_app

    test_client = TestClient(TestServer(build_app(teams_settings)))
    await test_client.start_server()
    yield test_client
    await test_client.close()


async def test_health_check(client):
    response = await client.get("/healthz")
    assert response.status == 200
    assert (await response.json())["status"] == "ok"


async def test_the_messaging_endpoint_rejects_unauthenticated_callers(client):
    """Only Azure Bot Service may post activities, proven by a signed JWT."""
    response = await client.post("/api/messages", json={"type": "message", "text": "hi"})
    assert response.status == 401


async def test_a_forged_sign_in_link_is_refused(client):
    response = await client.get("/oauth/atlassian/start?t=obviously-not-signed")
    assert response.status == 400


async def test_a_link_signed_with_another_secret_is_refused(client):
    forged = issue_sign_in_state("attacker-secret", ALICE, ttl_seconds=300)
    response = await client.get(f"/oauth/atlassian/start?t={forged}")
    assert response.status == 400


async def test_an_expired_link_says_so(client, teams_settings):
    stale = issue_sign_in_state(teams_settings.state_secret, ALICE, ttl_seconds=-5)
    response = await client.get(f"/oauth/atlassian/start?t={stale}")
    assert response.status == 400
    assert "expired" in (await response.text()).lower()


async def test_the_callback_reports_a_refusal_from_atlassian(client):
    response = await client.get(
        "/oauth/atlassian/callback?error=invalid_scope&error_description=Unknown+scope"
    )
    assert response.status == 400
    body = await response.text()
    assert "invalid_scope" in body and "Unknown scope" in body


async def test_the_callback_rejects_an_unknown_state(client):
    """A code with no in-flight login behind it must not be exchanged."""
    response = await client.get("/oauth/atlassian/callback?code=abc&state=never-issued")
    assert response.status == 400
    assert "no longer active" in (await response.text())


async def test_the_callback_needs_both_code_and_state(client):
    response = await client.get("/oauth/atlassian/callback?code=abc")
    assert response.status == 400


@pytest.mark.skipif(
    os.environ.get("ATLASSIAN_LIVE_TESTS") != "1",
    reason="set ATLASSIAN_LIVE_TESTS=1 to exercise the real Atlassian handshake",
)
async def test_a_valid_link_redirects_to_atlassian(client, teams_settings):
    from urllib.parse import parse_qs, urlsplit

    token = issue_sign_in_state(teams_settings.state_secret, ALICE, ttl_seconds=300)
    response = await client.get(f"/oauth/atlassian/start?t={token}", allow_redirects=False)

    assert response.status == 302
    target = urlsplit(response.headers["Location"])
    params = parse_qs(target.query)
    assert target.netloc == "auth.atlassian.com"
    assert params["redirect_uri"] == [teams_settings.redirect_uri]
    assert params["code_challenge_method"] == ["S256"]
    assert "delete:jira:agent-interface" not in params["scope"][0]

    replayed = await client.get(f"/oauth/atlassian/start?t={token}", allow_redirects=False)
    assert replayed.status == 400
