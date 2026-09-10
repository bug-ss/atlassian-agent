"""OAuth 2.1 plumbing for the Atlassian Rovo MCP Server.

The MCP Python SDK ships the protocol half of OAuth - discovery, dynamic client
registration, PKCE, token exchange and refresh - as an `httpx.Auth`
implementation. What it deliberately leaves to the application is the two
human-facing steps and persistence:

* `redirect_handler`  - get the user to the authorization URL
* `callback_handler`  - receive `code` + `state` back from the browser
* `TokenStorage`      - remember tokens and the registered client across runs

This module supplies all three for a desktop/CLI setting: a loopback HTTP
listener on a fixed port, a browser launch, and a 0600 JSON cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.auth_utils import calculate_token_expiry

from .config import Settings

logger = logging.getLogger(__name__)

# Refresh this many seconds before the access token actually expires, so a long
# tool call started just under the wire doesn't die mid-flight.
REFRESH_SKEW_SECONDS = 60.0

CACHE_VERSION = 1

# What the MCP SDK expects for the two human-facing steps of the flow.
RedirectHandler = Callable[[str], Awaitable[None]]
CallbackHandler = Callable[[], Awaitable[tuple[str, str | None]]]


class OAuthCallbackError(RuntimeError):
    """The authorization server came back with an error instead of a code."""


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------


class FileTokenStorage(TokenStorage):
    """Persist OAuth tokens and the dynamic client registration to a JSON file.

    One file can hold entries for several MCP servers, keyed by server URL.

    A registration is only reusable while the parameters it was created with
    still hold, so each entry records a fingerprint of the redirect URI, scopes
    and client name. When that changes the entry is ignored, which transparently
    forces a fresh registration and login instead of failing later with an
    opaque `invalid_scope` or `redirect_uri_mismatch`.
    """

    def __init__(self, path: Path, server_url: str, registration_fingerprint: str) -> None:
        self.path = Path(path).expanduser()
        self.server_url = server_url
        self.registration_fingerprint = registration_fingerprint

    # -- file helpers -------------------------------------------------------

    def _read_file(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {"version": CACHE_VERSION, "servers": {}}
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt cache should cost you one login, not a crash loop.
            logger.warning("Ignoring unreadable token cache %s: %s", self.path, exc)
            return {"version": CACHE_VERSION, "servers": {}}
        if not isinstance(data, dict) or "servers" not in data:
            return {"version": CACHE_VERSION, "servers": {}}
        return data

    def _write_file(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, chmod it while it is still
        # private, then rename - so the tokens are never briefly world-readable.
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def _entry(self) -> dict[str, Any]:
        entry = self._read_file()["servers"].get(self.server_url)
        if not isinstance(entry, dict):
            return {}
        if entry.get("fingerprint") != self.registration_fingerprint:
            logger.info(
                "OAuth settings changed since the cached login for %s; re-authorizing.",
                self.server_url,
            )
            return {}
        return entry

    def _update_entry(self, **fields: Any) -> None:
        data = self._read_file()
        servers = data.setdefault("servers", {})
        entry = servers.get(self.server_url)
        if not isinstance(entry, dict) or entry.get("fingerprint") != self.registration_fingerprint:
            entry = {"fingerprint": self.registration_fingerprint}
        entry.update(fields)
        servers[self.server_url] = entry
        data["version"] = CACHE_VERSION
        self._write_file(data)

    # -- TokenStorage protocol ---------------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._entry().get("tokens")
        if not raw:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except ValueError as exc:
            logger.warning("Discarding malformed cached tokens: %s", exc)
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._update_entry(
            tokens=tokens.model_dump(mode="json", exclude_none=True),
            expires_at=calculate_token_expiry(tokens.expires_in),
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._entry().get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except ValueError as exc:
            logger.warning("Discarding malformed cached client registration: %s", exc)
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._update_entry(client_info=client_info.model_dump(mode="json", exclude_none=True))

    # -- extras -------------------------------------------------------------

    async def get_token_expiry(self) -> float | None:
        """Absolute expiry of the cached access token, if the server gave one.

        The SDK holds this in memory but does not persist it, so without this a
        restart treats a long-expired token as good, spends a request finding
        out it is not, and then sends you back through the browser. Restoring it
        turns that into a silent refresh.
        """
        expires_at = self._entry().get("expires_at")
        return float(expires_at) if isinstance(expires_at, (int, float)) else None

    def snapshot(self) -> dict[str, Any]:
        """The cached entry for this server, or `{}` if there is nothing usable."""
        return dict(self._entry())

    def clear(self) -> None:
        """Forget this server's tokens and client registration."""
        data = self._read_file()
        if data.get("servers", {}).pop(self.server_url, None) is not None:
            self._write_file(data)


# ---------------------------------------------------------------------------
# Browser + loopback callback
# ---------------------------------------------------------------------------

_SUCCESS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Authorized</title></head>
<body style="font-family:system-ui,sans-serif;margin:4rem auto;max-width:32rem;text-align:center">
<h1>You're connected</h1>
<p>The Atlassian agent has its access token. You can close this tab and go back
to the terminal.</p>
</body></html>
"""

_FAILURE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Authorization failed</title></head>
<body style="font-family:system-ui,sans-serif;margin:4rem auto;max-width:32rem;text-align:center">
<h1>Authorization failed</h1>
<p>{message}</p>
<p>Check the terminal for details.</p>
</body></html>
"""


class LoopbackOAuthFlow:
    """Runs the browser half of the authorization-code flow.

    Provides the two callables the MCP SDK's `OAuthClientProvider` expects. The
    listener is started by `redirect_handler` (before the browser is pointed at
    Atlassian) and torn down once `callback_handler` has the code, so nothing is
    listening on the port outside an active login.
    """

    def __init__(self, settings: Settings, *, open_browser: bool = True) -> None:
        self.settings = settings
        self.open_browser = open_browser
        self._server: asyncio.AbstractServer | None = None
        self._result: asyncio.Future[tuple[str, str | None]] | None = None

    async def redirect_handler(self, authorization_url: str) -> None:
        await self._start_server()
        print("\nOpening your browser to authorize this agent with Atlassian.")
        print("If it doesn't open, paste this URL yourself:\n")
        print(f"  {authorization_url}\n")
        if self.open_browser:
            try:
                opened = webbrowser.open(authorization_url)
            except Exception as exc:  # pragma: no cover - platform dependent
                logger.debug("webbrowser.open failed: %s", exc)
                opened = False
            if not opened:
                print("(No browser could be launched here - use the URL above.)\n")

    async def callback_handler(self) -> tuple[str, str | None]:
        if self._result is None:  # pragma: no cover - defensive
            raise RuntimeError("callback_handler called before redirect_handler")
        try:
            return await asyncio.wait_for(self._result, timeout=self.settings.auth_timeout)
        except TimeoutError as exc:
            raise OAuthCallbackError(
                f"Timed out after {self.settings.auth_timeout:.0f}s waiting for the "
                "Atlassian authorization callback. Set ATLASSIAN_OAUTH_TIMEOUT to allow "
                "more time."
            ) from exc
        finally:
            await self._stop_server()

    # -- internals ----------------------------------------------------------

    async def _start_server(self) -> None:
        if self._server is not None:
            return
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        try:
            self._server = await asyncio.start_server(
                self._handle_request, self.settings.callback_host, self.settings.callback_port
            )
        except OSError as exc:
            raise OAuthCallbackError(
                f"Cannot listen on {self.settings.redirect_uri} ({exc}). "
                "Free the port, or set ATLASSIAN_OAUTH_CALLBACK_PORT to another one "
                "(Atlassian accepts any loopback port; the new redirect URI is "
                "registered on next login). If nothing may listen at all, use the "
                "paste-back flow instead - `atlassian-agent --login --paste-code`."
            ) from exc

    async def _stop_server(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await self._read_request(reader)
            if request_line is None:
                return
            path = request_line.split(" ")[1] if len(request_line.split(" ")) > 1 else "/"
            split = urlsplit(path)
            if split.path != self.settings.callback_path:
                # Browsers ask for /favicon.ico and friends; don't let that
                # resolve the login.
                await self._respond(writer, 404, "text/plain; charset=utf-8", "Not found")
                return

            params = parse_qs(split.query)
            error = _first(params, "error")
            if error:
                description = _first(params, "error_description") or ""
                message = f"{error}: {description}".strip(": ")
                await self._respond(
                    writer,
                    400,
                    "text/html; charset=utf-8",
                    _FAILURE_HTML.format(message=_escape(message)),
                )
                self._set_exception(
                    OAuthCallbackError(
                        f"Atlassian refused the authorization request ({message}). "
                        "A rejected scope is the usual cause - check "
                        "ATLASSIAN_OAUTH_SCOPES against what your site has enabled."
                    )
                )
                return

            code = _first(params, "code")
            if not code:
                await self._respond(
                    writer,
                    400,
                    "text/html; charset=utf-8",
                    _FAILURE_HTML.format(message="No authorization code."),
                )
                self._set_exception(
                    OAuthCallbackError("Callback arrived without an authorization code.")
                )
                return

            await self._respond(writer, 200, "text/html; charset=utf-8", _SUCCESS_HTML)
            self._set_result((code, _first(params, "state")))
        except Exception as exc:  # pragma: no cover - transport level
            logger.debug("OAuth callback request failed: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):  # pragma: no cover
                pass

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> str | None:
        try:
            header_block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except asyncio.IncompleteReadError as exc:
            header_block = exc.partial
        except (TimeoutError, asyncio.LimitOverrunError, ValueError):
            return None
        text = header_block.decode("latin-1", errors="replace")
        first_line = text.split("\r\n", 1)[0]
        return first_line or None

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter, status: int, content_type: str, body: str
    ) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        payload = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + payload)
        await writer.drain()

    def _set_result(self, value: tuple[str, str | None]) -> None:
        if self._result is not None and not self._result.done():
            self._result.set_result(value)

    def _set_exception(self, exc: Exception) -> None:
        if self._result is not None and not self._result.done():
            self._result.set_exception(exc)


def _first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    return values[0] if values else None


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


class ManualPasteFlow:
    """Authorization without listening on any socket.

    Prints the authorization URL, you complete consent in whatever browser you
    like - on another machine if need be - and paste the URL you land on back
    into the terminal. The browser will show a connection error on that final
    redirect, which is expected and harmless: the code is in its address bar,
    and that is all we need.

    Use this where a loopback listener is impossible: a locked-down host, a
    container with no port mapping, or a remote shell with no way to forward a
    port back.
    """

    def __init__(self, settings: Settings, *, reader: Callable[[str], str] | None = None) -> None:
        self.settings = settings
        self._reader = reader or input

    async def redirect_handler(self, authorization_url: str) -> None:
        print("\nOpen this URL in any browser and approve the request:\n")
        print(f"  {authorization_url}\n")
        print(
            f"You will be redirected to {self.settings.redirect_uri} and the page will\n"
            "fail to load. That is expected - copy the full URL out of the address bar."
        )

    async def callback_handler(self) -> tuple[str, str | None]:
        raw = (
            await asyncio.to_thread(self._reader, "\nPaste the full redirect URL here: ")
        ).strip()
        return parse_redirect_response(raw)


def parse_redirect_response(raw: str) -> tuple[str, str | None]:
    """Pull `code` and `state` out of a pasted redirect URL.

    Raises with a usable message rather than letting a half-pasted value fail
    later as an opaque state mismatch.
    """
    if not raw:
        raise OAuthCallbackError("Nothing pasted; the login was not completed.")

    query = urlsplit(raw).query or (raw if "=" in raw else "")
    params = parse_qs(query)

    error = _first(params, "error")
    if error:
        description = _first(params, "error_description")
        detail = f"{error}: {description}" if description else error
        raise OAuthCallbackError(f"Atlassian refused the authorization request ({detail})")

    code = _first(params, "code")
    if not code:
        raise OAuthCallbackError(
            "That does not look like a redirect URL - no `code` parameter found. "
            "Paste the entire URL from the browser's address bar, starting with http."
        )

    state = _first(params, "state")
    if not state:
        raise OAuthCallbackError(
            "The pasted URL has a `code` but no `state`. Paste the whole URL, "
            "unmodified - `state` is what proves the response belongs to this login."
        )
    return code, state


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AtlassianOAuthProvider(OAuthClientProvider):
    """`OAuthClientProvider` that also restores token expiry from storage.

    The base class loads tokens from storage but not the expiry that went with
    them, so a token cached an hour ago looks valid until the server rejects it.
    Restoring the expiry lets the normal refresh path run first, which is the
    difference between one browser login and one per token lifetime.
    """

    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        get_expiry = getattr(storage, "get_token_expiry", None)
        if get_expiry is None:
            return
        expires_at = await get_expiry()
        if expires_at is not None:
            self.context.token_expiry_time = expires_at - REFRESH_SKEW_SECONDS
            if not self.context.is_token_valid():
                logger.debug("Cached Atlassian access token is stale; will refresh.")


def registration_fingerprint(settings: Settings) -> str:
    """Fingerprint the inputs that a stored client registration depends on."""
    material = "|".join(
        [settings.mcp_url, settings.redirect_uri, settings.scope_string, settings.client_name]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def build_token_storage(settings: Settings) -> FileTokenStorage:
    return FileTokenStorage(
        settings.token_cache_path,
        server_url=settings.mcp_url,
        registration_fingerprint=registration_fingerprint(settings),
    )


class PinnedScopeClientMetadata(OAuthClientMetadata):
    """Client metadata whose `scope` the MCP SDK is not allowed to widen.

    The SDK implements the MCP spec's scope selection strategy: on the 401 that
    starts the flow it *replaces* whatever scope you configured with the scope
    from `WWW-Authenticate`, or - when the header carries none, which is
    Atlassian's case - with every scope in the server's protected-resource
    metadata. For Atlassian that is the full catalog, `delete:jira:*` and
    `manage:jira:*` included, so a client asking only to read Jira would land
    the user on a consent screen granting deletion rights.

    The overwritten value feeds both dynamic client registration and the
    authorization URL, so ignoring writes to `scope` here is what keeps the
    configured least-privilege set intact end to end. Set the scopes to `auto`
    (an empty `Settings.scopes`) to get the SDK's default behavior back.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "scope":
            if value != self.scope:
                logger.debug(
                    "Ignoring scope widening to %r; keeping configured scope %r", value, self.scope
                )
            return
        super().__setattr__(name, value)


def build_client_metadata(settings: Settings) -> OAuthClientMetadata:
    """Client metadata for a public, PKCE-only OAuth client."""
    common: dict[str, Any] = {
        "client_name": settings.client_name,
        "redirect_uris": [settings.redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        # Atlassian advertises "none" among its supported auth methods: this is
        # a public client, so it holds no secret and relies on PKCE.
        "token_endpoint_auth_method": "none",
    }
    if not settings.scopes:
        # "auto": let the SDK request whatever the server advertises.
        return OAuthClientMetadata(**common)
    return PinnedScopeClientMetadata(scope=settings.scope_string, **common)


def build_oauth_provider(
    settings: Settings,
    *,
    storage: TokenStorage | None = None,
    open_browser: bool = True,
    manual_paste: bool = False,
    redirect_handler: RedirectHandler | None = None,
    callback_handler: CallbackHandler | None = None,
) -> AtlassianOAuthProvider:
    """Assemble the `httpx.Auth` that authorizes every request to the MCP server.

    By default the browser half runs through `LoopbackOAuthFlow`, which suits a
    CLI or a desktop app. `manual_paste=True` swaps in `ManualPasteFlow`, which
    needs no listening socket at all. Pass your own `redirect_handler` /
    `callback_handler` to host the flow somewhere else - a web app that
    redirects the user and resumes on its own callback route, or a test that
    captures the URL.
    """
    if (redirect_handler is None) != (callback_handler is None):
        raise ValueError(
            "redirect_handler and callback_handler must be supplied together: "
            "whoever sends the user out is also the one who gets the code back."
        )
    if redirect_handler is None or callback_handler is None:
        flow: LoopbackOAuthFlow | ManualPasteFlow = (
            ManualPasteFlow(settings)
            if manual_paste
            else LoopbackOAuthFlow(settings, open_browser=open_browser)
        )
        redirect_handler, callback_handler = flow.redirect_handler, flow.callback_handler

    return AtlassianOAuthProvider(
        server_url=settings.mcp_url,
        client_metadata=build_client_metadata(settings),
        storage=storage or build_token_storage(settings),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=settings.auth_timeout,
    )


def token_status(settings: Settings) -> dict[str, Any]:
    """Human-readable view of the cached credentials, for `--status`."""
    storage = build_token_storage(settings)
    entry = storage.snapshot()
    if not entry.get("tokens"):
        return {"authorized": False}
    expires_at = entry.get("expires_at")
    return {
        "authorized": True,
        "client_id": (entry.get("client_info") or {}).get("client_id"),
        "scope": (entry.get("tokens") or {}).get("scope"),
        "has_refresh_token": bool((entry.get("tokens") or {}).get("refresh_token")),
        "expires_in_seconds": (
            max(0.0, float(expires_at) - time.time())
            if isinstance(expires_at, (int, float))
            else None
        ),
        "cache_path": str(storage.path),
    }
