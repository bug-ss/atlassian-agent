"""A multi-turn conversation over a single MCP session.

`atlassian_agent_session` keeps one authorized MCP session open for the whole
block, and the checkpointer keeps the conversation history, so follow-up
questions like "and who is assigned to it?" resolve against the earlier turns.
"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver

from atlassian_agent import atlassian_agent_session

TURNS = [
    "Which Atlassian sites can you reach?",
    "List the five most recently updated issues in the first site.",
    "Summarize what those issues have in common.",
]


async def main() -> None:
    async with atlassian_agent_session(checkpointer=InMemorySaver()) as agent:
        print(f"{len(agent.tools)} tools loaded.\n")
        for turn in TURNS:
            print(f"you   › {turn}")
            answer = await agent.ainvoke(turn, thread_id="demo")
            print(f"agent › {answer}\n")


if __name__ == "__main__":
    asyncio.run(main())
