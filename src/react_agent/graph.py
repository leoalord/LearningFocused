"""React Agent Graph - Main agent implementation.

Purpose:
    Defines a simple ReAct agent using `create_agent` from langchain.agents.
    The agent loops: Reason → Act (call tool) → Observe → Repeat until done.
    
Key Components:
    - create_agent: LangChain helper for an agent runtime (built on LangGraph)
    - Tools: Imported from shared tools module (Chroma, Neo4j)
    - Model: Configurable LLM (default: gemini-flash-latest)
    - Checkpointer: SqliteSaver on a gitignored path (survives process restart)
    
Usage:
    from src.react_agent.graph import react_agent
    result = await react_agent.ainvoke(
        {"messages": [HumanMessage(content="...")]},
        config={"configurable": {"thread_id": "my-thread"}}
    )
"""

from typing import Any, Optional
from langchain.agents import create_agent
from langchain.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from src.react_agent.configuration import Configuration
from src.react_agent.tools import tools
from src.react_agent.utils import create_chat_model
from src.react_agent.prompts import system_prompt
from src.react_agent.checkpointer import get_sqlite_checkpointer

# Production checkpointer: SQLite on a gitignored path (not MemorySaver).
memory = get_sqlite_checkpointer()


def get_react_agent(
    config: Optional[RunnableConfig] = None,
    checkpointer: Optional[Any] = None,
) -> Any:
    """Create a ReAct agent with the specified configuration.
    
    Args:
        config: Optional RunnableConfig with configurable dict (model, max_tokens, etc.)
        checkpointer: Optional checkpointer for conversation memory. Defaults to
            the production SqliteSaver. Tests may inject MemorySaver. Pass a
            thread_id in config to enable memory:
            config={"configurable": {"thread_id": "my-thread"}}
        
    Returns:
        LangChain agent runnable that accepts {"messages": [...]}
    """
    cfg = Configuration.from_runnable_config(config)
    
    # Create the chat model instance
    model = create_chat_model(
        model_name=cfg.model,
        config=config,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        timeout=cfg.timeout,
    )
    
    # Create agent with model, tools, and checkpointer for conversation memory
    # create_agent returns a compiled graph that acts as a runnable
    agent: Any = create_agent(
        model,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=checkpointer if checkpointer is not None else memory,
    )

    # Respect configured iteration limits; default to Configuration.max_iterations
    recursion_limit = None
    if isinstance(config, dict):
        recursion_limit = config.get("recursion_limit")
    if recursion_limit is None:
        recursion_limit = cfg.max_iterations

    return agent.with_config(recursion_limit=recursion_limit)


# Create default agent instance with durable SQLite checkpointer
react_agent = get_react_agent(checkpointer=memory)
