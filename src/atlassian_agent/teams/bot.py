"""The Teams bot: one message in, one agent run out, as the sender.

Every turn resolves the sender to a stable key, looks up *their* Atlassian
grant, and runs the agent with a provider scoped to it. A user with no grant
gets a sign-in card instead of an answer; nobody ever borrows anyone else's
Atlassian permissions.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from microsoft_agents.hosting.core import (
    AgentApplication,
    ApplicationOptions,
    CardFactory,
    MemoryStorage,
    MessageFactory,
    TurnContext,
    TurnState,
)

from .access import AtlassianAccess, NotConnected
from .cards import sign_in_card
from .config import TeamsSettings
from .identity import TeamsUser, issue_sign_in_state, user_from_activity

logger = logging.getLogger(__name__)

DISCONNECT_PATTERN = re.compile(r"^\s*(disconnect|unlink|sign\s*out)\b", re.IGNORECASE)
STATUS_PATTERN = re.compile(r"^\s*(status|whoami|who am i)\b", re.IGNORECASE)
HELP_PATTERN = re.compile(r"^\s*(help|\?)\s*$", re.IGNORECASE)

HELP_TEXT = (
    "I answer questions about Jira and Confluence using **your own** Atlassian "
    "permissions.\n\n"
    "- Just ask, e.g. *what are my open issues in the Platform project?*\n"
    "- `status` - whether your Atlassian account is connected\n"
    "- `disconnect` - forget my access to your Atlassian account\n\n"
    "Answers are posted in this chat, so everyone here can see them."
)


def build_bot(
    access: AtlassianAccess,
    teams_settings: TeamsSettings,
    *,
    adapter: Any,
    connection_manager: Any = None,
    agents_config: dict[str, Any] | None = None,
) -> AgentApplication:
    """Wire the message handlers onto an `AgentApplication`."""
    app: AgentApplication = AgentApplication(
        ApplicationOptions(
            adapter=adapter,
            storage=MemoryStorage(),
            bot_app_id=(agents_config or {}).get("CLIENTID"),
            # Teams prefixes "@BotName " onto channel mentions; strip it so the
            # model sees the question and not the mention.
            remove_recipient_mention=True,
            start_typing_timer=True,
        ),
        connection_manager=connection_manager,
    )

    async def _resolve(context: TurnContext) -> TeamsUser | None:
        """Identify the sender, rejecting tenants we were not deployed for."""
        user = user_from_activity(context.activity)
        allowed = teams_settings.allowed_tenant_ids
        if allowed and (user.tenant_id or "") not in allowed:
            logger.warning("Refusing message from tenant %r", user.tenant_id)
            await context.send_activity("This bot is not enabled for your Microsoft 365 tenant.")
            return None
        return user

    async def _send_sign_in(context: TurnContext, user: TeamsUser) -> None:
        state = issue_sign_in_state(
            teams_settings.state_secret, user, ttl_seconds=teams_settings.sign_in_ttl_seconds
        )
        url = f"{teams_settings.public_base_url.rstrip('/')}/oauth/atlassian/start?t={state}"
        await context.send_activity(
            MessageFactory.attachment(
                CardFactory.adaptive_card(sign_in_card(user.display_name, url))
            )
        )

    @app.message(HELP_PATTERN)
    async def on_help(context: TurnContext, _state: TurnState) -> None:
        await context.send_activity(HELP_TEXT)

    @app.message(STATUS_PATTERN)
    async def on_status(context: TurnContext, _state: TurnState) -> None:
        user = await _resolve(context)
        if user is None:
            return
        if await access.is_connected(user):
            await context.send_activity(
                f"Your Atlassian account is connected, {user.display_name}. "
                "I act with your permissions."
            )
        else:
            await _send_sign_in(context, user)

    @app.message(DISCONNECT_PATTERN)
    async def on_disconnect(context: TurnContext, _state: TurnState) -> None:
        user = await _resolve(context)
        if user is None:
            return
        removed = await access.disconnect(user)
        await context.send_activity(
            "Done - I've forgotten your Atlassian tokens. Revoke the app itself at "
            "id.atlassian.com if you want to be thorough."
            if removed
            else "You weren't connected, so there was nothing to forget."
        )

    @app.message(re.compile(r".*", re.DOTALL))
    async def on_message(context: TurnContext, _state: TurnState) -> None:
        user = await _resolve(context)
        if user is None:
            return

        question = (context.activity.text or "").strip()
        if not question:
            await context.send_activity(HELP_TEXT)
            return

        try:
            agent = await access.agent_for(user)
        except NotConnected:
            await _send_sign_in(context, user)
            return

        try:
            answer = await agent.ainvoke(question)
        except NotConnected:
            # The grant expired or was revoked between the check and the call.
            await access.disconnect(user)
            await _send_sign_in(context, user)
            return
        except Exception:
            logger.exception("Agent run failed for %s", user.key)
            await context.send_activity(
                "Something went wrong reaching Atlassian. The details are in my logs."
            )
            return

        await context.send_activity(_format(answer, user, teams_settings))

    @app.error
    async def on_error(context: TurnContext, error: Exception) -> None:
        logger.exception("Unhandled error in a Teams turn", exc_info=error)
        await context.send_activity("Sorry - I hit an unexpected error.")

    return app


def _format(answer: str, user: TeamsUser, teams_settings: TeamsSettings) -> str:
    text = answer.strip() or "I didn't get an answer for that."
    if teams_settings.show_attribution:
        text += f"\n\n_Answered using {user.display_name}'s Atlassian access._"
    return text
