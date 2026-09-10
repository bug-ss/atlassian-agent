"""The LangChain `create_agent` harness, wired to Atlassian's MCP tools.

Two ways in:

* `create_atlassian_agent()` - tools open a short MCP session per call. Good for
  one-shot scripts and request handlers.
* `atlassian_agent_session()` - an async context manager that holds one MCP
  session open for the agent's lifetime. Good for conversations.

Both hand back an `AtlassianAgent` whose `.agent` is the compiled graph returned
by `create_agent`, so anything you can do with a LangChain agent - streaming,
middleware, checkpointers, embedding it in LangGraph - you can do here.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .config import Settings
from .mcp_client import SERVER_NAME, build_mcp_client, load_atlassian_tools

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You are an assistant that works inside a user's Atlassian Cloud organization \
(Jira, Confluence, Compass, Bitbucket and related products) through the \
Atlassian MCP server. You act on their real data, so accuracy matters more \
than speed.

How to work:
- Most Atlassian tools are scoped to a site and need a cloud ID. If you do not \
have one yet, call the tool that lists accessible Atlassian resources first and \
reuse the result for the rest of the conversation rather than asking again.
- Prefer searching (JQL, CQL, or the search tools) over guessing keys, IDs or \
space names. If a lookup returns nothing, say so instead of inventing a result.
- Before anything that writes - creating or transitioning an issue, editing a \
page, posting a comment - state exactly what you are about to do and get the \
user's go-ahead, unless they already told you to do it.
- Report what actually happened, including tool errors, and include issue keys \
and links so the user can verify.
- If a tool fails because a permission or scope is missing, name the scope; do \
not retry the same call in a loop.
"""


def resolve_model(model: str | BaseChatModel, **model_kwargs: Any) -> BaseChatModel:
    """Turn a `"provider:model"` string into a chat model, with a clear error.

    A `BaseChatModel` is passed straight through, so callers can configure
    temperature, thinking, retries or an entirely different provider themselves.
    """
    if isinstance(model, BaseChatModel):
        return model
    if model.startswith("anthropic:") and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set, so the Anthropic model cannot be created. "
            "Export the key, or point ATLASSIAN_AGENT_MODEL at another provider "
            "(for example 'openai:gpt-4.1' with that provider's package installed)."
        )
    return init_chat_model(model, **model_kwargs)


def build_agent(
    tools: Sequence[BaseTool],
    *,
    model: str | BaseChatModel | None = None,
    system_prompt: str | None = None,
    middleware: Sequence[Any] = (),
    checkpointer: Any | None = None,
    **agent_kwargs: Any,
):
    """The `create_agent` call itself - pure wiring, no network access.

    Separated out so it can be unit-tested with fake tools, and so you can reuse
    it with your own tools alongside the Atlassian ones.
    """
    return create_agent(
        resolve_model(model if model is not None else Settings.from_env().model),
        list(tools),
        system_prompt=system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT,
        middleware=tuple(middleware),
        checkpointer=checkpointer,
        **agent_kwargs,
    )


@dataclass
class AtlassianAgent:
    """A compiled agent plus the MCP plumbing it depends on."""

    agent: Any
    tools: list[BaseTool]
    client: MultiServerMCPClient
    settings: Settings
    _thread_counter: int = field(default=0, repr=False)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools]

    async def ainvoke(
        self,
        prompt: str | Sequence[BaseMessage],
        *,
        thread_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Run one turn and return the agent's final text.

        `thread_id` is only meaningful when the agent was built with a
        checkpointer; without one every call starts from an empty history.
        """
        messages = [("user", prompt)] if isinstance(prompt, str) else list(prompt)
        config = kwargs.pop("config", {})
        if thread_id is not None:
            config = {
                **config,
                "configurable": {**config.get("configurable", {}), "thread_id": thread_id},
            }
        result = await self.agent.ainvoke({"messages": messages}, config=config or None, **kwargs)
        return final_text(result["messages"])


def final_text(messages: Sequence[BaseMessage]) -> str:
    """Extract the assistant's last text, skipping tool-call-only messages."""
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        text = message.text
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


async def create_atlassian_agent(
    settings: Settings | None = None,
    *,
    model: str | BaseChatModel | None = None,
    system_prompt: str | None = None,
    middleware: Sequence[Any] = (),
    checkpointer: Any | None = None,
    extra_tools: Sequence[BaseTool] = (),
    open_browser: bool = True,
    manual_paste: bool = False,
    **agent_kwargs: Any,
) -> AtlassianAgent:
    """Authorize, load the Atlassian tool catalog, and compile an agent.

    The first call opens a browser for the OAuth consent screen; later calls
    reuse the cached tokens until the refresh token itself expires.
    """
    settings = settings or Settings.from_env()
    client = build_mcp_client(settings, open_browser=open_browser, manual_paste=manual_paste)
    tools = [*await load_atlassian_tools(client), *extra_tools]
    agent = build_agent(
        tools,
        model=model or settings.model,
        system_prompt=system_prompt,
        middleware=middleware,
        checkpointer=checkpointer,
        **agent_kwargs,
    )
    return AtlassianAgent(agent=agent, tools=tools, client=client, settings=settings)


@asynccontextmanager
async def atlassian_agent_session(
    settings: Settings | None = None,
    *,
    model: str | BaseChatModel | None = None,
    system_prompt: str | None = None,
    middleware: Sequence[Any] = (),
    checkpointer: Any | None = None,
    extra_tools: Sequence[BaseTool] = (),
    open_browser: bool = True,
    manual_paste: bool = False,
    **agent_kwargs: Any,
) -> AsyncIterator[AtlassianAgent]:
    """Same agent, but over a single MCP session held open for the block.

    Worth it for anything multi-turn: one HTTP session and one initialize
    handshake for the whole conversation instead of per tool call.
    """
    settings = settings or Settings.from_env()
    client = build_mcp_client(settings, open_browser=open_browser, manual_paste=manual_paste)
    async with client.session(SERVER_NAME) as session:
        tools = [*await load_mcp_tools(session), *extra_tools]
        logger.info("Loaded %d Atlassian tools over a persistent session", len(tools))
        agent = build_agent(
            tools,
            model=model or settings.model,
            system_prompt=system_prompt,
            middleware=middleware,
            checkpointer=checkpointer,
            **agent_kwargs,
        )
        yield AtlassianAgent(agent=agent, tools=tools, client=client, settings=settings)
