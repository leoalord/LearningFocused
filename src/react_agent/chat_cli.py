"""React Agent CLI - Simple terminal chat interface.

Purpose:
    Interactive chat loop for testing the react agent.
    
Features:
    - Colored output (ANSI codes)
    - Streams tool calls and final responses
    - Conversation memory via LangGraph checkpointer (persists within session)
    - Type 'exit' or Ctrl+C to quit
    
Usage:
    uv run python -m src.react_agent.chat_cli
"""

import sys
import asyncio
import os
import uuid
from typing import Any, Sequence, Union

from dotenv import load_dotenv
from langchain.messages import HumanMessage, AIMessage, ToolMessage
from langchain_core.messages import BaseMessage
from langgraph.errors import GraphRecursionError
from langchain_core.runnables import RunnableConfig

from src.react_agent.graph import react_agent
from src.react_agent.configuration import Configuration

# Simple ANSI colors with auto-disable if not a TTY
SUPPORTS_COLOR = sys.stdout.isatty()
COLOR_RESET = "\033[0m" if SUPPORTS_COLOR else ""
COLOR_INFO = "\033[36m" if SUPPORTS_COLOR else ""       # cyan
COLOR_TOOL = "\033[33m" if SUPPORTS_COLOR else ""       # yellow
COLOR_SUCCESS = "\033[32m" if SUPPORTS_COLOR else ""    # green
COLOR_PROMPT = "\033[94m" if SUPPORTS_COLOR else ""     # blue
COLOR_DIM = "\033[90m" if SUPPORTS_COLOR else ""        # gray
COLOR_BOLD = "\033[1m" if SUPPORTS_COLOR else ""        # bold


def _truncate(text: str, limit: int = 600) -> str:
    """Truncate long tool outputs for terminal readability."""
    if len(text) <= limit:
        return text
    return text[:limit] + " ...[truncated]..."

def _format_ai_content(content: Any) -> str:
    """Normalize provider-specific message content into a readable string.

    Some providers (notably Gemini) may return content as a list of blocks like:
      [{"type": "text", "text": "...", "extras": {...}}]
    We extract the user-visible text and drop huge metadata payloads.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Common structured formats: list[dict] with "text"
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
            elif isinstance(item, str) and item.strip():
                parts.append(item)
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    # Fallback: string representation
    return str(content)


def _get_stream_mode() -> Union[str, Sequence[str]]:
    """Parse REACT_AGENT_STREAM_MODE env var."""
    value = os.environ.get("REACT_AGENT_STREAM_MODE")
    if not value:
        return ("updates", "values")
    raw = value.strip()
    if "," not in raw:
        return raw
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else ("updates", "values")


async def run_turn(user_input: str, thread_id: str) -> None:
    """Run one turn through the agent.
    
    The checkpointer automatically retrieves and stores conversation history
    based on the thread_id, so we only need to send the new user message.
    """
    stream_mode = _get_stream_mode()
    final_response: Any = None
    cfg = Configuration()
    
    # Include thread_id in configurable for checkpointer to manage history
    run_config = RunnableConfig(
        recursion_limit=cfg.max_iterations,
        configurable={"thread_id": thread_id},
    )

    # Track messages we've already displayed in this turn
    last_seen = 0
    latest_messages: list[BaseMessage] = []
    
    try:
        async for chunk in react_agent.astream(
            {"messages": [HumanMessage(content=user_input)]},
            config=run_config,
            stream_mode=stream_mode,
        ):
            # stream_mode="values" yields {"messages": [...]}
            if isinstance(chunk, dict) and "messages" in chunk:
                chunk_messages = chunk.get("messages") or []
                latest_messages = chunk_messages

                # Only process new messages appended since last chunk
                for msg in chunk_messages[last_seen:]:
                    if isinstance(msg, ToolMessage):
                        print(f"{COLOR_DIM}  └─ Result ({msg.name}): {_truncate(str(msg.content), 200)}{COLOR_RESET}")
                    elif isinstance(msg, AIMessage):
                        if getattr(msg, "tool_calls", None):
                            for tc in msg.tool_calls:
                                name = tc.get("name", "unknown")
                                args = tc.get("args", {})
                                print(f"{COLOR_TOOL}  ┌─ Call: {name}{COLOR_RESET}{COLOR_DIM}({args}){COLOR_RESET}")
                        elif msg.content:
                            final_response = msg.content
                            print(f"\n{COLOR_SUCCESS}=== Agent Response ==={COLOR_RESET}")
                            print(_format_ai_content(final_response))
                            print(f"{COLOR_SUCCESS}======================{COLOR_RESET}\n")

                last_seen = len(chunk_messages)

            # stream_mode="updates" yields per-node updates like {"model": ...} / {"tools": ...}
            elif isinstance(chunk, dict):
                pass
                
    except GraphRecursionError as e:
        print(
            f"{COLOR_INFO}Error: {e}{COLOR_RESET}\n"
            f"{COLOR_INFO}Tip: The agent hit its iteration limit. Try rephrasing, or increase REACT_AGENT_MAX_ITERATIONS.{COLOR_RESET}"
        )
        return
    
    # If we never printed a final response but we do have messages, try to print the last AI output.
    if final_response is None and latest_messages:
        for msg in reversed(latest_messages):
            if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None) and msg.content:
                final_response = msg.content
                print(f"\n{COLOR_SUCCESS}=== Agent Response ==={COLOR_RESET}")
                print(_format_ai_content(final_response))
                print(f"{COLOR_SUCCESS}======================{COLOR_RESET}\n")
                break


async def main():
    """Main chat loop."""
    load_dotenv()
    
    # Generate a unique thread ID for this session
    # The checkpointer uses this to store/retrieve conversation history
    thread_id = str(uuid.uuid4())
    
    print(f"{COLOR_BOLD}React Agent Chat{COLOR_RESET} (type 'exit' to quit)")
    print(f"{COLOR_DIM}Session: {thread_id[:8]}...{COLOR_RESET}")
    print(f"{COLOR_DIM}{'-'*50}{COLOR_RESET}")
    print()
    
    try:
        while True:
            # Get user input
            user_input = input(f"{COLOR_PROMPT}You{COLOR_RESET}: ").strip()
            
            if not user_input or user_input.lower() == "exit":
                print("Goodbye!")
                break
            
            # Run agent turn - checkpointer handles message history automatically
            await run_turn(user_input, thread_id)
            
            print(f"{COLOR_DIM}{'-'*80}{COLOR_RESET}")
            print()
            
    except KeyboardInterrupt:
        print("\n\nGoodbye!")
    except Exception as e:
        print(f"\n{COLOR_INFO}Error: {e}{COLOR_RESET}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
