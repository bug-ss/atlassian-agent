"""End-to-end wiring of the create_agent harness, using a scripted model.

No network: the point is to prove the harness actually runs a tool-calling
loop over the tools it is given, and that the Atlassian connection is shaped
the way `langchain-mcp-adapters` expects.
"""

from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from atlassian_agent.agent import (
    DEFAULT_SYSTEM_PROMPT,
    AtlassianAgent,
    build_agent,
    final_text,
    resolve_model,
)
from atlassian_agent.config import Settings
from atlassian_agent.mcp_client import SERVER_NAME, build_connection, build_mcp_client
from atlassian_agent.oauth import AtlassianOAuthProvider


class ScriptedChatModel(BaseChatModel):
    """Replays a fixed list of assistant messages and records what it was asked."""

    responses: list[AIMessage] = []
    seen_prompts: list[list[BaseMessage]] = []
    bound_tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        self.bound_tool_names = [getattr(t, "name", str(t)) for t in tools]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen_prompts.append(list(messages))
        index = min(len(self.seen_prompts) - 1, len(self.responses) - 1)
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


@tool
def get_issue(issue_key: str) -> str:
    """Look up a Jira issue by key."""
    return f"{issue_key}: Login page returns 500"


@pytest.fixture
def scripted_model() -> ScriptedChatModel:
    return ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_issue",
                        "args": {"issue_key": "ENG-1"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="ENG-1 is about the login page returning a 500."),
        ]
    )


async def test_agent_runs_a_tool_calling_loop(scripted_model):
    agent = build_agent([get_issue], model=scripted_model)

    result = await agent.ainvoke({"messages": [("user", "What is ENG-1 about?")]})

    assert final_text(result["messages"]) == "ENG-1 is about the login page returning a 500."
    # The tool actually ran and its result was fed back to the model.
    assert any("Login page returns 500" in str(m.content) for m in result["messages"])
    assert scripted_model.bound_tool_names == ["get_issue"]
    assert len(scripted_model.seen_prompts) == 2


async def test_default_system_prompt_is_sent(scripted_model):
    agent = build_agent([get_issue], model=scripted_model)
    await agent.ainvoke({"messages": [("user", "hi")]})

    first_prompt = scripted_model.seen_prompts[0]
    assert isinstance(first_prompt[0], SystemMessage)
    assert first_prompt[0].content == DEFAULT_SYSTEM_PROMPT
    assert "cloud ID" in DEFAULT_SYSTEM_PROMPT


async def test_system_prompt_can_be_replaced(scripted_model):
    agent = build_agent([get_issue], model=scripted_model, system_prompt="Be terse.")
    await agent.ainvoke({"messages": [("user", "hi")]})
    assert scripted_model.seen_prompts[0][0].content == "Be terse."


async def test_extra_tools_are_merged_with_atlassian_tools(scripted_model):
    @tool
    def local_note(text: str) -> str:
        """Write a note locally."""
        return text

    agent = build_agent([get_issue, local_note], model=scripted_model)
    await agent.ainvoke({"messages": [("user", "hi")]})
    assert scripted_model.bound_tool_names == ["get_issue", "local_note"]


async def test_agent_helper_returns_final_text(scripted_model):
    holder = AtlassianAgent(
        agent=build_agent([get_issue], model=scripted_model),
        tools=[get_issue],
        client=None,
        settings=Settings(),
    )
    assert holder.tool_names == ["get_issue"]
    answer = await holder.ainvoke("What is ENG-1 about?")
    assert answer.startswith("ENG-1 is about")


def test_final_text_skips_tool_call_only_messages():
    messages = [
        HumanMessage("hi"),
        AIMessage(
            content="", tool_calls=[{"name": "x", "args": {}, "id": "1", "type": "tool_call"}]
        ),
        AIMessage(content="the answer"),
    ]
    assert final_text(messages) == "the answer"
    assert final_text([HumanMessage("hi")]) == ""


# -- model resolution -------------------------------------------------------


def test_resolve_model_passes_through_a_model_instance(scripted_model):
    assert resolve_model(scripted_model) is scripted_model


def test_resolve_model_builds_from_a_provider_string():
    model = resolve_model("anthropic:claude-opus-5")
    assert model.model == "claude-opus-5"


def test_missing_anthropic_key_is_explained(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        resolve_model("anthropic:claude-opus-5")


# -- MCP connection shape ---------------------------------------------------


def test_connection_carries_oauth_on_the_transport(settings):
    connection = build_connection(settings, open_browser=False)

    assert connection["transport"] == "streamable_http"
    assert connection["url"] == settings.mcp_url
    assert isinstance(connection["auth"], AtlassianOAuthProvider)
    assert connection["timeout"] == settings.request_timeout
    assert connection["sse_read_timeout"] == settings.sse_read_timeout


def test_extra_headers_are_forwarded(tmp_path):
    settings = Settings(
        token_cache_path=tmp_path / "t.json",
        extra_headers={"X-Trace": "abc"},
    )
    assert build_connection(settings, open_browser=False)["headers"] == {"X-Trace": "abc"}


def test_headers_are_omitted_when_unset(settings):
    assert "headers" not in build_connection(settings, open_browser=False)


def test_client_shares_one_oauth_provider_across_sessions(settings):
    client = build_mcp_client(settings, open_browser=False)
    assert list(client.connections) == [SERVER_NAME]
    # Same object every time, so the in-memory access token is reused rather
    # than re-read (or re-fetched) per session.
    first = client.connections[SERVER_NAME]["auth"]
    second = client.connections[SERVER_NAME]["auth"]
    assert first is second
