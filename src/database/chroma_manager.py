import argparse
from typing import List, Dict, Any, Optional, cast
from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document

# Load environment variables
load_dotenv()

# Constants
from src.config import (
    CHROMA_DIR,
    ensure_data_dirs,
)

COLLECTION_NAME = "education_knowledge_engine"

# Retrieval defaults used when tools omit allowed_types.
DEFAULT_SEGMENT_TYPES = ["transcript_segment", "article_text", "youtube_transcript_segment"]
DEFAULT_SUMMARY_TYPES = [
    "series_overview",
    "series_motivation",
    "key_takeaway",
    "article_summary_overview",
    "youtube_summary_overview",
]

def get_vector_store(*, create_if_missing: bool = True) -> Chroma:
    """Initialize and return the ChromaDB vector store."""
    if create_if_missing:
        ensure_data_dirs()
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
        persist_directory=str(CHROMA_DIR),
        create_collection_if_not_exists=create_if_missing,
    )

def query_segments(
    query: str,
    k: int = 5,
    filter_metadata: Optional[Dict[str, Any]] = None,
    *,
    allowed_types: Optional[List[str]] = None,
    vector_store: Optional[Chroma] = None,
) -> List[Document]:
    """
    Search for detailed content segments in the vector store.
    Defaults to podcast transcript segments, Substack article text, and YouTube transcript segments.
    
    Args:
        query: The search query string.
        k: Number of results to return.
        filter_metadata: Optional dictionary to filter by metadata (e.g., {'episode_id': '...'}).
    """
    vector_store = vector_store or get_vector_store()
    
    types = allowed_types or list(DEFAULT_SEGMENT_TYPES)
    # Prefer server-side filtering, but fall back to client-side if the backend doesn't support $in.
    filter_dict: Dict[str, Any] = {"type": {"$in": types}}
    if filter_metadata:
        filter_dict.update(filter_metadata)

    try:
        return vector_store.similarity_search(query, k=k, filter=cast(Any, filter_dict))
    except Exception:
        docs = vector_store.similarity_search(query, k=k * 3)
        filtered = [d for d in docs if d.metadata.get("type") in types]
        if filter_metadata:
            def _match_meta(d: Document) -> bool:
                for mk, mv in filter_metadata.items():
                    if d.metadata.get(mk) != mv:
                        return False
                return True
            filtered = [d for d in filtered if _match_meta(d)]
        return filtered[:k]

def query_summaries(
    query: str,
    k: int = 5,
    filter_metadata: Optional[Dict[str, Any]] = None,
    *,
    allowed_types: Optional[List[str]] = None,
    vector_store: Optional[Chroma] = None,
) -> List[Document]:
    """
    Search for episode and series summaries.
    Excludes transcript segments to focus on high-level content.
    """
    vector_store = vector_store or get_vector_store()

    # Summary-like doc types across audio + Substack + YouTube. Keep this conservative so
    # "summaries" doesn't accidentally return full article/transcript text.
    types = allowed_types or list(DEFAULT_SUMMARY_TYPES)

    # Prefer server-side $in filtering, but fall back to client-side if unsupported.
    filter_dict: Dict[str, Any] = {"type": {"$in": types}}
    if filter_metadata:
        filter_dict.update(filter_metadata)

    try:
        return vector_store.similarity_search(query, k=k, filter=cast(Any, filter_dict))
    except Exception:
        docs = vector_store.similarity_search(query, k=k * 3)
        filtered = [d for d in docs if d.metadata.get("type") in types]
        if filter_metadata:
            def _match_meta(d: Document) -> bool:
                for mk, mv in filter_metadata.items():
                    if d.metadata.get(mk) != mv:
                        return False
                return True
            filtered = [d for d in filtered if _match_meta(d)]
        return filtered[:k]

def update_chroma_db(
    reset: bool = False,
    *,
    include_audio: bool = True,
    include_articles: bool = True,
    include_youtube: bool = False,
    confirm_reset: str | None = None,
    prune_stale: bool = False,
):
    """Backwards-compatible wrapper for pipeline indexing.

    Note: Indexing orchestration lives in `src/pipeline/index_chroma.py` so this module
    can remain a thin DB adapter (connect/query helpers) for agent tools.
    """
    from src.pipeline.index_chroma import update_chroma_db as _update

    _update(
        reset=reset,
        include_audio=include_audio,
        include_articles=include_articles,
        include_youtube=include_youtube,
        confirm_reset=confirm_reset,
        prune_stale=prune_stale,
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate embeddings for Knowledge Engine.")
    parser.add_argument("--reset", action="store_true", help="Delete existing ChromaDB collection before starting")
    parser.add_argument(
        "--prune-stale",
        action="store_true",
        help=(
            "Delete segment rows for collected sources that on-disk artifacts no longer "
            "produce. This CLI still collects audio and Substack by default, so prune "
            "deletes those types too. For a youtube-only pass (e.g. after a YouTube "
            "document-id change), use python -m src.pipeline.youtube.run --prune-stale-chroma. "
            "Only safe when this machine holds the complete artifact set for the sources "
            "being collected."
        ),
    )
    parser.add_argument(
        "--include-youtube",
        action="store_true",
        help=(
            "Also collect/index YouTube documents (required to prune "
            "youtube_transcript_segment rows). Does not turn off audio/Substack collection."
        ),
    )
    args = parser.parse_args()

    update_chroma_db(
        args.reset,
        include_youtube=args.include_youtube,
        prune_stale=args.prune_stale,
    )
