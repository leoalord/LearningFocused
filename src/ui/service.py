"""Server-side chat adapter: preflight checks, react-agent invoke, SourceChunk cards.

The browser never sees API keys. Source cards are `SourceChunk.model_dump()`
from `search_knowledge_base_structured` (see `src/deep_research_agent/tools.py`).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Iterable

from dotenv import load_dotenv
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from src.deep_research_agent.tools import SearchResults, SourceChunk
from src.llm.content import format_message_content
from src.react_agent.configuration import Configuration

load_dotenv()

SEARCH_TOOL_NAMES = frozenset(
    {"search_knowledge_base", "search_knowledge_base_structured"}
)

# Fields the UI renders — 1:1 with SourceChunk in deep_research_agent.tools.
SOURCE_CARD_FIELDS = (
    "kind",
    "title",
    "canonical_url",
    "doc_id",
    "episode_id",
    "topic",
    "start_time",
    "end_time",
    "group_title",
    "snippet",
    "metadata",
)


class ChatUIError(Exception):
    """Readable, UI-safe failure (missing key, missing/empty Chroma, etc.)."""

    def __init__(self, message: str, code: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


format_ai_content = format_message_content


def _tool_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return format_ai_content(content)


def parse_structured_payload(raw: Any) -> list[SourceChunk]:
    """Parse `search_knowledge_base_structured` JSON into SourceChunk models."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, SearchResults):
        return list(raw.summaries) + list(raw.segments)
    if isinstance(raw, SourceChunk):
        return [raw]
    data: Any = raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
    if isinstance(data, list):
        chunks: list[SourceChunk] = []
        for item in data:
            try:
                chunks.append(SourceChunk.model_validate(item))
            except Exception:
                continue
        return chunks
    if not isinstance(data, dict):
        return []
    try:
        results = SearchResults.model_validate(data)
        return list(results.summaries) + list(results.segments)
    except Exception:
        chunks = []
        for key in ("summaries", "segments"):
            for item in data.get(key) or []:
                try:
                    chunks.append(SourceChunk.model_validate(item))
                except Exception:
                    continue
        return chunks


def source_chunk_to_card(chunk: SourceChunk) -> dict[str, Any]:
    """Map a SourceChunk to the JSON object rendered as a source card."""
    dumped = chunk.model_dump()
    return {field: dumped.get(field) for field in SOURCE_CARD_FIELDS}


def _dedupe_chunks(chunks: Iterable[SourceChunk]) -> list[SourceChunk]:
    seen: set[tuple[Any, ...]] = set()
    out: list[SourceChunk] = []
    for chunk in chunks:
        key = (
            chunk.kind,
            chunk.doc_id,
            chunk.episode_id,
            chunk.canonical_url,
            (chunk.snippet or "")[:240],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(chunk)
    return out


def sources_from_messages(messages: Iterable[Any]) -> list[SourceChunk]:
    """Collect SourceChunks from structured-search tool messages."""
    chunks: list[SourceChunk] = []
    for msg in messages:
        name = getattr(msg, "name", None)
        if name != "search_knowledge_base_structured":
            continue
        chunks.extend(parse_structured_payload(getattr(msg, "content", None)))
    return _dedupe_chunks(chunks)


def tools_used_from_messages(messages: Iterable[Any]) -> list[str]:
    names: list[str] = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.name:
            names.append(msg.name)
            continue
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                if isinstance(tc, dict):
                    name = tc.get("name")
                else:
                    name = getattr(tc, "name", None)
                if name:
                    names.append(str(name))
    # Preserve order, drop duplicates
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def final_answer_from_messages(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            text = format_ai_content(msg.content)
            if text.strip():
                return text
    return ""


def llm_api_key_error() -> str | None:
    """Return a readable error if the configured model's env key is missing."""
    cfg = Configuration()
    model = cfg.model
    entry = (cfg.model_registry or {}).get(model) or {}
    env_var = entry.get("env_var") or "GOOGLE_API_KEY"
    if not os.getenv(env_var):
        return (
            f"Missing {env_var} for model '{model}'. "
            "Add it to the project-root .env. Keys stay on the server; "
            "the browser never receives them."
        )
    return None


def chroma_preflight_error() -> str | None:
    """Return a readable error if Chroma is missing or the collection is empty."""
    from src.config import CHROMA_DIR
    from src.database.chroma_manager import COLLECTION_NAME

    if not CHROMA_DIR.exists():
        return (
            f"Chroma directory not found at {CHROMA_DIR}. "
            "Index the knowledge base before using the chat UI."
        )

    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        try:
            collection = client.get_collection(name=COLLECTION_NAME)
        except Exception:
            return (
                f"Chroma collection '{COLLECTION_NAME}' was not found in {CHROMA_DIR}. "
                "Index the knowledge base before using the chat UI."
            )
        count = collection.count()
    except Exception as exc:
        return f"Could not open Chroma at {CHROMA_DIR}: {exc}"

    if count <= 0:
        return (
            f"Chroma collection '{COLLECTION_NAME}' is empty. "
            "Index podcast, Substack, or YouTube artifacts first."
        )
    return None


def preflight() -> None:
    """Raise ChatUIError if the UI cannot answer (missing key or Chroma)."""
    key_err = llm_api_key_error()
    if key_err:
        raise ChatUIError(key_err, code="missing_api_key")
    chroma_err = chroma_preflight_error()
    if chroma_err:
        code = "missing_chroma" if "not found" in chroma_err.lower() else "empty_chroma"
        if "empty" in chroma_err.lower():
            code = "empty_chroma"
        raise ChatUIError(chroma_err, code=code)


def health_payload() -> dict[str, Any]:
    """Status for the UI banner. Never includes secrets."""
    try:
        preflight()
    except ChatUIError as exc:
        return {"ok": False, "error": exc.message, "code": exc.code}
    return {"ok": True, "error": None, "code": None}


def _fallback_structured_sources(query: str) -> list[SourceChunk]:
    """Call the structured search tool so cards always map to SourceChunk."""
    from src.deep_research_agent.tools import search_knowledge_base_structured

    raw = search_knowledge_base_structured.invoke({"query": query})
    return parse_structured_payload(raw)


async def arun_chat(question: str, thread_id: str | None = None) -> dict[str, Any]:
    """Invoke the react agent and return answer + SourceChunk cards.

    Uses the durable SQLite checkpointer from TASK-7 when a thread_id is set.
    """
    cleaned = (question or "").strip()
    if not cleaned:
        raise ChatUIError("Enter a question.", code="empty_question", status_code=400)

    preflight()

    # Lazy import so the server can start (and the UI can show errors) without keys.
    from src.react_agent.graph import react_agent

    tid = (thread_id or "").strip() or str(uuid.uuid4())
    cfg = Configuration()
    run_config = RunnableConfig(
        recursion_limit=cfg.max_iterations,
        configurable={"thread_id": tid},
    )

    result = await react_agent.ainvoke(
        {"messages": [HumanMessage(content=cleaned)]},
        config=run_config,
    )
    messages = list((result or {}).get("messages") or [])
    tools_used = tools_used_from_messages(messages)
    tool_backed = any(name in SEARCH_TOOL_NAMES for name in tools_used)

    chunks = sources_from_messages(messages)
    if not chunks and tool_backed:
        chunks = _fallback_structured_sources(cleaned)
    elif not chunks:
        # Still try to show citations if the store has hits, even if the model skipped tools.
        try:
            chunks = _fallback_structured_sources(cleaned)
        except Exception:
            chunks = []

    answer = final_answer_from_messages(messages)
    if not answer:
        answer = "The agent returned no text. Try asking again."

    cards = [source_chunk_to_card(c) for c in _dedupe_chunks(chunks)]
    return {
        "answer": answer,
        "sources": cards,
        "thread_id": tid,
        "tool_backed": tool_backed,
        "tools_used": tools_used,
    }
