"""Require human approval before the agent writes to Atlassian.

The agent reads freely, but anything that creates, edits, transitions or
deletes pauses and waits for you. This is the pattern to reach for once an
agent has write scopes on a real Jira or Confluence site.

`create_agent` takes LangChain middleware directly, so this is a matter of
passing `HumanInTheLoopMiddleware` through - the MCP tools need no special
handling.
"""

import asyncio

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from atlassian_agent import atlassian_agent_session

# Tool names vary by product, so gate on the verb rather than a hardcoded list.
WRITE_PREFIXES = ("create", "update", "edit", "delete", "add", "transition", "move", "assign")


def write_tools(tool_names: list[str]) -> dict[str, bool]:
    return {name: True for name in tool_names if name.lower().startswith(WRITE_PREFIXES)}


async def main() -> None:
    config = {"configurable": {"thread_id": "approvals"}}

    # One session to discover the tool names, then the real agent with the
    # approval gate configured from them.
    async with atlassian_agent_session() as probe:
        gated = write_tools(probe.tool_names)
    print(f"Requiring approval for {len(gated)} write tools.\n")

    async with atlassian_agent_session(
        middleware=[HumanInTheLoopMiddleware(interrupt_on=gated)],
        checkpointer=InMemorySaver(),
    ) as agent:
        payload = {"messages": [("user", "Create a Jira task called 'Test from the agent'.")]}

        while True:
            result = await agent.agent.ainvoke(payload, config=config)

            interrupts = result.get("__interrupt__")
            if not interrupts:
                print(result["messages"][-1].text())
                return

            decisions = []
            for request in interrupts[0].value["action_requests"]:
                print(f"\nThe agent wants to call {request['name']} with:")
                for key, value in request["args"].items():
                    print(f"  {key}: {value}")
                approved = input("Allow it? [y/N] ").strip().lower().startswith("y")
                decisions.append(
                    {"type": "approve"}
                    if approved
                    else {"type": "reject", "message": "The user declined this call."}
                )

            payload = Command(resume={"decisions": decisions})


if __name__ == "__main__":
    asyncio.run(main())
