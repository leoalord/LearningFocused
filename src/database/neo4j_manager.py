"""Neo4j DB adapter (thin).

LangSmith tracing
To enable traces for the LLM transform + Neo4j write steps, set (e.g., in `.env`):
- LANGCHAIN_TRACING_V2=true
- LANGCHAIN_API_KEY=...            # LangSmith API key
- LANGCHAIN_PROJECT=learningfocused-neo4j

Optional:
- LANGCHAIN_ENDPOINT=https://api.smith.langchain.com
- NEO4J_GRAPH_LLM_TIMEOUT_SECONDS=120  # fail fast instead of hanging for many minutes
"""

import os
import logging
from typing import Any, Optional, cast
from dotenv import load_dotenv

from langchain_neo4j import Neo4jGraph

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

def _get_neo4j_env() -> tuple[str, str, str]:
    """Return Neo4j connection env vars or raise a helpful error."""
    uri = os.getenv("NEO4J_URI")
    username = os.getenv("NEO4J_USERNAME")
    password = os.getenv("NEO4J_PASSWORD")
    if not all([uri, username, password]):
        raise ValueError(
            "Missing Neo4j configuration. Please check your .env file.\n"
            "Required: NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD"
        )
    return uri, username, password

def get_graph() -> Neo4jGraph:
    """Initialize and return the Neo4jGraph connection."""
    uri, username, password = _get_neo4j_env()
    return Neo4jGraph(
        url=uri,
        username=username,
        password=password,
    )

def run_cypher_query(query: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Execute a Cypher query against the knowledge graph."""
    graph = get_graph()
    try:
        # Neo4jGraph typing expects dict[Any, Any]; normalize Optional/typed dict to satisfy type checkers.
        return graph.query(query, params=cast(dict[Any, Any], (params or {})))
    except Exception as e:
        logger.error(f"Cypher query failed: {e}")
        return []

def get_graph_schema() -> str:
    """Return the schema of the knowledge graph."""
    graph = get_graph()
    return graph.schema

def update_knowledge_graph(
    *,
    include_audio: bool = True,
    include_articles: bool = True,
    audio_limit: int | None = None,
    article_limit: int | None = None,
    schema_profile: str | None = None,
):
    """Backwards-compatible wrapper for pipeline indexing.

    Graph extraction now writes to the Postgres AGE graph (`src/pipeline/index_graph.py`),
    not to Neo4j; this wrapper only forwards to it.
    """
    from src.pipeline.index_graph import update_knowledge_graph as _update

    _update(
        include_audio=include_audio,
        include_articles=include_articles,
        audio_limit=audio_limit,
        article_limit=article_limit,
        schema_profile=schema_profile,
    )

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Build/update Neo4j knowledge graph from pipeline artifacts.")
    p.add_argument("--schema-profile", choices=["core", "extended"], default=None)
    p.add_argument("--include-audio", action="store_true", help="Include audio transcript segments (default: on).")
    p.add_argument("--no-include-audio", action="store_true", help="Exclude audio transcript segments.")
    p.add_argument("--include-articles", action="store_true", help="Include Substack article text docs (default: on).")
    p.add_argument("--no-include-articles", action="store_true", help="Exclude Substack article text docs.")
    p.add_argument("--audio-limit", type=int, default=None, help="Max number of audio Documents to transform/write.")
    p.add_argument("--article-limit", type=int, default=None, help="Max number of article Documents to transform/write.")
    args = p.parse_args()

    include_audio = not args.no_include_audio
    include_articles = not args.no_include_articles
    update_knowledge_graph(
        include_audio=include_audio,
        include_articles=include_articles,
        audio_limit=args.audio_limit,
        article_limit=args.article_limit,
        schema_profile=args.schema_profile,
    )
