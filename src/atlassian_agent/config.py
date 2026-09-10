"""Configuration for the Atlassian MCP agent.

Every value has a working default, so the only thing you strictly need in the
environment is an Anthropic API key. Everything else is here to be overridden
when your Atlassian org restricts scopes, or when port 8901 is already taken.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

# The current Atlassian Rovo MCP Server endpoint. The v1 endpoints still work,
# but the /v1/sse transport is being retired after 2026-06-30, so we speak
# Streamable HTTP to v2.
DEFAULT_MCP_URL = "https://mcp.atlassian.com/v2/mcp"

# Scopes advertised by the server's protected-resource metadata at
# https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp
#
# `offline_access` is the important one: without it the authorization server
# returns no refresh token, and you get pushed back through the browser every
# time the access token expires. The rest is a read/write Jira + Confluence
# working set. Destructive scopes (delete:*, manage:*) are left out so the
# consent screen does not hand an agent the right to delete your issues.
#
# Keeping them out takes deliberate effort: the MCP SDK follows the spec's
# scope selection strategy and would otherwise replace this list with every
# scope the server advertises. See `PinnedScopeClientMetadata` in oauth.py.
# Set ATLASSIAN_OAUTH_SCOPES=auto to opt into that server-chosen behavior.
DEFAULT_SCOPES: tuple[str, ...] = (
    "offline_access",
    "read:me",
    "read:account",
    "read:jira:agent-interface",
    "write:jira:agent-interface",
    "search:jira:agent-interface",
    "read:confluence:agent-interface",
    "write:confluence:agent-interface",
    "search:confluence:agent-interface",
    "search:rovo:agent-interface",
)

DEFAULT_MODEL = "anthropic:claude-opus-5"

# Fixed by default on purpose. The redirect URI is baked into the dynamic
# client registration stored in the token cache, so a port that moves between
# runs would force a re-registration each time.
DEFAULT_CALLBACK_PORT = 8901
DEFAULT_CALLBACK_PATH = "/oauth/callback"

# RFC 8252 section 8.3 recommends native apps use the literal loopback address
# rather than the name "localhost", because "localhost" depends on the host's
# resolver: it commonly resolves to ::1 first, and a client listening only on
# 127.0.0.1 then gets an unreachable redirect. Atlassian accepts either, plus
# any port, so the literal address is the safer default.
DEFAULT_CALLBACK_HOST = "127.0.0.1"

DEFAULT_TOKEN_CACHE = Path.home() / ".atlassian-agent" / "tokens.json"


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_scopes(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    if raw.strip().lower() == "auto":
        # Empty means "ask for whatever the server advertises".
        return ()
    # Accept space- or comma-separated, since both show up in docs.
    return tuple(scope for scope in raw.replace(",", " ").split() if scope)


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one Atlassian MCP connection."""

    mcp_url: str = DEFAULT_MCP_URL
    # An empty tuple means "auto": request whatever the server advertises.
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    model: str = DEFAULT_MODEL
    token_cache_path: Path = DEFAULT_TOKEN_CACHE
    callback_host: str = DEFAULT_CALLBACK_HOST
    callback_port: int = DEFAULT_CALLBACK_PORT
    callback_path: str = DEFAULT_CALLBACK_PATH
    client_name: str = "Atlassian LangChain Agent"
    # How long the OAuth provider waits for you to finish the browser flow.
    auth_timeout: float = 300.0
    # Per-request HTTP timeout, and how long to wait on an idle SSE stream.
    request_timeout: float = 60.0
    sse_read_timeout: float = 300.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def redirect_uri(self) -> str:
        """The loopback URI registered with Atlassian and served by `oauth.py`."""
        host = f"[{self.callback_host}]" if ":" in self.callback_host else self.callback_host
        return f"http://{host}:{self.callback_port}{self.callback_path}"

    @property
    def scope_string(self) -> str:
        return " ".join(self.scopes)

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        """Build settings from environment variables, then apply explicit overrides.

        Overrides win over the environment so callers can pin one field without
        having to reconstruct the rest.
        """
        settings = cls(
            mcp_url=_env_str("ATLASSIAN_MCP_URL", DEFAULT_MCP_URL),
            scopes=_env_scopes("ATLASSIAN_OAUTH_SCOPES", DEFAULT_SCOPES),
            model=_env_str("ATLASSIAN_AGENT_MODEL", DEFAULT_MODEL),
            token_cache_path=Path(
                _env_str("ATLASSIAN_TOKEN_CACHE", str(DEFAULT_TOKEN_CACHE))
            ).expanduser(),
            callback_host=_env_str("ATLASSIAN_OAUTH_CALLBACK_HOST", DEFAULT_CALLBACK_HOST),
            callback_port=_env_int("ATLASSIAN_OAUTH_CALLBACK_PORT", DEFAULT_CALLBACK_PORT),
            callback_path=_env_str("ATLASSIAN_OAUTH_CALLBACK_PATH", DEFAULT_CALLBACK_PATH),
            client_name=_env_str("ATLASSIAN_OAUTH_CLIENT_NAME", "Atlassian LangChain Agent"),
            auth_timeout=_env_float("ATLASSIAN_OAUTH_TIMEOUT", 300.0),
            request_timeout=_env_float("ATLASSIAN_MCP_TIMEOUT", 60.0),
            sse_read_timeout=_env_float("ATLASSIAN_MCP_SSE_READ_TIMEOUT", 300.0),
        )
        if overrides:
            settings = replace(settings, **{k: v for k, v in overrides.items() if v is not None})
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.mcp_url.startswith(("http://", "https://")):
            raise ValueError(f"ATLASSIAN_MCP_URL must be an http(s) URL, got {self.mcp_url!r}")
        if not self.callback_path.startswith("/"):
            raise ValueError(f"callback path must start with '/', got {self.callback_path!r}")
        if not 1 <= self.callback_port <= 65535:
            raise ValueError(f"callback port out of range: {self.callback_port}")
        if not self.callback_host:
            raise ValueError("callback host must not be empty")
        for scope in self.scopes:
            if scope.split() != [scope]:
                raise ValueError(f"scope entries must not contain whitespace: {scope!r}")
