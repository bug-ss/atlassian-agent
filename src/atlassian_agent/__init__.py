"""A LangChain `create_agent` harness for the Atlassian Rovo MCP Server over OAuth 2.1."""

from .agent import (
    DEFAULT_SYSTEM_PROMPT,
    AtlassianAgent,
    atlassian_agent_session,
    build_agent,
    create_atlassian_agent,
)
from .config import Settings
from .mcp_client import build_connection, build_mcp_client, load_atlassian_tools
from .oauth import build_oauth_provider, build_token_storage, token_status

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "AtlassianAgent",
    "Settings",
    "atlassian_agent_session",
    "build_agent",
    "build_connection",
    "build_mcp_client",
    "build_oauth_provider",
    "build_token_storage",
    "create_atlassian_agent",
    "load_atlassian_tools",
    "token_status",
]

__version__ = "0.1.0"
