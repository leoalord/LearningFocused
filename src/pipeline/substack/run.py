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
from src.pipeline.substack import process_all

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the Substack pipeline (processing + optional indexing).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--skip-processing", action="store_true", help="Skip ingest + summaries.")
    p.add_argument("--skip-index", "--skip-chroma", dest="skip_index", action="store_true",
                   help="Skip updating the Postgres chunks and embeddings.")
    p.add_argument("--skip-graph", "--skip-neo4j", dest="skip_graph", action="store_true",
                   help="Skip LLM graph extraction into the Postgres AGE graph (billed LLM work).")
    p.add_argument(
        "--graph-schema-profile",
        "--neo4j-schema-profile",
        dest="graph_schema_profile",
        choices=["core", "extended"],
        default=None,
        help="Relationship schema profile (defaults to extended).",
    )
    p.add_argument(
        "--graph-article-limit",
        "--neo4j-article-limit",
        dest="graph_article_limit",
        type=int,
        default=None,
        help="Max number of Substack article Documents to send through graph extraction (test runs).",
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

    if not args.skip_graph:
        from src.pipeline.index_graph import update_knowledge_graph

        # For Substack runs, default to indexing only articles (not audio) to keep test runs fast and scoped.
        update_knowledge_graph(
            include_audio=False,
            include_articles=True,
            audio_limit=None,
            article_limit=args.graph_article_limit,
            schema_profile=args.graph_schema_profile,
        )


if __name__ == "__main__":
    main()
