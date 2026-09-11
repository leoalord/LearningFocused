from datetime import datetime
from langchain.tools import tool, BaseTool
from typing import List, Any, Optional, Dict
import os
from langchain.messages import ToolMessage, AIMessage
from src.deep_research_agent.configuration import Configuration

async def get_all_tools(config):
    # Only return the real research tools; keep reflection inline to avoid tool bloat.
    from src.deep_research_agent.tools import tools
    return tools

def _resolve_registry_entry(model_name: str, config: Any) -> Dict[str, str]:
    cfg = Configuration.from_runnable_config(config)
    registry = getattr(cfg, "model_registry", {}) or {}
    return registry.get(model_name, {})

def get_api_key_for_model(model_name: str, config: Any) -> Optional[str]:
    entry = _resolve_registry_entry(model_name, config)
    env_var = entry.get("env_var")
    if env_var:
        return os.getenv(env_var) or None
    # Fallback: best-effort by provider
    provider = entry.get("provider", "")
    if provider == "openai":
        return os.getenv("OPENAI_API_KEY")
    if provider == "google_genai":
        return os.getenv("GOOGLE_API_KEY")
    if provider == "anthropic":
        return os.getenv("ANTHROPIC_API_KEY")
    return None

def get_model_provider(model_name: str) -> Optional[str]:
    """Infer provider for init_chat_model when using non-OpenAI models."""
    name = (model_name or "").lower()
    if "gemini" in name:
        return "google_genai"
    return None

def get_model_token_limit(model_name: str) -> int:
    # Placeholder
    return 100000

def build_model_config(model_name: str, max_tokens: int, config: Any, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    cfg = {
        "model": model_name,
        "max_tokens": max_tokens,
        "api_key": get_api_key_for_model(model_name, config),
        "tags": tags or ["langsmith:nostream"],
    }
    if provider := get_model_provider(model_name):
        cfg["model_provider"] = provider
    return cfg

def get_today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def is_token_limit_exceeded(e: Exception, model_name: str) -> bool:
    return "context length" in str(e).lower() or "token limit" in str(e).lower()

def openai_websearch_called(message):
    return False

def anthropic_websearch_called(message):
    return False

def get_notes_from_tool_calls(messages: List[Any]) -> List[str]:
    return [str(m.content) for m in messages if isinstance(m, ToolMessage)]

def build_tool_legend(tools: List[Any]) -> str:
    """Return a concise, human-readable legend of available tools."""
    if not tools:
        return "Tools available: none."
    entries = []
    for tool in tools:
        name = getattr(tool, "name", None) or tool.get("name", "unknown")  # type: ignore[attr-defined]
        desc = getattr(tool, "description", "") or getattr(tool, "__doc__", "") or ""
        desc = desc.strip().replace("\n", " ")
        if desc:
            entries.append(f"- {name}: {desc}")
        else:
            entries.append(f"- {name}")
    return "Tools available:\n" + "\n".join(entries)

def remove_up_to_last_ai_message(messages: List[Any]) -> List[Any]:
    # Find last AI message and remove everything up to it? 
    # Or maybe remove the last AI message and subsequent tool messages?
    # Simple strategy: remove the oldest pair of (AI, Tool) messages if possible
    if len(messages) > 2:
        return messages[2:]
    return messages
