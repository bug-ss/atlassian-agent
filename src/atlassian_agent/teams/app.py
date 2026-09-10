"""Compose the aiohttp application: bot endpoint + OAuth routes."""

from __future__ import annotations

import logging
import os

from aiohttp import web
from microsoft_agents.activity import load_configuration_from_env
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)

from ..config import Settings
from .access import AtlassianAccess
from .bot import build_bot
from .config import TeamsSettings
from .store import UserTokenStore
from .web import add_oauth_routes

logger = logging.getLogger(__name__)

# Typed keys, as aiohttp prefers, so the stored objects are discoverable.
ACCESS_KEY: web.AppKey[AtlassianAccess] = web.AppKey("atlassian_access")
SETTINGS_KEY: web.AppKey[Settings] = web.AppKey("atlassian_settings")
TEAMS_SETTINGS_KEY: web.AppKey[TeamsSettings] = web.AppKey("teams_settings")


def build_app(
    teams_settings: TeamsSettings | None = None,
    settings: Settings | None = None,
) -> web.Application:
    """The whole bot as one aiohttp app.

    `/api/messages` is authenticated by the Bot Framework JWT middleware - it
    is Azure Bot Service calling in, not a browser. The OAuth routes are
    deliberately outside that: they are hit by the user's browser and carry
    their own signed-state check instead.
    """
    teams_settings = teams_settings or TeamsSettings.from_env()
    settings = settings or teams_settings.atlassian_settings()

    store = UserTokenStore(teams_settings.token_store_path, teams_settings.token_encryption_key)
    access = AtlassianAccess(settings, teams_settings, store)

    # Bot Framework credentials come from the SDK's own CONNECTIONS__* environment
    # variables, so the Azure app registration is configured exactly as it is for
    # any other Microsoft 365 Agents SDK bot.
    agents_config = load_configuration_from_env(os.environ)
    connection_manager = MsalConnectionManager(**agents_config)
    adapter = CloudAdapter(connection_manager=connection_manager)
    bot = build_bot(
        access,
        teams_settings,
        adapter=adapter,
        connection_manager=connection_manager,
        agents_config=agents_config.get("AGENTAPPLICATION", {}),
    )

    async def messages(request: web.Request) -> web.Response:
        return await start_agent_process(request, bot, adapter)

    bot_app = web.Application(middlewares=[jwt_authorization_middleware])
    # The middleware validates the Bot Framework JWT against this configuration;
    # without it every call to the messaging endpoint fails as unconfigured.
    bot_app["agent_configuration"] = connection_manager.get_default_connection_configuration()
    bot_app.router.add_post("", messages)

    app = web.Application()
    app.add_subapp(teams_settings.messages_path, bot_app)
    add_oauth_routes(app, access, teams_settings)

    app[ACCESS_KEY] = access
    app[SETTINGS_KEY] = settings
    app[TEAMS_SETTINGS_KEY] = teams_settings

    logger.info(
        "Atlassian Teams bot ready. messages=%s callback=%s",
        teams_settings.messages_path,
        teams_settings.redirect_uri,
    )
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass

    teams_settings = TeamsSettings.from_env()
    web.run_app(build_app(teams_settings), host=teams_settings.host, port=teams_settings.port)


if __name__ == "__main__":
    main()
