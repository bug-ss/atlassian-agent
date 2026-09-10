"""Command line front end: one-shot questions, a REPL, and credential management."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from .agent import DEFAULT_SYSTEM_PROMPT, AtlassianAgent, atlassian_agent_session, final_text
from .config import Settings
from .errors import explain as _explain
from .mcp_client import SERVER_NAME, build_mcp_client
from .oauth import OAuthCallbackError, build_token_storage, token_status

REPL_BANNER = """\
Atlassian agent ready. Ask about Jira, Confluence, or anything else your
Atlassian tools cover.

  /tools    list the tools loaded from the MCP server
  /reset    start a fresh conversation
  /exit     quit (Ctrl-D works too)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlassian-agent",
        description=(
            "A LangChain agent that reaches Jira, Confluence and the rest of "
            "Atlassian Cloud through the Atlassian MCP server, authorized with OAuth 2.1."
        ),
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="Ask one question and exit. With no query, starts an interactive session.",
    )
    parser.add_argument("--model", help="Chat model, e.g. anthropic:claude-opus-5")
    parser.add_argument("--mcp-url", dest="mcp_url", help="Override the MCP server URL")
    parser.add_argument(
        "--scopes",
        help="Space- or comma-separated OAuth scopes (changing these forces a new login)",
    )
    parser.add_argument("--system-prompt", help="Replace the default system prompt")
    parser.add_argument(
        "--system-prompt-file",
        type=Path,
        help="Read the system prompt from a file",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of launching a browser",
    )
    parser.add_argument(
        "--paste-code",
        action="store_true",
        help=(
            "Authorize without listening on a port: open the URL anywhere, then "
            "paste the redirect URL back. Use when the loopback callback is blocked."
        ),
    )
    parser.add_argument(
        "--hide-tool-calls",
        action="store_true",
        help="Don't show tool calls as the agent makes them",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")

    actions = parser.add_argument_group("credentials and diagnostics")
    actions.add_argument(
        "--login", action="store_true", help="Authorize now and exit (also refreshes tool list)"
    )
    actions.add_argument(
        "--logout", action="store_true", help="Delete the cached tokens and client registration"
    )
    actions.add_argument("--status", action="store_true", help="Show cached credential state")
    actions.add_argument("--list-tools", action="store_true", help="List available tools and exit")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if args.model:
        overrides["model"] = args.model
    if args.mcp_url:
        overrides["mcp_url"] = args.mcp_url
    if args.scopes:
        overrides["scopes"] = tuple(s for s in args.scopes.replace(",", " ").split() if s)
    return Settings.from_env(**overrides)


def resolve_system_prompt(args: argparse.Namespace) -> str:
    if args.system_prompt_file:
        return args.system_prompt_file.read_text(encoding="utf-8")
    if args.system_prompt:
        return args.system_prompt
    return DEFAULT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def cmd_status(settings: Settings) -> int:
    status = token_status(settings)
    print(f"MCP server : {settings.mcp_url}")
    print(f"Token cache: {settings.token_cache_path}")
    if not status["authorized"]:
        print("Status     : not authorized - run `atlassian-agent --login`")
        return 0
    expires_in = status["expires_in_seconds"]
    print(f"Status     : authorized as client {status['client_id']}")
    print(f"Scopes     : {status['scope'] or '(not reported)'}")
    print(f"Refresh    : {'yes' if status['has_refresh_token'] else 'no (offline_access missing)'}")
    if expires_in is not None:
        print(f"Expires in : {expires_in / 60:.0f} min")
    return 0


async def cmd_logout(settings: Settings) -> int:
    build_token_storage(settings).clear()
    print(f"Cleared cached Atlassian credentials for {settings.mcp_url}.")
    return 0


async def cmd_login(settings: Settings, *, open_browser: bool, manual_paste: bool = False) -> int:
    client = build_mcp_client(settings, open_browser=open_browser, manual_paste=manual_paste)
    async with client.session(SERVER_NAME) as session:
        result = await session.list_tools()
    info = token_status(settings)
    print(f"\nAuthorized. {len(result.tools)} tools available from {settings.mcp_url}.")
    if not info.get("has_refresh_token"):
        print(
            "Note: no refresh token was issued, so you'll be sent back through the "
            "browser when this one expires. Include 'offline_access' in "
            "ATLASSIAN_OAUTH_SCOPES to avoid that."
        )
    return 0


async def cmd_list_tools(
    settings: Settings, *, open_browser: bool, manual_paste: bool = False
) -> int:
    client = build_mcp_client(settings, open_browser=open_browser, manual_paste=manual_paste)
    async with client.session(SERVER_NAME) as session:
        result = await session.list_tools()
    for tool in sorted(result.tools, key=lambda t: t.name):
        summary = (tool.description or "").strip().splitlines()
        print(f"  {tool.name:<44} {summary[0] if summary else ''}")
    print(f"\n{len(result.tools)} tools.")
    return 0


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


def _describe_tool_call(call: dict[str, Any]) -> str:
    args = call.get("args") or {}
    rendered = ", ".join(f"{k}={_truncate(repr(v), 60)}" for k, v in list(args.items())[:4])
    if len(args) > 4:
        rendered += ", ..."
    return f"{call.get('name', '?')}({rendered})"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def run_turn(
    holder: AtlassianAgent,
    prompt: str,
    *,
    config: dict[str, Any] | None,
    show_tool_calls: bool,
) -> str:
    """Stream one turn, echoing tool activity, and return the final answer."""
    messages: list[Any] = []
    async for update in holder.agent.astream(
        {"messages": [("user", prompt)]}, config=config, stream_mode="updates"
    ):
        for node_output in update.values():
            if not isinstance(node_output, dict):
                continue
            for message in node_output.get("messages", []) or []:
                messages.append(message)
                if not show_tool_calls:
                    continue
                if isinstance(message, AIMessage) and message.tool_calls:
                    for call in message.tool_calls:
                        print(f"  → {_describe_tool_call(call)}", file=sys.stderr)
                elif isinstance(message, ToolMessage) and message.status == "error":
                    print(
                        f"  ! {message.name} failed: {_truncate(str(message.content), 200)}",
                        file=sys.stderr,
                    )
    return final_text(messages)


async def cmd_once(
    settings: Settings,
    query: str,
    *,
    system_prompt: str,
    open_browser: bool,
    show_tools: bool,
    manual_paste: bool = False,
) -> int:
    async with atlassian_agent_session(
        settings,
        system_prompt=system_prompt,
        open_browser=open_browser,
        manual_paste=manual_paste,
    ) as holder:
        answer = await run_turn(holder, query, config=None, show_tool_calls=show_tools)
    print(answer or "(no answer)")
    return 0


async def cmd_repl(
    settings: Settings,
    *,
    system_prompt: str,
    open_browser: bool,
    show_tools: bool,
    manual_paste: bool = False,
) -> int:
    from langgraph.checkpoint.memory import InMemorySaver

    checkpointer = InMemorySaver()
    thread = 0
    async with atlassian_agent_session(
        settings,
        system_prompt=system_prompt,
        open_browser=open_browser,
        manual_paste=manual_paste,
        checkpointer=checkpointer,
    ) as holder:
        print(f"\n{REPL_BANNER}\n{len(holder.tools)} tools loaded.\n")
        while True:
            try:
                line = (await asyncio.to_thread(input, "you › ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            if line in {"/exit", "/quit"}:
                return 0
            if line == "/tools":
                for name in sorted(holder.tool_names):
                    print(f"  {name}")
                continue
            if line == "/reset":
                thread += 1
                print("Started a new conversation.")
                continue

            config = {"configurable": {"thread_id": f"cli-{thread}"}}
            try:
                answer = await run_turn(holder, line, config=config, show_tool_calls=show_tools)
            except KeyboardInterrupt:
                print("\n(interrupted)")
                continue
            print(f"\nagent › {answer or '(no answer)'}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        # The adapters log one line per tool load at INFO; keep the CLI quiet.
        logging.getLogger("atlassian_agent").setLevel(logging.WARNING)
        # The MCP SDK logs a full traceback for every failed OAuth attempt,
        # including ordinary ones like "you didn't finish in the browser".
        # We report the actionable message ourselves; -v brings the trace back.
        logging.getLogger("mcp.client.auth.oauth2").setLevel(logging.CRITICAL)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - optional convenience
        pass

    try:
        settings = settings_from_args(args)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    open_browser = not args.no_browser
    show_tools = not args.hide_tool_calls
    manual_paste = args.paste_code

    if args.logout:
        return await cmd_logout(settings)
    if args.status:
        return await cmd_status(settings)
    if args.login:
        return await cmd_login(settings, open_browser=open_browser, manual_paste=manual_paste)
    if args.list_tools:
        return await cmd_list_tools(settings, open_browser=open_browser, manual_paste=manual_paste)

    system_prompt = resolve_system_prompt(args)
    if args.query:
        return await cmd_once(
            settings,
            " ".join(args.query),
            system_prompt=system_prompt,
            open_browser=open_browser,
            show_tools=show_tools,
            manual_paste=manual_paste,
        )
    return await cmd_repl(
        settings,
        system_prompt=system_prompt,
        open_browser=open_browser,
        show_tools=show_tools,
        manual_paste=manual_paste,
    )


def explain(exc: BaseException) -> str:
    """The most useful message in an exception tree, for the terminal."""
    return _explain(exc, prefer=(OAuthCallbackError, ValueError, RuntimeError))


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        print(f"Error: {explain(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
