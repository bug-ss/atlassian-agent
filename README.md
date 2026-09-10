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

On a headless box, `--no-browser` prints the URL for you to open elsewhere. The
redirect still has to reach `http://127.0.0.1:8901/oauth/callback` on the machine
running the agent - if it cannot, see
[When the loopback callback is blocked](#when-the-loopback-callback-is-blocked).

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

## When the loopback callback is blocked

If nothing can listen on `127.0.0.1:8901`, or the browser cannot reach it, you
have several options. Atlassian is permissive about redirect URIs - its
registration endpoint and `/authorize` both accept any loopback host, any port,
public HTTPS URLs, custom schemes and the OOB URN - so the constraint is almost
always local.

**1. Move the port.** Any port works.

```bash
ATLASSIAN_OAUTH_CALLBACK_PORT=53101 atlassian-agent --login
```

**2. Move the host.** The default is the literal `127.0.0.1` rather than
`localhost`, per RFC 8252 §8.3 - `localhost` depends on the resolver and often
resolves to `::1` first, which misses an IPv4-only listener. If your setup wants
the opposite, flip it:

```bash
ATLASSIAN_OAUTH_CALLBACK_HOST=localhost atlassian-agent --login   # or ::1
```

**3. Paste the code back instead of listening at all.** This binds no socket:

```bash
atlassian-agent --login --paste-code
```

It prints the URL, you approve in any browser on any machine, and paste the URL
you land on back into the terminal. That final page will fail to load - that is
expected and harmless; the code is in the address bar and that is all it needs.
Paste the **whole** URL: the `state` parameter is what proves the response
belongs to your login, and the flow refuses a bare code without it.

**4. Forward the port**, if the agent is on a remote host and the browser is
local:

```bash
ssh -L 8901:127.0.0.1:8901 you@remote-host
```

**5. Use a public HTTPS callback**, if you are running this behind a web service.
Point `ATLASSIAN_OAUTH_CALLBACK_*` at your own route and supply your own
handlers via `build_oauth_provider(redirect_handler=..., callback_handler=...)`.

**6. Skip OAuth entirely** with an Atlassian API token or service-account key,
if the agent should act as one service identity rather than on behalf of each
user. An org admin must first enable this under Atlassian Administration → Rovo
→ Rovo MCP server → Authentication. It is also the only way to reach Jira
Service Management tools, which do not support OAuth 2.1.

Note that **the device authorization grant is not an option** - Atlassian's
authorization server metadata does not list `device_code` among its supported
grant types.

Changing the host, port or path changes the registered redirect URI, so the next
login re-registers and re-consents. That is deliberate, not a bug.

## Configuration

Everything has a default; only `ANTHROPIC_API_KEY` is strictly required.

| Variable | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | – | Required for the default model |
| `ATLASSIAN_AGENT_MODEL` | `anthropic:claude-opus-5` | Any `init_chat_model` string |
| `ATLASSIAN_MCP_URL` | `https://mcp.atlassian.com/v2/mcp` | `/v1/sse` is retired after 2026-06-30 |
| `ATLASSIAN_OAUTH_SCOPES` | least-privilege set above | Space/comma separated, or `auto` |
| `ATLASSIAN_OAUTH_CALLBACK_HOST` | `127.0.0.1` | Part of the registered redirect URI |
| `ATLASSIAN_OAUTH_CALLBACK_PORT` | `8901` | Any port works; part of the redirect URI |
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

## Running it in Microsoft Teams

The `teams` extra hosts the agent as a Teams bot that acts **as whoever sent the
message**. Each person connects their own Atlassian account once; the bot then
answers with exactly their Jira and Confluence permissions, and nobody borrows
anyone else's.

```bash
pip install -e ".[teams]"     # Python 3.12+ - the Agents SDK requires it
atlassian-agent-teams          # or: python -m atlassian_agent.teams
```

### Read this before you deploy it

**Answers are posted into the chat they were asked in.** Atlassian's permission
model is per-user; a Teams channel is not. If someone asks about a restricted
issue in a channel, the bot answers *with their access* but *to everyone in the
room*. That is a deliberate choice of this configuration, not an oversight -
but it means the channel becomes as trusted as the most sensitive thing anyone
asks about. Each answer carries a line naming whose access produced it
(`TEAMS_SHOW_ATTRIBUTION=0` to remove it). To avoid it entirely, have people
message the bot in a 1:1 chat.

**Sign-in links are personal.** A link is signed, bound to one Teams identity,
valid for five minutes and usable once. Anyone who opens someone else's link
would attach *their* Atlassian account to *that person's* Teams identity, so the
card says so plainly. Hardening it further means delivering the link by direct
message rather than into the channel - `Proactive.create_conversation` in the
Agents SDK is the hook for that.

### Setup

1. **Create an Azure Bot** resource with a Microsoft Entra app registration
   (single- or multi-tenant). Note the app (client) ID, client secret and tenant
   ID, and set the messaging endpoint to `https://<your-host>/api/messages`.
2. **Deploy this app** anywhere that gives you a public HTTPS URL, with the
   environment variables from `.env.example` under the Teams heading. Generate
   the two secrets with the commands in the comments there and keep the
   encryption key backed up - losing it means every user reconnects.
3. **Package the Teams app** from `teams-manifest/` (see its README) and upload
   it, or have an admin publish it to your org.
4. **Add the bot** to your group chat or channel and `@mention` it.

The Atlassian redirect URI is `TEAMS_PUBLIC_BASE_URL` + `/oauth/atlassian/callback`.
Nothing to pre-register on the Atlassian side - the bot registers itself
dynamically on first use, and that one registration is shared by all users.

### How a turn works

```
@Atlassian Agent what changed on ENG-412?
   │
   ├─ identify the sender        Entra object ID + tenant, stable across chats
   ├─ look up THEIR grant        encrypted, per-user, found by that key
   │    └─ none? → sign-in card, bound to them, single-use, 5 minutes
   ├─ run create_agent           with an OAuth provider scoped to that person
   └─ post the answer in-channel with an attribution line
```

Commands: `help`, `status`, `disconnect`.

### Operational notes

- **One provider instance per user is cached and reused.** The MCP SDK
  serializes token refreshes on a lock held by the provider; building a fresh
  one per message would let two concurrent messages from the same person
  refresh independently and race.
- **The message path can never prompt.** Its OAuth handlers raise instead of
  opening a browser, so an unconnected user gets a card rather than a request
  that hangs until it times out.
- **Sign-in is a two-request handshake in one process.** The flow parks on a
  future that the callback route resolves, correlated by the `state` the MCP SDK
  generates. Running multiple replicas needs sticky routing or shared state -
  the token store is already pluggable, the in-flight login registry is not.
- **`/api/messages` is JWT-authenticated** by the Bot Framework middleware; the
  OAuth routes sit outside it and carry their own signed-state check.

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

Teams tests add sender identification, sign-in token forgery and expiry,
per-user credential isolation and encryption at rest, and the bot's HTTP surface
booted for real. They skip automatically without the `teams` extra. One test
exercises the live Atlassian handshake and is opt-in:

```bash
ATLASSIAN_LIVE_TESTS=1 pytest tests/test_teams_web.py
```

## Layout

```
src/atlassian_agent/
  config.py      settings and defaults
  oauth.py       token storage, loopback flow, scope pinning, provider
  mcp_client.py  the MCP connection, with OAuth on the transport
  agent.py       the create_agent harness
  errors.py      unwrapping anyio ExceptionGroups into readable causes
  cli.py         command line front end
  teams/
    identity.py  who sent the message; signed, single-use sign-in links
    store.py     encrypted per-user grants + the shared registration
    access.py    per-user providers and agents, and the sign-in handshake
    web.py       /oauth/atlassian/start and the callback route
    bot.py       message handlers
    app.py       aiohttp composition and entry point
teams-manifest/  Teams app package template
```
