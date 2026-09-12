"""Substack source runner.

This is the canonical orchestration entrypoint for ingesting Substack posts and producing:
- persisted article artifacts (metadata/html/text)
- doc-level structured summaries + tags
- optional indexing into Chroma (so `search_knowledge_base` can retrieve them)
"""

from __future__ import annotations

import argparse
from typing import Sequence

from dotenv import load_dotenv

from src.config import ensure_data_dirs
from src.pipeline.index_chroma import delete_substack_from_chroma, update_chroma_db
from src.pipeline.index_neo4j import update_knowledge_graph
from src.pipeline.substack import process_all

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the Substack pipeline (processing + optional indexing).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--skip-processing", action="store_true", help="Skip ingest + summaries.")
    p.add_argument("--skip-chroma", action="store_true", help="Skip updating the Chroma vector DB.")
    p.add_argument("--skip-neo4j", action="store_true", help="Skip updating the Neo4j knowledge graph.")
    # Destructive option (hidden; requires env+confirm token inside update_chroma_db anyway)
    p.add_argument("--reset-chroma", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--confirm-reset-chroma", default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--delete-substack-from-chroma",
        action="store_true",
        help="Delete ONLY Substack vectors from Chroma (keeps audio). Runs before indexing.",
    )
    p.add_argument(
        "--neo4j-schema-profile",
        choices=["core", "extended"],
        default=None,
        help="Neo4j relationship schema profile (defaults to extended).",
    )
    p.add_argument(
        "--neo4j-article-limit",
        type=int,
        default=None,
        help="Max number of Substack article Documents to transform/write to Neo4j (test runs).",
    )
    p.add_argument(
        "process_all_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to `src.pipeline.substack.process_all`. Example: -- --mode daily --ingest-limit 10",
    )
    return p


def main(argv: Sequence[str] | None = None) -> None:
    load_dotenv()
    ensure_data_dirs()
    args = build_parser().parse_args(argv)

    forwarded = list(args.process_all_args)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]

    if not args.skip_processing:
        process_all.main(forwarded)

    if not args.skip_chroma:
        if args.delete_substack_from_chroma:
            delete_substack_from_chroma()
        # Substack runs should default to indexing only Substack docs to keep runs scoped and cheap.
        update_chroma_db(
            reset=bool(args.reset_chroma),
            include_audio=False,
            include_articles=True,
            include_youtube=False,
            confirm_reset=args.confirm_reset_chroma,
        )

    if not args.skip_neo4j:
        # For Substack runs, default to indexing only articles (not audio) to keep test runs fast and scoped.
        update_knowledge_graph(
            include_audio=False,
            include_articles=True,
            audio_limit=None,
            article_limit=args.neo4j_article_limit,
            schema_profile=args.neo4j_schema_profile,
        )


if __name__ == "__main__":
    main()
