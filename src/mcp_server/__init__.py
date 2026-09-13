"""Public FastMCP 4 Streamable HTTP server (search + fetch).

Local: `uv run python -m src.mcp_server` → http://127.0.0.1:8000/mcp
"""

from src.mcp_server.retrieval import fetch, search

__all__ = ["search", "fetch"]
