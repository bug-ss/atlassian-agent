import pytest

from atlassian_agent.config import DEFAULT_MCP_URL, DEFAULT_MODEL, Settings


def test_defaults_target_the_v2_endpoint_and_request_offline_access():
    settings = Settings.from_env()
    assert settings.mcp_url == DEFAULT_MCP_URL == "https://mcp.atlassian.com/v2/mcp"
    assert settings.model == DEFAULT_MODEL
    # Without offline_access there is no refresh token, and the browser flow
    # would repeat on every expiry.
    assert "offline_access" in settings.scopes


def test_redirect_uri_is_built_from_port_and_path():
    settings = Settings(callback_port=9000, callback_path="/cb")
    assert settings.redirect_uri == "http://localhost:9000/cb"


@pytest.mark.parametrize("raw", ["a b c", "a,b,c", " a , b ,c "])
def test_scopes_accept_commas_or_spaces(monkeypatch, raw):
    monkeypatch.setenv("ATLASSIAN_OAUTH_SCOPES", raw)
    assert Settings.from_env().scopes == ("a", "b", "c")


def test_env_is_read_and_overrides_win(monkeypatch):
    monkeypatch.setenv("ATLASSIAN_AGENT_MODEL", "anthropic:claude-sonnet-5")
    monkeypatch.setenv("ATLASSIAN_OAUTH_CALLBACK_PORT", "9999")
    assert Settings.from_env().model == "anthropic:claude-sonnet-5"
    assert Settings.from_env().callback_port == 9999
    assert Settings.from_env(model="anthropic:claude-opus-5").model == "anthropic:claude-opus-5"


def test_none_overrides_do_not_clobber_env(monkeypatch):
    monkeypatch.setenv("ATLASSIAN_AGENT_MODEL", "anthropic:claude-sonnet-5")
    assert Settings.from_env(model=None).model == "anthropic:claude-sonnet-5"


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ATLASSIAN_MCP_URL", "   ")
    assert Settings.from_env().mcp_url == DEFAULT_MCP_URL


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mcp_url": "ftp://example.com"},
        {"callback_path": "oauth"},
        {"callback_port": 0},
        {"callback_port": 70000},
        {"scopes": ("read:me write:me",)},
    ],
)
def test_validate_rejects_bad_configuration(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs).validate()


def test_auto_means_take_whatever_the_server_advertises(monkeypatch):
    monkeypatch.setenv("ATLASSIAN_OAUTH_SCOPES", "auto")
    settings = Settings.from_env()
    assert settings.scopes == ()
    assert settings.scope_string == ""
    settings.validate()  # empty is legal; it selects the SDK's default strategy


def test_non_integer_port_is_reported_clearly(monkeypatch):
    monkeypatch.setenv("ATLASSIAN_OAUTH_CALLBACK_PORT", "eight")
    with pytest.raises(ValueError, match="must be an integer"):
        Settings.from_env()
