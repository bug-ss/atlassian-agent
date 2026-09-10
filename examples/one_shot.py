"""Smallest useful thing: authorize, load Atlassian tools, ask one question.

    python examples/one_shot.py "What are my open Jira issues?"

The first run opens a browser for consent. After that the cached refresh token
keeps it silent.
"""

import asyncio
import sys

from atlassian_agent import create_atlassian_agent


async def main() -> None:
    default = (
        "Which Atlassian sites can you reach, and what are my most recently updated Jira issues?"
    )
    question = " ".join(sys.argv[1:]) or default

    agent = await create_atlassian_agent()
    print(f"{len(agent.tools)} Atlassian tools loaded.\n")

    answer = await agent.ainvoke(question)
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
