"""Simple terminal chat loop for the Deep Research agent.

Usage:
    uv run python -m src.deep_research_agent.chat_cli

The script:
- Loads .env
- Starts an interactive prompt
- On each turn, sends the accumulated messages to the compiled graph
- Prints intermediate events (clarification, supervisor progress) and the final report if produced
"""

import sys
import asyncio
from typing import List, Any, cast

from dotenv import load_dotenv
from langchain.messages import HumanMessage, AIMessage
from langchain_core.messages import BaseMessage

from src.deep_research_agent.graph import deep_researcher

# Simple ANSI colors with auto-disable if not a TTY
SUPPORTS_COLOR = sys.stdout.isatty()
COLOR_RESET = "\033[0m" if SUPPORTS_COLOR else ""
COLOR_INFO = "\033[36m" if SUPPORTS_COLOR else ""       # cyan
COLOR_WARN = "\033[33m" if SUPPORTS_COLOR else ""       # yellow
COLOR_SUPERVISOR = "\033[35m" if SUPPORTS_COLOR else "" # magenta
COLOR_SUCCESS = "\033[32m" if SUPPORTS_COLOR else ""    # green
COLOR_PROMPT = "\033[94m" if SUPPORTS_COLOR else ""     # blue


def _format_event(key: str, value: Any) -> str:
    if key == "clarify_with_user":
        msgs = value.get("messages") or []
        if msgs:
            return f"{COLOR_INFO}[Clarify]{COLOR_RESET} {msgs[-1].content}"
    if key == "write_research_brief":
        brief = value.get("research_brief")
        if brief:
            return f"{COLOR_WARN}[Brief]{COLOR_RESET}\n{brief}"
    if key == "research_supervisor":
        return f"{COLOR_SUPERVISOR}[Supervisor]{COLOR_RESET} ..."
    if key == "final_report_generation":
        report = value.get("final_report")
        if report:
            return f"{COLOR_SUCCESS}[Final Report]{COLOR_RESET}\n{report}"
    return ""


async def run_turn(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Run one turn through the graph and return updated messages (if any)."""
    async for event in deep_researcher.astream(cast(Any, {"messages": messages})):
        for key, value in event.items():
            # Print readable updates
            out = _format_event(key, value)
            if out:
                print(out)
                print("-" * 60)  # visual separation between messages
            # Collect message updates if present
            if isinstance(value, dict) and "messages" in value:
                msgs = value.get("messages") or []
                # Keep all messages if provided, else ignore
                if msgs:
                    messages = msgs  # type: ignore[assignment]
    return messages


async def main():
    load_dotenv()
    print("Deep Research Chat (type 'exit' to quit)")
    print()
    messages: List[BaseMessage] = []
    # Initial user prompt
    user = input(f"{COLOR_PROMPT}You{COLOR_RESET}: ").strip()
    if not user or user.lower() == "exit":
        return
    messages.append(HumanMessage(content=user))

    while True:
        messages = await run_turn(messages)
        # Ask user for next input and append
        print()  # extra spacing before next prompt
        user = input(f"{COLOR_PROMPT}You{COLOR_RESET}: ").strip()
        if not user or user.lower() == "exit":
            break
        messages.append(HumanMessage(content=user))


if __name__ == "__main__":
    asyncio.run(main())

