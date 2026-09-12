"""React Agent Tools - Tool definitions and exports.

Purpose:
    Re-exports the shared tools from src.deep_research_agent.tools.
    This keeps tools DRY while allowing react_agent-specific overrides if needed.
    
Available Tools (from shared module):
    - search_knowledge_base: Semantic search over podcast, Substack, and YouTube docs (Chroma)
    - query_knowledge_graph: Run Cypher queries against Neo4j
    - inspect_graph_schema: Get Neo4j schema for writing Cypher
    
Design Notes:
    - For now, we simply re-export; if react_agent needs different tools, add them here
    - Tools are defined with @tool decorator and have docstrings the LLM can read
"""

# Re-export tools from the shared location
from src.deep_research_agent.tools import (
    tools,
    search_knowledge_base,
    search_knowledge_base_structured,
    query_knowledge_graph,
    inspect_graph_schema,
)

__all__ = [
    "tools",
    "search_knowledge_base",
    "search_knowledge_base_structured",
    "query_knowledge_graph",
    "inspect_graph_schema",
]
