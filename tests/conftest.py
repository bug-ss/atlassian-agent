import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ENV_VARS = [
    "ATLASSIAN_MCP_URL",
    "ATLASSIAN_OAUTH_SCOPES",
    "ATLASSIAN_AGENT_MODEL",
    "ATLASSIAN_TOKEN_CACHE",
    "ATLASSIAN_OAUTH_CALLBACK_PORT",
    "ATLASSIAN_OAUTH_CALLBACK_PATH",
    "ATLASSIAN_OAUTH_CLIENT_NAME",
    "ATLASSIAN_OAUTH_TIMEOUT",
    "ATLASSIAN_MCP_TIMEOUT",
    "ATLASSIAN_MCP_SSE_READ_TIMEOUT",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Keep the developer's real environment out of the tests."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")


@pytest.fixture
def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def settings(tmp_path, unused_port):
    from atlassian_agent.config import Settings

    return Settings(
        token_cache_path=tmp_path / "tokens.json",
        callback_port=unused_port,
        auth_timeout=5.0,
    )
