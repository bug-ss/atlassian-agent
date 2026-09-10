"""Configuration for the Teams-hosted agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings

DEFAULT_CALLBACK_PATH = "/oauth/atlassian/callback"
DEFAULT_MESSAGES_PATH = "/api/messages"
DEFAULT_SIGN_IN_TTL = 300.0


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is required to run the Teams bot. See README section "
            f"'Running it in Microsoft Teams' for how to generate it."
        )
    return value


@dataclass(frozen=True)
class TeamsSettings:
    """Everything the bot needs beyond the Atlassian settings."""

    public_base_url: str
    state_secret: str
    token_encryption_key: str
    token_store_path: Path = Path("./data/user-tokens.json")
    callback_path: str = DEFAULT_CALLBACK_PATH
    messages_path: str = DEFAULT_MESSAGES_PATH
    sign_in_ttl_seconds: float = DEFAULT_SIGN_IN_TTL
    # Restrict the bot to your own Microsoft Entra tenant(s). Empty means any
    # tenant that can reach the endpoint, which is rarely what you want.
    allowed_tenant_ids: tuple[str, ...] = ()
    # Append "acting as <name>'s Atlassian account" to answers, so a group chat
    # can see whose permissions produced a result.
    show_attribution: bool = True
    port: int = 3978
    host: str = "0.0.0.0"  # noqa: S104 - a container binds all interfaces
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.callback_path}"

    def atlassian_settings(self) -> Settings:
        """Atlassian settings pointed at this bot's public callback route."""
        return Settings.from_env(redirect_uri_override=self.redirect_uri)

    @classmethod
    def from_env(cls) -> TeamsSettings:
        tenants = os.environ.get("TEAMS_ALLOWED_TENANT_IDS", "")
        return cls(
            public_base_url=_require("TEAMS_PUBLIC_BASE_URL"),
            state_secret=_require("TEAMS_STATE_SECRET"),
            token_encryption_key=_require("TEAMS_TOKEN_ENCRYPTION_KEY"),
            token_store_path=Path(
                os.environ.get("TEAMS_TOKEN_STORE_PATH", "./data/user-tokens.json")
            ).expanduser(),
            callback_path=os.environ.get("TEAMS_CALLBACK_PATH", DEFAULT_CALLBACK_PATH),
            messages_path=os.environ.get("TEAMS_MESSAGES_PATH", DEFAULT_MESSAGES_PATH),
            sign_in_ttl_seconds=float(
                os.environ.get("TEAMS_SIGN_IN_TTL_SECONDS", DEFAULT_SIGN_IN_TTL)
            ),
            allowed_tenant_ids=tuple(t.strip() for t in tenants.replace(",", " ").split() if t),
            show_attribution=os.environ.get("TEAMS_SHOW_ATTRIBUTION", "1").lower()
            not in {"0", "false", "no"},
            port=int(os.environ.get("PORT", "3978")),
            host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104
        )
