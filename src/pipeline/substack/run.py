"""Substack source runner.

This is the canonical orchestration entrypoint for ingesting Substack posts and producing:
- persisted article artifacts (metadata/html/text)
- doc-level structured summaries + tags
- optional indexing into Postgres chunks + pgvector embeddings (so the MCP tools can retrieve them)
"""

from __future__ import annotations

import argparse
from typing import Sequence

from dotenv import load_dotenv

from src.config import ensure_data_dirs
from src.pipeline.index_neo4j import update_knowledge_graph
from src.pipeline.substack import process_all

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the Substack pipeline (processing + optional indexing).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--skip-processing", action="store_true", help="Skip ingest + summaries.")
    p.add_argument("--skip-index", "--skip-chroma", dest="skip_index", action="store_true",
                   help="Skip updating the Postgres chunks and embeddings.")
    p.add_argument("--skip-neo4j", action="store_true", help="Skip updating the Neo4j knowledge graph.")
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
    if not args.skip_index:
        from src.pipeline.index_pg import require_postgres

        require_postgres()

    forwarded = list(args.process_all_args)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]

    if not args.skip_processing:
        process_all.main(forwarded)

    if not args.skip_index:
        from src.pipeline.index_pg import update_pg_index

        # Substack runs should default to indexing only Substack docs to keep runs scoped and cheap.
        update_pg_index(include_audio=False, include_articles=True, include_youtube=False)

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
