"""Per-user Atlassian access: agents, providers, and the sign-in handshake.

Two rules drive the design.

**One provider instance per user, reused.** The MCP SDK serializes refreshes on
a lock held by the provider's context. Building a fresh provider per message
would let two concurrent messages from the same person refresh independently,
and if Atlassian rotates the refresh token the loser persists a token the
server has already invalidated. Caching the provider makes that lock do its job.

**Never prompt from inside a message.** At message time the handlers raise
`NotConnected` instead of opening a browser nobody is looking at, so an
unconnected user gets a sign-in card rather than a request that hangs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..agent import AtlassianAgent, create_atlassian_agent
from ..config import Settings
from ..errors import explain
from ..mcp_client import SERVER_NAME, build_mcp_client
from ..oauth import build_oauth_provider
from .config import TeamsSettings
from .identity import TeamsUser
from .store import ScopedTokenStorage, UserTokenStore

logger = logging.getLogger(__name__)

# Rebuild a user's agent occasionally so tool changes on the Atlassian side
# get picked up without restarting the bot.
AGENT_TTL_SECONDS = 900.0


class NotConnected(Exception):
    """This user has no usable Atlassian grant; they need to sign in."""


class LoginFailed(Exception):
    """The sign-in handshake could not be completed."""


@dataclass
class _PendingLogin:
    user: TeamsUser
    authorization_url: asyncio.Future[str]
    code: asyncio.Future[tuple[str, str | None]]
    task: asyncio.Task[None] | None = None
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class _CachedAgent:
    agent: AtlassianAgent
    created_at: float


class AtlassianAccess:
    """Owns everything per-user: providers, agents and in-flight logins."""

    def __init__(
        self,
        settings: Settings,
        teams_settings: TeamsSettings,
        store: UserTokenStore,
    ) -> None:
        self.settings = settings
        self.teams_settings = teams_settings
        self.store = store
        self._providers: dict[str, Any] = {}
        self._agents: dict[str, _CachedAgent] = {}
        self._pending: dict[str, _PendingLogin] = {}
        self._build_locks: dict[str, asyncio.Lock] = {}

    # -- providers ----------------------------------------------------------

    def _provider_for(self, user_key: str, *, interactive_flow: _PendingLogin | None = None):
        """A cached, non-prompting provider for this user.

        `interactive_flow` is passed only during an explicit sign-in, and is
        never cached - a message-time provider must never be able to prompt.
        """
        if interactive_flow is not None:
            return build_oauth_provider(
                self.settings,
                storage=ScopedTokenStorage(self.store, user_key),
                redirect_handler=_make_redirect_handler(interactive_flow),
                callback_handler=_make_callback_handler(
                    interactive_flow, self.teams_settings.sign_in_ttl_seconds
                ),
            )

        provider = self._providers.get(user_key)
        if provider is None:
            provider = build_oauth_provider(
                self.settings,
                storage=ScopedTokenStorage(self.store, user_key),
                redirect_handler=_refuse_interactive,
                callback_handler=_refuse_interactive,
            )
            self._providers[user_key] = provider
        return provider

    # -- agents -------------------------------------------------------------

    async def agent_for(self, user: TeamsUser) -> AtlassianAgent:
        """The agent that acts as this user. Raises `NotConnected` if unlinked."""
        if not await self.is_connected(user):
            raise NotConnected(user.key)

        cached = self._agents.get(user.key)
        if cached and (time.monotonic() - cached.created_at) < AGENT_TTL_SECONDS:
            return cached.agent

        lock = self._build_locks.setdefault(user.key, asyncio.Lock())
        async with lock:
            cached = self._agents.get(user.key)
            if cached and (time.monotonic() - cached.created_at) < AGENT_TTL_SECONDS:
                return cached.agent
            agent = await create_atlassian_agent(self.settings, auth=self._provider_for(user.key))
            self._agents[user.key] = _CachedAgent(agent=agent, created_at=time.monotonic())
            return agent

    async def is_connected(self, user: TeamsUser) -> bool:
        return await ScopedTokenStorage(self.store, user.key).get_tokens() is not None

    async def disconnect(self, user: TeamsUser) -> bool:
        self._providers.pop(user.key, None)
        self._agents.pop(user.key, None)
        return await self.store.delete_grant(user.key)

    # -- sign-in handshake --------------------------------------------------

    async def begin_login(self, user: TeamsUser) -> str:
        """Kick off the OAuth flow and return the URL to send the user to.

        The flow runs as a background task parked on `code`, which the callback
        route resolves. We correlate the two by the `state` the MCP SDK puts in
        the authorization URL - the SDK owns that parameter and validates it,
        so we read it rather than inventing a second one.
        """
        loop = asyncio.get_running_loop()
        pending = _PendingLogin(
            user=user, authorization_url=loop.create_future(), code=loop.create_future()
        )
        pending.task = asyncio.create_task(self._drive_login(user, pending))

        done, _ = await asyncio.wait(
            [pending.authorization_url, pending.task],
            timeout=60,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if pending.authorization_url.done():
            url = pending.authorization_url.result()
            state = _state_from_url(url)
            if not state:
                pending.task.cancel()
                raise LoginFailed("Atlassian's authorization URL carried no state parameter.")
            self._pending[state] = pending
            return url

        if pending.task in done:  # finished without ever needing a browser
            pending.task.result()
            raise LoginFailed(
                "Atlassian did not ask for authorization; you may already be connected."
            )
        pending.task.cancel()
        raise LoginFailed("Timed out preparing the Atlassian sign-in link.")

    async def complete_login(self, state: str, code: str) -> TeamsUser:
        """Hand the authorization code to the waiting flow and let it finish."""
        pending = self._pending.pop(state, None)
        if pending is None:
            raise LoginFailed(
                "This sign-in link is no longer active. It may have been used already, "
                "or the bot restarted. Ask the bot to connect again."
            )
        if not pending.code.done():
            pending.code.set_result((code, state))
        try:
            await asyncio.wait_for(pending.task, timeout=60)
        except TimeoutError as exc:
            raise LoginFailed("Timed out exchanging the authorization code.") from exc
        except BaseException as exc:
            # Atlassian rejecting the code, a revoked client, a network fault -
            # all arrive wrapped in an anyio ExceptionGroup.
            self._providers.pop(pending.user.key, None)
            raise LoginFailed(f"Atlassian would not complete the sign-in: {explain(exc)}") from exc
        # Drop any cached not-connected provider so the next message picks the grant up.
        self._providers.pop(pending.user.key, None)
        self._agents.pop(pending.user.key, None)
        return pending.user

    async def _drive_login(self, user: TeamsUser, pending: _PendingLogin) -> None:
        """Run one real MCP request so the SDK performs the full OAuth flow."""
        provider = self._provider_for(user.key, interactive_flow=pending)
        client = build_mcp_client(self.settings, auth=provider)
        async with client.session(SERVER_NAME) as session:
            await session.list_tools()
        logger.info("Atlassian sign-in completed for %s", user.key)


def _state_from_url(url: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get("state")
    return values[0] if values else None


async def _refuse_interactive(*_args: Any, **_kwargs: Any):
    raise NotConnected(
        "This Teams user has no valid Atlassian grant, and a bot cannot open a browser."
    )


def _make_redirect_handler(pending: _PendingLogin):
    async def redirect_handler(authorization_url: str) -> None:
        if not pending.authorization_url.done():
            pending.authorization_url.set_result(authorization_url)

    return redirect_handler


def _make_callback_handler(pending: _PendingLogin, timeout: float):
    async def callback_handler() -> tuple[str, str | None]:
        try:
            return await asyncio.wait_for(pending.code, timeout=timeout)
        except TimeoutError as exc:
            raise LoginFailed("The user did not finish signing in.") from exc

    return callback_handler
