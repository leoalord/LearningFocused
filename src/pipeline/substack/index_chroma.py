"""Substack pipeline -> Chroma indexing helpers.

Pipeline-owned logic:
- how we convert Substack artifacts (article summaries + article text) into Documents
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List

from langchain_core.documents import Document

from src.config import ARTICLE_SUMMARIES_DIR, SUBSTACK_METADATA_DIR, SUBSTACK_TEXT_DIR


def _load_json_file(file_path: str) -> Dict[str, Any]:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_article_summary_documents(article_summary: Dict[str, Any]) -> List[Document]:
    """Convert an article summary into Documents for embedding.

    Lean default: only emit an overview vector (1 per article) to avoid noisy/bloated indexing.
    """
    documents: List[Document] = []

    doc_id = article_summary.get("doc_id", "unknown")
    title = article_summary.get("title", "Unknown Article")
    canonical_url = article_summary.get("canonical_url")
    published_at = article_summary.get("published_at")
    author = article_summary.get("author")
    source = article_summary.get("source")

    content = article_summary.get("generated_content", {}) or {}
    thesis = content.get("thesis", "")
    overview = content.get("overview", "")
    themes = content.get("themes", []) or []
    # Note: we still *store* these fields in the JSON summary, but do not embed them by default.
    keywords = content.get("keywords", []) or []
    studies = content.get("studies_and_sources", []) or []

    base_meta = {
        "doc_id": doc_id,
        "title": title,
        "canonical_url": canonical_url,
        "published_at": published_at,
        "author": author,
        "source": source,
        "source_type": article_summary.get("source_type"),
    }

    overview_text = (
        f"Article: {title}\n"
        f"Thesis: {thesis}\n\n"
        f"Overview:\n{overview}\n\n"
        f"Themes: {', '.join(themes)}\n"
        f"Keywords: {', '.join(keywords)}\n"
    )
    if studies:
        overview_text += f"Studies/Sources mentioned: {', '.join(studies)}\n"

    # Use doc_id + type as unique identifier for ChromaDB deduplication
    document_id = f"article_summary_{doc_id}"
    documents.append(
        Document(
            page_content=overview_text,
            metadata={
                **base_meta,
                "type": "article_summary_overview",
                # Used to skip re-embedding unchanged documents.
                "chroma_content_hash": _sha256_text(overview_text),
            },
        )
    )
    # Store ID in metadata for later use when adding to ChromaDB
    documents[-1].metadata["_chroma_id"] = document_id

    return documents


def create_article_text_document(*, meta: Dict[str, Any], text: str) -> Document:
    """Create an article text Document (used as segment-like detailed content)."""
    title = meta.get("title", "Unknown Article")
    canonical_url = meta.get("canonical_url")
    published_at = meta.get("published_at")
    author = meta.get("author")
    source = meta.get("source")
    doc_id = meta.get("doc_id", "unknown")
    # Use doc_id + type as unique identifier for ChromaDB deduplication
    document_id = f"article_text_{doc_id}"
    doc = Document(
        page_content=f"Article: {title}\n\n{text}",
        metadata={
            "type": "article_text",
            "doc_id": doc_id,
            "title": title,
            "canonical_url": canonical_url,
            "published_at": published_at,
            "author": author,
            "source": source,
            "source_type": meta.get("source_type"),
            # Prefer artifact-level content hash if present; otherwise hash the exact text we embed.
            "chroma_content_hash": meta.get("content_hash") or _sha256_text(f"Article: {title}\n\n{text}"),
        },
    )
    # Store ID in metadata for later use when adding to ChromaDB
    doc.metadata["_chroma_id"] = document_id
    return doc


def collect_substack_documents() -> List[Document]:
    """Collect all Substack-derived Documents for Chroma indexing."""
    documents: List[Document] = []

    # Article summaries
    if ARTICLE_SUMMARIES_DIR.exists():
        for filename in sorted(os.listdir(ARTICLE_SUMMARIES_DIR)):
            if filename.endswith("_summary.json"):
                path = os.path.join(ARTICLE_SUMMARIES_DIR, filename)
                try:
                    data = _load_json_file(path)
                    documents.extend(create_article_summary_documents(data))
                except Exception:
                    continue

    # Article text
    if SUBSTACK_TEXT_DIR.exists() and SUBSTACK_METADATA_DIR.exists():
        for text_path in sorted(SUBSTACK_TEXT_DIR.glob("*.md")):
            meta_path = SUBSTACK_METADATA_DIR / f"{text_path.stem}.json"
            if not meta_path.exists():
                continue
            try:
                meta = _load_json_file(str(meta_path))
                text = text_path.read_text(encoding="utf-8").strip()
                if not text:
                    continue
                documents.append(create_article_text_document(meta=meta, text=text))
            except Exception:
                continue

    return documents


