# atlassian-agent

A [LangChain `create_agent`](https://docs.langchain.com/oss/python/langchain/agents)
harness wired to the [Atlassian Rovo MCP Server](https://github.com/atlassian/atlassian-mcp-server),
authorized with OAuth 2.1.

Ask it about Jira and Confluence in English; it discovers Atlassian's tools over
MCP and calls them on your behalf. The first run sends you through Atlassian's
consent screen in a browser; after that a cached refresh token keeps it silent.

```
$ atlassian-agent "What changed on ENG-412 this week?"
  → <tool calls stream to stderr as the agent makes them>
ENG-412 moved from In Progress to In Review on Monday...
```

(The exact tool names come from the server at runtime - run
`atlassian-agent --list-tools` to see the catalog your site exposes.)

## How it fits together

```
       your question
            │
            ▼
  create_agent  ──────────────── the LangChain agent loop (model ⇄ tools)
            │
            ▼
  langchain-mcp-adapters ─────── MCP tools as LangChain tools
            │
            ▼
  MCP Streamable HTTP transport
            │  auth=OAuthClientProvider   ← an httpx.Auth, so the whole OAuth
            ▼                                dance lives inside the transport
  https://mcp.atlassian.com/v2/mcp
```

The interesting seam is the `auth` field on the MCP connection. It accepts any
`httpx.Auth`, and the MCP SDK's `OAuthClientProvider` is one. So discovery,
dynamic client registration, PKCE, token exchange and refresh all happen below
the agent - nothing in the agent loop knows a token exists.

What this project adds around that: persistent token storage, the loopback
browser flow, a least-privilege scope policy that survives the SDK's defaults,
and a CLI.

## Quick start

Requires Python 3.11+ and an Atlassian Cloud account.

```bash
pip install -e .                      # or: uv pip install -e .
cp .env.example .env                  # then set ANTHROPIC_API_KEY
atlassian-agent --login               # opens the browser once
atlassian-agent                       # interactive session
```

One-shot:

```bash
atlassian-agent "List my open bugs in the Platform project"
```

On a headless box, `--no-browser` prints the URL for you to open elsewhere.
The redirect still has to reach `http://localhost:8901/oauth/callback` on the
machine running the agent, so forward that port if you are on SSH.

## Using it as a library

The short version - tools open a fresh MCP session per call:

```python
from atlassian_agent import create_atlassian_agent

agent = await create_atlassian_agent()
print(await agent.ainvoke("Summarize the last 5 issues I commented on"))
```

For a conversation, hold one MCP session open for the whole exchange:

```python
from langgraph.checkpoint.memory import InMemorySaver
from atlassian_agent import atlassian_agent_session

async with atlassian_agent_session(checkpointer=InMemorySaver()) as agent:
    await agent.ainvoke("Which sites can you reach?", thread_id="t1")
    await agent.ainvoke("List open bugs in the first one", thread_id="t1")
```

`agent.agent` is the compiled graph from `create_agent`, so everything LangChain
offers still applies - streaming, middleware, structured output, checkpointers,
embedding it in a larger LangGraph app:

```python
from langchain.agents.middleware import HumanInTheLoopMiddleware, ToolCallLimitMiddleware

async with atlassian_agent_session(
    model="anthropic:claude-opus-5",
    system_prompt="You only read. Never write.",
    middleware=[
        ToolCallLimitMiddleware(thread_limit=25),
        # Names come from --list-tools; approve_writes.py matches them by verb.
        HumanInTheLoopMiddleware(interrupt_on={"createJiraIssue": True}),
    ],
    extra_tools=[my_own_tool],
) as agent:
    ...
```

See `examples/`:

| File | Shows |
| --- | --- |
| `one_shot.py` | The smallest working agent |
| `interactive_session.py` | Multi-turn over one MCP session |
| `approve_writes.py` | Human approval before any write reaches Atlassian |

## OAuth, concretely

On the first request the server answers `401` with a `WWW-Authenticate` header,
and the SDK works forward from there:

1. Fetch protected-resource metadata from `mcp.atlassian.com`, which names
   `auth.atlassian.com` as the authorization server.
2. Fetch that server's metadata - it advertises a registration endpoint and
   `token_endpoint_auth_method: none`.
3. **Register dynamically.** No client ID to create by hand, no Atlassian
   developer-console app. You get a public client, PKCE only, no secret.
4. Open the browser for consent, receive the code on `localhost:8901`,
   exchange it for tokens.
5. Cache tokens and the registration in `~/.atlassian-agent/tokens.json`
   (mode `0600`).

Later runs read the cache. When the access token is stale the refresh token
renews it in the background - no browser - which is why `offline_access` is in
the default scopes. `atlassian-agent --status` shows what is cached;
`--logout` forgets it.

### Scopes are pinned deliberately

The MCP SDK implements the spec's *scope selection strategy*: at the `401` it
**replaces** whatever scope you configured with the scope from
`WWW-Authenticate`, or - when that header carries none, which is Atlassian's
case - with every scope the server advertises. For Atlassian that is the entire
catalog, `delete:jira:agent-interface` and `manage:jira:agent-interface`
included, and that widened list is what reaches both the client registration
and the consent screen.

An agent asked to read Jira should not ask the user to grant deletion rights, so
`PinnedScopeClientMetadata` (in `oauth.py`) ignores that overwrite and keeps the
configured set. Verified against the live authorization server - the request
carries exactly:

```
offline_access read:me read:account
read:jira:agent-interface  write:jira:agent-interface  search:jira:agent-interface
read:confluence:agent-interface write:confluence:agent-interface search:confluence:agent-interface
search:rovo:agent-interface
```

Set `ATLASSIAN_OAUTH_SCOPES=auto` to drop the pin and take whatever the server
advertises. Set an explicit list to go narrower - drop every `write:*` for a
read-only agent. Changing scopes invalidates the cached login on purpose, so the
next run re-registers and re-consents rather than replaying a token that no
longer matches.

## Configuration

Everything has a default; only `ANTHROPIC_API_KEY` is strictly required.

| Variable | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | – | Required for the default model |
| `ATLASSIAN_AGENT_MODEL` | `anthropic:claude-opus-5` | Any `init_chat_model` string |
| `ATLASSIAN_MCP_URL` | `https://mcp.atlassian.com/v2/mcp` | `/v1/sse` is retired after 2026-06-30 |
| `ATLASSIAN_OAUTH_SCOPES` | least-privilege set above | Space/comma separated, or `auto` |
| `ATLASSIAN_OAUTH_CALLBACK_PORT` | `8901` | Part of the registered redirect URI |
| `ATLASSIAN_OAUTH_CALLBACK_PATH` | `/oauth/callback` | |
| `ATLASSIAN_OAUTH_TIMEOUT` | `300` | Seconds to finish the browser flow |
| `ATLASSIAN_TOKEN_CACHE` | `~/.atlassian-agent/tokens.json` | Written `0600` |
| `ATLASSIAN_MCP_TIMEOUT` | `60` | Per-request HTTP timeout |
| `ATLASSIAN_MCP_SSE_READ_TIMEOUT` | `300` | Idle SSE read timeout |

CLI flags `--model`, `--mcp-url`, `--scopes`, `--system-prompt[-file]`,
`--no-browser`, `--hide-tool-calls` and `-v` override the environment.

## CLI

```
atlassian-agent [query ...]      ask once, or start a REPL with no query
  --login                        authorize now and report the tool count
  --logout                       forget cached tokens and registration
  --status                       what is cached, and when it expires
  --list-tools                   list the tools the server exposes
  -v                             debug logging, including the MCP SDK's
```

In the REPL: `/tools`, `/reset`, `/exit`.

## Notes and limitations

- **Verified against the live server**, end to end through registration, PKCE
  and the authorization URL. The final consent click and the tool calls after it
  need a real Atlassian account and a browser, so those paths are exercised by
  unit tests rather than by CI against production.
- **Scopes your site has not enabled** cause Atlassian to reject the
  authorization with `invalid_scope`. The agent surfaces the server's reason;
  trim `ATLASSIAN_OAUTH_SCOPES` to the products you actually use.
- **Delete and manage tools** are disabled by default on the Atlassian side and
  need an org admin to enable them. They are also outside the default scopes
  here, so enabling them takes two deliberate steps.
- **Jira Service Management tools require API-token auth**, not OAuth, per
  Atlassian's docs - they are not reachable through this flow.
- **The token cache is plaintext JSON** at mode `0600`, matching what most MCP
  clients do. Point `ATLASSIAN_TOKEN_CACHE` at an encrypted volume, or
  substitute your own `TokenStorage`, if you need more.
- **The default model is `claude-opus-5`**, whose adaptive thinking is on when
  the `thinking` parameter is omitted. Pass a configured `BaseChatModel` to
  `create_atlassian_agent(model=...)` to change that or switch providers.

## Development

```bash
uv venv && . .venv/bin/activate
uv pip install -e ".[dev]"
pytest -q          # 70 tests, no network
ruff check src tests examples && ruff format --check src tests examples
```

The tests cover the token cache (round-trip, permissions, expiry persistence,
corrupt files, fingerprint invalidation), the loopback callback server (real
HTTP requests, error params, stray requests, timeouts, busy ports), scope
pinning, error unwrapping, and the agent loop itself via a scripted model - no
Atlassian account needed.

## Layout

```
src/atlassian_agent/
  config.py      settings and defaults
  oauth.py       token storage, loopback flow, scope pinning, provider
  mcp_client.py  the MCP connection, with OAuth on the transport
  agent.py       the create_agent harness
  cli.py         command line front end
```
