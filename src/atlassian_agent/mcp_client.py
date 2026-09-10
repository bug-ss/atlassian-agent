"""Connect `langchain-mcp-adapters` to the Atlassian MCP server over OAuth.

`MultiServerMCPClient` takes an `auth` field on a Streamable HTTP connection and
hands it straight to the underlying `httpx.AsyncClient`. Since the MCP SDK's
`OAuthClientProvider` *is* an `httpx.Auth`, the whole OAuth 2.1 dance - 401
challenge, protected-resource discovery, dynamic client registration, PKCE,
token exchange, refresh - happens inside the transport. Nothing above this layer
has to know a token exists.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

from .config import Settings
from .oauth import build_oauth_provider

logger = logging.getLogger(__name__)

SERVER_NAME = "atlassian"


def build_connection(
    settings: Settings,
    *,
    auth: httpx.Auth | None = None,
    open_browser: bool = True,
) -> StreamableHttpConnection:
    """Describe the Atlassian MCP endpoint, with OAuth attached to the transport."""
    connection: StreamableHttpConnection = {
        "transport": "streamable_http",
        "url": settings.mcp_url,
        "auth": auth
        if auth is not None
        else build_oauth_provider(settings, open_browser=open_browser),
        "timeout": settings.request_timeout,
        "sse_read_timeout": settings.sse_read_timeout,
    }
    if settings.extra_headers:
        connection["headers"] = dict(settings.extra_headers)
    return connection


def build_mcp_client(
    settings: Settings,
    *,
    auth: httpx.Auth | None = None,
    open_browser: bool = True,
    **client_kwargs: Any,
) -> MultiServerMCPClient:
    """A `MultiServerMCPClient` holding one authorized Atlassian connection.

    Build this once and keep it: the connection dict carries a single
    `OAuthClientProvider` instance, so every session made from this client
    shares the same in-memory access token rather than re-reading the cache.
    """
    connection = build_connection(settings, auth=auth, open_browser=open_browser)
    return MultiServerMCPClient({SERVER_NAME: connection}, **client_kwargs)


async def load_atlassian_tools(client: MultiServerMCPClient) -> list[BaseTool]:
    """Fetch the Atlassian tool catalog as LangChain tools.

    Each tool opens its own short-lived MCP session when called. That is the
    simplest thing that works for scripts and request handlers; for a long-lived
    conversation prefer `atlassian_agent_session`, which keeps one session open.
    """
    tools = await client.get_tools(server_name=SERVER_NAME)
    logger.info("Loaded %d tools from the Atlassian MCP server", len(tools))
    return tools
