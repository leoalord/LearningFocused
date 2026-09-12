# React Agent

A lightweight LangChain ReAct agent for querying the Future of Education podcast knowledge base.

## Overview

This agent implements the **ReAct pattern** (Reason → Act/tool call → Observe → repeat) using `langchain.agents.create_agent`. It's intended for fast, tool-grounded Q&A over the local stores (Chroma + Neo4j).

**Features:**
- **Conversation memory** via LangGraph's SQLite checkpointer (survives process restart)
- **Multi-provider support** (OpenAI, Anthropic, Google, Fireworks)
- **Streaming** tool calls and responses

**When to use this vs deep_research_agent:**
- **React Agent**: Quick questions, single-topic lookups, testing tools
- **Deep Research Agent**: Complex multi-part research, comprehensive reports, cross-episode synthesis

## File Structure

| File | Purpose |
|------|---------|
| `graph.py` | Main agent using `langchain.agents.create_agent` |
| `checkpointer.py` | SqliteSaver helper (gitignored `.checkpoints/react_agent.sqlite`) |
| `prompts.py` | System prompt guiding agent behavior |
| `configuration.py` | Settings (model, max iterations, etc.) with model registry |
| `utils.py` | Helper functions for model creation and tool management |
| `tools.py` | Re-exports shared tools (Chroma, Neo4j) |
| `chat_cli.py` | Interactive terminal interface |
| `__init__.py` | Package exports |

## Architecture

```
User Query
    │
    ▼
┌─────────────────────────────────────┐
│  ReAct Agent (create_agent)         │
│  ┌─────────────────────────────┐    │
│  │ 1. Reason: What do I need?  │    │
│  │ 2. Act: Call a tool         │◄───┼─── Tools:
│  │ 3. Observe: Read result     │    │    • search_knowledge_base (Chroma)
│  │ 4. Repeat or Answer         │    │    • query_knowledge_graph (Neo4j)
│  └─────────────────────────────┘    │    • inspect_graph_schema
└─────────────────────────────────────┘
    │
    ▼
  Answer
```

## Implementation Details

### Dynamic Schema Loading
The agent dynamically loads the Neo4j graph schema at startup (in `prompts.py`) to minimize tool calls. This allows the agent to write Cypher queries immediately without needing an initial `inspect_graph_schema` call, reducing latency and cost.

### Multi-Provider Support

Model creation is delegated to the shared factory in `src/llm/factory.py`, which supports multiple providers and validates the required API key env var for the chosen model.

Model names accept **stable aliases** (recommended) that are mapped to provider-specific IDs under the hood:
- **OpenAI**: `gpt-5`, `gpt-5-mini`
- **Anthropic**: `claude-sonnet-4-20250514` (alias), plus internal IDs like `claude-sonnet-4-5`
- **Google Gemini**: `gemini-flash-latest` (alias)

### Model Configuration

Models are configured via the `Configuration` class which supports:
- Model selection (default: `gemini-flash-latest`)
- Max tokens (default: 4000)
- Temperature (default: 0.0)
- Timeout (default: 30 seconds)
- Max iterations (default: 25)

Configuration can be overridden at runtime via `RunnableConfig`.

## Usage

### Interactive Chat

```bash
# Interactive chat (prints the full thread_id)
uv run python -m src.react_agent.chat_cli
```

### Replay a thread after restart

Checkpoints are stored at `.checkpoints/react_agent.sqlite` (gitignored; override with `REACT_AGENT_CHECKPOINT_PATH`). The CLI prints the full `thread_id`. Pass it back on the next process to continue the same conversation:

```bash
# Process 1 — start a chat and copy the printed thread_id
uv run python -m src.react_agent.chat_cli
# thread_id: 550e8400-e29b-41d4-a716-446655440000
# You: Remember that my favorite episode is about Two Hour Learning.
# ... type exit ...

# Process 2 — same thread_id continues the conversation
uv run python -m src.react_agent.chat_cli --thread-id 550e8400-e29b-41d4-a716-446655440000
# You: What did I say my favorite episode was?
```

You can also set `REACT_AGENT_THREAD_ID` instead of `--thread-id`.

### Programmatic Usage

```python
from src.react_agent.graph import react_agent, get_react_agent
from langchain_core.messages import HumanMessage

# Use default agent with durable SQLite conversation memory
# Pass a thread_id so the same conversation survives process restart
result = await react_agent.ainvoke(
    {"messages": [HumanMessage(content="What is Two Hour Learning?")]},
    config={"configurable": {"thread_id": "my-session-123"}}
)

# Follow-up questions in the same thread remember context
result = await react_agent.ainvoke(
    {"messages": [HumanMessage(content="Tell me more about that")]},
    config={"configurable": {"thread_id": "my-session-123"}}
)

# Create agent with custom config
from langchain_core.runnables import RunnableConfig

config = RunnableConfig(
    configurable={
        "model": "claude-sonnet-4-5",
        "max_tokens": 8000,
        "temperature": 0.1,
        "thread_id": "custom-thread"
    }
)
custom_agent = get_react_agent(config)
result = await custom_agent.ainvoke({
    "messages": [HumanMessage(content="What is Two Hour Learning?")]
}, config=config)
```

## Comparison with deep_research_agent

| Aspect | React Agent | Deep Research Agent |
|--------|-------------|---------------------|
| Framework | LangChain `create_agent` | LangGraph custom graph |
| Graph complexity | Single ReAct loop | Multi-node (clarify → brief → supervisor → researchers → report) |
| Use case | Quick Q&A | In-depth research |
| Tool access | Direct | Delegated via researchers |
| Output | Single response | Structured report |
| State | Minimal (messages + checkpointer memory) | Rich (brief, notes, iterations) |
| Memory | SqliteSaver checkpointer (disk, by thread_id) | Graph state |
| Model creation | Provider-specific classes | `init_chat_model` with configurable fields |

## Tools Available

The agent has access to these tools (re-exported from `deep_research_agent.tools`):

1. **search_knowledge_base(query)** - Semantic search over podcast transcripts and episode summaries in ChromaDB
2. **query_knowledge_graph(query)** - Run Cypher queries against the Neo4j knowledge graph
3. **inspect_graph_schema()** - Get the Neo4j schema (nodes, relationships) to help write Cypher

## Configuration

Default settings in `configuration.py`:
- Model: `gemini-flash-latest`
- Max iterations: 25 (safety limit)
- Max tokens: 4000
- Temperature: 0 (deterministic)
- Timeout: 30 seconds

Override via `RunnableConfig`:
```python
from langchain_core.runnables import RunnableConfig

config = RunnableConfig(
    configurable={
        "model": "gpt-5",
        "max_iterations": 5,
        "max_tokens": 8000,
        "temperature": 0.1
    }
)
result = await react_agent.ainvoke(input, config=config)
```

Or set environment variable:
```bash
export REACT_AGENT_DEFAULT_MODEL="claude-sonnet-4-20250514"
```

## Dependencies

Dependencies are managed in the repo’s `pyproject.toml`. The key runtime pieces are LangChain (+ provider integrations) and `python-dotenv`.
