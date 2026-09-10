"""The browser-facing half of the bot: sign-in start and OAuth callback."""

from __future__ import annotations

import html
import logging

from aiohttp import web

from .access import AtlassianAccess, LoginFailed
from .config import TeamsSettings
from .identity import InvalidState, TeamsUser, verify_sign_in_state

logger = logging.getLogger(__name__)

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;margin:4rem auto;max-width:34rem;
             padding:0 1rem;text-align:center;color:#172b4d">
<h1 style="font-size:1.4rem">{title}</h1>
<p style="line-height:1.6">{body}</p>
</body></html>
"""


def _page(title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(
        text=_PAGE.format(title=html.escape(title), body=body),
        content_type="text/html",
        status=status,
    )


def add_oauth_routes(
    app: web.Application, access: AtlassianAccess, teams_settings: TeamsSettings
) -> None:
    """Mount `/oauth/atlassian/start` and the callback route."""

    async def start(request: web.Request) -> web.StreamResponse:
        """Verify the signed link, then send the user on to Atlassian.

        The link is signed, short-lived and single-use, so this endpoint cannot
        be used to spin up sign-in flows for arbitrary identities.
        """
        token = request.query.get("t", "")
        try:
            payload = verify_sign_in_state(teams_settings.state_secret, token)
        except InvalidState as exc:
            return _page("Sign-in link not valid", html.escape(str(exc)), status=400)

        fresh = await access.store.consume_nonce(payload["n"], expires_at=float(payload["exp"]))
        if not fresh:
            return _page(
                "Sign-in link already used",
                "Each link works once. Ask the bot to connect again for a new one.",
                status=400,
            )

        user = TeamsUser(
            key=payload["k"],
            display_name=payload.get("name", "there"),
            channel_user_id=payload.get("name", ""),
        )
        try:
            authorization_url = await access.begin_login(user)
        except LoginFailed as exc:
            return _page("Could not start sign-in", html.escape(str(exc)), status=502)
        except Exception:
            logger.exception("Failed to start Atlassian sign-in")
            return _page("Could not start sign-in", "Check the bot logs.", status=500)

        raise web.HTTPFound(authorization_url)

    async def callback(request: web.Request) -> web.StreamResponse:
        error = request.query.get("error")
        if error:
            detail = request.query.get("error_description", "")
            return _page(
                "Atlassian declined the request",
                html.escape(f"{error}: {detail}".strip(": ")),
                status=400,
            )

        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            return _page("Incomplete callback", "No authorization code was returned.", status=400)

        try:
            user = await access.complete_login(state, code)
        except LoginFailed as exc:
            return _page("Sign-in could not be completed", html.escape(str(exc)), status=400)
        except Exception:
            logger.exception("Atlassian sign-in failed during token exchange")
            return _page("Sign-in failed", "Check the bot logs.", status=500)

        return _page(
            "Atlassian connected",
            f"Linked to the Teams account of <b>{html.escape(user.display_name)}</b>.<br><br>"
            "If that is not you, disconnect it now by sending the bot "
            "<code>disconnect atlassian</code>.<br><br>"
            "You can close this tab and go back to Teams.",
        )

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/oauth/atlassian/start", start)
    app.router.add_get(teams_settings.callback_path, callback)
    app.router.add_get("/healthz", healthz)
