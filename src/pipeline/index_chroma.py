"""Cross-pipeline Chroma indexing orchestrator.

This module owns *how we index* (batching, reset semantics, scoping) while the
pipeline-specific modules own *what to index* (collecting/converting artifacts).

Why this exists:
- Keep `src/database/chroma_manager.py` thin (connect/query/upsert helpers for tools).
- Keep pipeline-specific document construction in:
  - `src/pipeline/audio/index_chroma.py`
  - `src/pipeline/substack/index_chroma.py`
"""

from __future__ import annotations

import os
import shutil
from typing import cast

from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma

from src.config import CHROMA_DIR, ensure_data_dirs

COLLECTION_NAME = "education_knowledge_engine"

# Note: historically we produced multiple Substack vector types. Today we primarily use
# `article_text` + `article_summary_overview`, but cleanup should remove all `article_*` docs.
SUBSTACK_TYPE_PREFIX = "article_"
AUDIO_CHROMA_TYPES = ["transcript_segment", "series_overview", "series_motivation", "key_takeaway"]
YOUTUBE_CHROMA_TYPES = ["youtube_transcript_segment", "youtube_summary_overview"]

DESTRUCTIVE_OPS_ENV = "LEARNINGFOCUSED_ALLOW_DESTRUCTIVE_OPS"
RESET_CHROMA_CONFIRM_TOKEN = "DELETE_ALL_CHROMA"


def _delete_by_type(matches_type, label: str) -> int:
    """Delete documents whose `type` metadata satisfies `matches_type`.

    Scans ids+metadatas rather than using a server-side filter, which avoids
    backend-specific filter limitations and also catches legacy type names.
    Scoped deletion is safer than `reset=True`, which wipes the whole collection.
    """
    ensure_data_dirs()
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vector_store = Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
        persist_directory=str(CHROMA_DIR),
    )

    try:
        to_delete: list[str] = []
        offset = 0
        page_size = 2000
        while True:
            data = vector_store._collection.get(  # type: ignore[attr-defined]
                include=["metadatas"],
                limit=page_size,
                offset=offset,
            )
            ids = data.get("ids") or []
            metas = data.get("metadatas") or []
            if not ids:
                break
            for _id, meta in zip(ids, metas):
                if not isinstance(meta, dict):
                    continue
                t = meta.get("type")
                if isinstance(t, str) and matches_type(t):
                    to_delete.append(str(_id))
            offset += len(ids)

        if to_delete:
            vector_store._collection.delete(ids=to_delete)  # type: ignore[attr-defined]
        print(f"Deleted {len(to_delete)} documents from Chroma ({label}).")
        return len(to_delete)
    except Exception as e:
        raise RuntimeError(f"Failed to delete {label} docs from Chroma: {e}") from e


def delete_substack_from_chroma() -> None:
    """Delete only Substack-derived documents from Chroma (keeps audio vectors)."""
    _delete_by_type(
        lambda t: t.startswith(SUBSTACK_TYPE_PREFIX),
        f"type startswith '{SUBSTACK_TYPE_PREFIX}'",
    )


def delete_audio_from_chroma() -> None:
    """Delete only audio-derived documents from Chroma (keeps Substack vectors).

    Useful for one-time cleanup if legacy runs created duplicates before stable IDs.
    """
    audio_types = set(AUDIO_CHROMA_TYPES)
    _delete_by_type(lambda t: t in audio_types, f"types: {AUDIO_CHROMA_TYPES}")


def delete_transcript_segments_from_chroma() -> int:
    """Delete only `transcript_segment` docs (keeps podcast summaries, Substack, YouTube).

    Needed when the document id scheme changes: re-indexing writes new ids, so the
    rows under the old ids would otherwise linger as orphans.
    """
    return _delete_by_type(
        lambda t: t == "transcript_segment", "type: transcript_segment"
    )


def prune_stale_segments(keep_ids: set[str], doc_type: str = "transcript_segment") -> int:
    """Delete `doc_type` rows whose id is not in `keep_ids`.

    Upserting cannot remove rows, so a change to the document id scheme (or a
    deleted episode) would otherwise leave the old rows searchable alongside the
    new ones.

    This DELETES data, so callers must pass the ids for the *whole* corpus of
    `doc_type`. `update_chroma_db` only calls this when explicitly asked and when
    every artifact on disk parsed, because the collectors skip unreadable files and
    a partial id set would otherwise delete every episode it could not read.
    """
    if not keep_ids:
        return 0

    ensure_data_dirs()
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vector_store = Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
        persist_directory=str(CHROMA_DIR),
    )

    stale: list[str] = []
    offset = 0
    page_size = 2000
    while True:
        data = vector_store._collection.get(  # type: ignore[attr-defined]
            include=["metadatas"],
            limit=page_size,
            offset=offset,
        )
        ids = data.get("ids") or []
        metas = data.get("metadatas") or []
        if not ids:
            break
        for _id, meta in zip(ids, metas):
            if not isinstance(meta, dict):
                continue
            if meta.get("type") == doc_type and str(_id) not in keep_ids:
                stale.append(str(_id))
        offset += len(ids)

    if stale:
        vector_store._collection.delete(ids=stale)  # type: ignore[attr-defined]
        print(f"Pruned {len(stale)} stale {doc_type} documents from Chroma.")
    return len(stale)


def _prune_after_index(
    *,
    prune_stale: bool,
    unreadable: list[str],
    segment_ids: set[str],
    youtube_segment_ids: set[str],
) -> None:
    """Run the opt-in prune, refusing when the id sets can't be trusted."""
    if not prune_stale:
        return
    if unreadable:
        print(
            f"Skipping prune: {len(unreadable)} artifact(s) could not be read, so the "
            "collected ids are an incomplete picture of the corpus and pruning would "
            "delete live rows. Fix or remove those files and re-run."
        )
        return
    if segment_ids:
        prune_stale_segments(segment_ids, "transcript_segment")
    if youtube_segment_ids:
        prune_stale_segments(youtube_segment_ids, "youtube_transcript_segment")


def delete_youtube_from_chroma() -> None:
    """Delete only YouTube-derived documents from Chroma (keeps audio + Substack).

    TASK-5 must not call this during ingest. Upsert youtube_* types only.
    """
    youtube_types = set(YOUTUBE_CHROMA_TYPES)
    _delete_by_type(lambda t: t in youtube_types, f"types: {YOUTUBE_CHROMA_TYPES}")


def update_chroma_db(
    reset: bool = False,
    *,
    include_audio: bool = True,
    include_articles: bool = True,
    include_youtube: bool = False,
    confirm_reset: str | None = None,
    prune_stale: bool = False,
) -> None:
    """Update the ChromaDB vector store.

    Args:
        reset: If True, delete the persisted Chroma directory before indexing.
        include_audio: If True, collect/index audio-derived documents.
        include_articles: If True, collect/index Substack-derived documents.
        include_youtube: If True, collect/index YouTube-derived documents (not folded into audio).
        prune_stale: If True, delete segment rows that the artifacts on disk no
            longer produce. Needed after a document-id scheme change, and off by
            default because indexing a partial set of artifacts would otherwise
            delete the segments for every episode not present locally.
    """
    ensure_data_dirs()

    # Pipeline-owned indexers (kept out of DB adapter files)
    from src.pipeline.audio.index_chroma import collect_audio_documents
    from src.pipeline.substack.index_chroma import collect_substack_documents
    from src.pipeline.youtube.index_chroma import collect_youtube_documents

    # Reset semantics: delete the entire ChromaDB directory (local dev).
    # This is a destructive, global operation (audio + substack + youtube). Keep it deliberately hard to run.
    if reset and CHROMA_DIR.exists():
        if os.getenv(DESTRUCTIVE_OPS_ENV, "").lower() != "true":
            raise RuntimeError(
                "Refusing to reset Chroma: destructive ops are disabled.\n"
                f"To enable, set {DESTRUCTIVE_OPS_ENV}=true and pass "
                f"confirm_reset='{RESET_CHROMA_CONFIRM_TOKEN}'."
            )
        if confirm_reset != RESET_CHROMA_CONFIRM_TOKEN:
            raise RuntimeError(
                "Refusing to reset Chroma: missing/invalid confirm token.\n"
                f"Pass confirm_reset='{RESET_CHROMA_CONFIRM_TOKEN}'."
            )
        print(f"Reset flag detected. Deleting existing ChromaDB at {CHROMA_DIR}...")
        shutil.rmtree(CHROMA_DIR)
        print("ChromaDB directory deleted.")

    print(f"Initializing ChromaDB in {CHROMA_DIR}...")
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    vector_store = Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
        persist_directory=str(CHROMA_DIR),
    )

    all_documents = []
    audio_count = 0
    article_count = 0
    youtube_count = 0

    # Ids for the full segment corpus, used to prune rows left behind by an older
    # id scheme once the new ones are safely indexed.
    segment_ids_on_disk: set[str] = set()
    youtube_segment_ids_on_disk: set[str] = set()
    # Artifacts the collectors could not parse. Their ids are missing from the sets
    # above, so pruning against an incomplete set would delete live rows.
    unreadable: list[str] = []

    if include_audio:
        print("Collecting audio documents...")
        audio_docs = collect_audio_documents(unreadable)
        audio_count = len(audio_docs)
        all_documents.extend(audio_docs)
        segment_ids_on_disk = {
            str(d.metadata["_chroma_id"])
            for d in audio_docs
            if d.metadata.get("type") == "transcript_segment" and d.metadata.get("_chroma_id")
        }
        print(f"  Found {audio_count} audio documents")

    if include_articles:
        print("Collecting Substack documents...")
        article_docs = collect_substack_documents()
        article_count = len(article_docs)
        all_documents.extend(article_docs)
        # Helpful mental model: Substack indexes ~2 Chroma docs per article.
        article_ids = {d.metadata.get("doc_id") for d in article_docs if isinstance(d.metadata, dict)}
        article_ids.discard(None)
        approx_articles = len(article_ids)
        if approx_articles:
            print(f"  Found {article_count} article documents (~{approx_articles} articles)")
        else:
            print(f"  Found {article_count} article documents")

    if include_youtube:
        print("Collecting YouTube documents...")
        youtube_docs = collect_youtube_documents(unreadable)
        youtube_count = len(youtube_docs)
        all_documents.extend(youtube_docs)
        youtube_segment_ids_on_disk = {
            str(d.metadata["_chroma_id"])
            for d in youtube_docs
            if d.metadata.get("type") == "youtube_transcript_segment"
            and d.metadata.get("_chroma_id")
        }
        yt_ids = {
            d.metadata.get("video_id")
            for d in youtube_docs
            if isinstance(d.metadata, dict) and d.metadata.get("video_id")
        }
        print(f"  Found {youtube_count} YouTube documents (~{len(yt_ids)} videos)")

    if unreadable:
        print(f"  WARNING: {len(unreadable)} artifact(s) could not be read and were skipped:")
        for item in unreadable[:10]:
            print(f"    - {item}")
        if len(unreadable) > 10:
            print(f"    ... and {len(unreadable) - 10} more")

    if not all_documents:
        print("No documents found to index.")
        return

    # DE-DUPLICATE the list of collected documents by `_chroma_id` before processing.
    # This prevents crashes if the filesystem contains multiple artifacts for the same ID.
    unique_docs_by_id = {}
    dupe_count = 0
    for doc in all_documents:
        _id = doc.metadata.get("_chroma_id")
        if _id in unique_docs_by_id:
            dupe_count += 1
        unique_docs_by_id[_id] = doc
    
    if dupe_count > 0:
        print(f"  (Sanity Check) Filtered out {dupe_count} duplicate documents from the collection list.")
    
    all_documents = list(unique_docs_by_id.values())
    print(f"\nPreparing {len(all_documents)} unique documents for ChromaDB (audio: {audio_count}, articles: {article_count}, youtube: {youtube_count})...")

    # Extract IDs from metadata for upserts/deduplication.
    # Indexers should provide `_chroma_id`. If any are missing, fail loudly so we don't
    # accidentally create duplicates.
    document_ids_raw = [doc.metadata.get("_chroma_id") for doc in all_documents]
    if any(i is None for i in document_ids_raw):
        missing = sum(1 for i in document_ids_raw if i is None)
        raise ValueError(
            f"Chroma indexing requires `_chroma_id` on every Document metadata. Missing={missing}."
        )
    document_ids: list[str] = [cast(str, i) for i in document_ids_raw]

    # Extract a stable content hash so we can skip re-embedding unchanged docs.
    # Indexers should set `chroma_content_hash`; if missing, we’ll still index, but we won’t be able to skip.
    content_hashes = [doc.metadata.get("chroma_content_hash") for doc in all_documents]

    # Remove `_chroma_id` before persisting metadata (internal plumbing).
    for doc in all_documents:
        doc.metadata.pop("_chroma_id", None)

    # Skip unchanged docs by comparing stored `chroma_content_hash` in Chroma metadata.
    # This prevents re-embedding costs on repeat runs.
    to_add_docs = []
    to_add_ids = []

    batch_size = 100
    total_batches = (len(all_documents) + batch_size - 1) // batch_size
    skipped_unchanged = 0
    missing_hash = 0
    existing_missing_hash = 0
    existing_not_found = 0

    for i in range(0, len(all_documents), batch_size):
        batch_docs = all_documents[i : i + batch_size]
        batch_ids = document_ids[i : i + batch_size]
        batch_hashes = content_hashes[i : i + batch_size]

        # Fetch existing metadatas for these IDs (if any).
        existing_hash_by_id: dict[str, str | None] = {}
        try:
            existing = vector_store._collection.get(ids=batch_ids, include=["metadatas"])  # type: ignore[attr-defined,arg-type]
            for _id, meta in zip(existing.get("ids") or [], existing.get("metadatas") or []):
                if isinstance(meta, dict):
                    existing_hash_by_id[str(_id)] = cast(str | None, meta.get("chroma_content_hash"))
        except Exception:
            existing_hash_by_id = {}

        for doc, _id, h in zip(batch_docs, batch_ids, batch_hashes):
            if not h:
                missing_hash += 1
                to_add_docs.append(doc)
                to_add_ids.append(_id)
                continue
            id_key = str(_id)
            if id_key not in existing_hash_by_id:
                existing_not_found += 1
                to_add_docs.append(doc)
                to_add_ids.append(_id)
                continue
            prev_h = existing_hash_by_id.get(id_key)
            if prev_h is None:
                existing_missing_hash += 1
                to_add_docs.append(doc)
                to_add_ids.append(_id)
                continue
            if prev_h == h:
                skipped_unchanged += 1
                continue
            to_add_docs.append(doc)
            to_add_ids.append(_id)

        print(f"  Prepared batch {i // batch_size + 1}/{total_batches}")

    print(
        f"\nIndexing {len(to_add_docs)} documents into ChromaDB "
        f"(skipped unchanged: {skipped_unchanged}, "
        f"existing missing hash: {existing_missing_hash}, "
        f"ids not found: {existing_not_found}, "
        f"missing hash: {missing_hash})..."
    )

    if not to_add_docs:
        print("No new/updated documents to index.")
        _prune_after_index(
            prune_stale=prune_stale,
            unreadable=unreadable,
            segment_ids=segment_ids_on_disk,
            youtube_segment_ids=youtube_segment_ids_on_disk,
        )
        return

    total_batches = (len(to_add_docs) + batch_size - 1) // batch_size
    for i in range(0, len(to_add_docs), batch_size):
        batch = to_add_docs[i : i + batch_size]
        batch_ids = to_add_ids[i : i + batch_size]
        vector_store.add_documents(documents=batch, ids=batch_ids)  # type: ignore[arg-type]
        print(f"  Indexed batch {i // batch_size + 1}/{total_batches}")

    # Prune only after the replacements are in, so a failed run can't leave a gap.
    _prune_after_index(
        prune_stale=prune_stale,
        unreadable=unreadable,
        segment_ids=segment_ids_on_disk,
        youtube_segment_ids=youtube_segment_ids_on_disk,
    )

    print("Success! Embeddings generated.")


