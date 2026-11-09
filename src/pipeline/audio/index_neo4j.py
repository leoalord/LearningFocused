"""Audio pipeline -> knowledge graph indexing helpers.

Pipeline-owned logic:
- how we choose/construct Documents from audio artifacts for graph extraction
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from langchain_core.documents import Document

from src.pipeline.audio.index_chroma import create_transcript_chunk_documents, create_transcript_documents


logger = logging.getLogger(__name__)


def collect_audio_graph_documents(
    segmented_dir: Path, *, limit: int | None = None, transcripts_dir: Path | None = None
) -> List[Document]:
    """Transcript chunks for graph extraction: the same chunks retrieval indexes.

    Built from raw transcripts (`transcripts_dir`, default `src.config.TRANSCRIPTS_DIR`)
    with topic labels from `segmented_dir`, so speaker names are current and no
    utterance is dropped. A segmented file with no raw transcript falls back to one
    Document per topic segment.
    """
    if transcripts_dir is None:
        from src.config import TRANSCRIPTS_DIR

        transcripts_dir = TRANSCRIPTS_DIR

    documents: List[Document] = []
    chunked: set[str] = set()

    def _add(chunk_docs: List[Document], source: Path) -> bool:
        for d in chunk_docs:
            meta = {k: v for k, v in d.metadata.items() if k not in {"_chroma_id", "chroma_content_hash"}}
            meta["source"] = str(source)
            meta["speakers"] = [s for s in str(meta.get("speakers") or "").split(", ") if s]
            documents.append(Document(page_content=d.page_content, metadata=meta))
            if limit is not None and len(documents) >= limit:
                return True
        return False

    for path in sorted(Path(transcripts_dir).glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            seg_path = Path(segmented_dir) / f"{path.stem}_segmented.json"
            seg = json.loads(seg_path.read_text(encoding="utf-8")) if seg_path.exists() else None
            chunked.add(path.stem)
            if _add(create_transcript_chunk_documents(data, seg), path):
                return documents
        except Exception as e:
            logger.exception("Error reading transcript %s: %s", path, e)

    for seg_path in sorted(Path(segmented_dir).glob("*_segmented.json")):
        if seg_path.name.removesuffix("_segmented.json") in chunked:
            continue
        try:
            data = json.loads(seg_path.read_text(encoding="utf-8"))
            if _add(create_transcript_documents(data), seg_path):
                return documents
        except Exception as e:
            logger.exception("Error reading segmented transcript %s: %s", seg_path, e)

    if not documents:
        logger.info("No transcripts found in %s or %s", transcripts_dir, segmented_dir)
    return documents
