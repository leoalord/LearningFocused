"""Audio source runner.

This is the canonical orchestration entrypoint for the audio/podcast source.

It composes:
- file production (download/transcribe/identify/segment): `src.pipeline.audio.process_all`
- summary generation: `src.pipeline.audio.generate_summaries`
- optional indexing:
  - Chroma embeddings: `src.database.chroma_manager.update_chroma_db`
  - Neo4j graph extraction: `src.database.neo4j_manager.update_knowledge_graph`

Design:
- Keep `process_all` thin and incremental/backfill-friendly.
- Keep indexing optional and explicit via flags.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from dotenv import load_dotenv

from src.config import COMBINED_DIR, METADATA_DIR, TRANSCRIPTS_DIR, ensure_data_dirs
from src.pipeline.index_chroma import update_chroma_db
from src.pipeline.index_neo4j import update_knowledge_graph, delete_audio_from_neo4j
from src.pipeline.audio import process_all
from src.pipeline.audio.generate_summaries import process_combined_summaries


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the audio pipeline (processing + optional indexing).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument(
        "--skip-processing",
        action="store_true",
        help="Skip download/transcribe/identify/segment (does not skip summaries/indexing).",
    )
    p.add_argument(
        "--skip-summaries",
        action="store_true",
        help="Skip combined summary generation.",
    )
    p.add_argument(
        "--run-summaries-without-new-transcripts",
        action="store_true",
        help=(
            "In daily mode, run summary grouping even if this run produced 0 new transcripts. "
            "By default we skip summaries in that case to avoid spending LLM calls on an unchanged corpus."
        ),
    )
    p.add_argument(
        "--skip-chroma",
        action="store_true",
        help="Skip updating the Chroma vector DB.",
    )
    p.add_argument(
        "--run-chroma-without-new-transcripts",
        action="store_true",
        help=(
            "In daily mode, run Chroma indexing even if this run produced 0 new transcripts. "
            "By default we skip Chroma indexing in that case to avoid a full corpus scan."
        ),
    )
    p.add_argument(
        "--delete-audio-from-chroma",
        action="store_true",
        help="Delete all audio documents from Chroma before indexing (safer than reset).",
    )
    p.add_argument(
        "--skip-neo4j",
        action="store_true",
        help="Skip updating the Neo4j knowledge graph.",
    )
    p.add_argument(
        "--run-neo4j-without-new-transcripts",
        action="store_true",
        help=(
            "In daily mode, run Neo4j indexing even if this run produced 0 new transcripts. "
            "By default we skip Neo4j indexing in that case to avoid a full corpus scan."
        ),
    )
    p.add_argument(
        "--delete-audio-from-neo4j",
        action="store_true",
        help="Delete all audio documents from Neo4j before indexing.",
    )
    p.add_argument(
        "--neo4j-schema-profile",
        choices=["core", "extended"],
        default=None,
        help=(
            "Neo4j relationship schema profile. "
            "If omitted, uses NEO4J_SCHEMA_PROFILE env var or defaults to 'core'."
        ),
    )
    p.add_argument(
        "--neo4j-audio-limit",
        type=int,
        default=None,
        help=(
            "Limit the number of audio Documents (segment-level) sent through graph extraction in this run. "
            "Useful for incremental testing and avoiding large LLM workloads."
        ),
    )
    # Destructive option (hidden; requires env+confirm token inside update_chroma_db anyway)
    p.add_argument("--reset-chroma", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--confirm-reset-chroma", default=None, help=argparse.SUPPRESS)

    p.add_argument(
        "process_all_args",
        nargs=argparse.REMAINDER,
        help=(
            "Arguments forwarded to `src.pipeline.audio.process_all`. "
            "Example: -- --mode backfill --skip-download --skip-transcribe --segment-limit 25"
        ),
    )

    return p


def main(argv: Sequence[str] | None = None) -> None:
    load_dotenv()
    ensure_data_dirs()
    args = build_parser().parse_args(argv)

    # argparse.REMAINDER includes the leading "--" (if present). Drop it.
    forwarded = list(args.process_all_args)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]

    process_result = None
    if not args.skip_processing:
        process_result = process_all.main(forwarded)

    no_new_daily = (
        process_result is not None
        and process_result.mode == "daily"
        and process_result.new_transcripts_created == 0
    )

    if not args.skip_summaries:
        if (
            process_result is not None
            and process_result.mode == "daily"
            and process_result.new_transcripts_created == 0
            and not args.run_summaries_without_new_transcripts
        ):
            msg = (
                "No new transcripts were created in this daily run; skipping combined summaries. "
                "Re-run with --run-summaries-without-new-transcripts to force summary grouping."
            )
            if process_result.transcribe_failed:
                msg += f" (Transcribe failures: {process_result.transcribe_failed})"
            print(msg)
        else:
            process_combined_summaries(TRANSCRIPTS_DIR, METADATA_DIR, COMBINED_DIR)

    if not args.skip_chroma:
        if no_new_daily and not args.run_chroma_without_new_transcripts:
            msg = (
                "No new transcripts were created in this daily run; skipping Chroma indexing. "
                "Re-run with --run-chroma-without-new-transcripts to force a full scan."
            )
            if process_result and process_result.transcribe_failed:
                msg += f" (Transcribe failures: {process_result.transcribe_failed})"
            print(msg)
        else:
            from src.pipeline.index_chroma import update_chroma_db, delete_audio_from_chroma

            if args.delete_audio_from_chroma:
                print("\n=== CLEANUP CHROMA (AUDIO) ===")
                delete_audio_from_chroma()

            # Audio runs should default to indexing only audio docs to keep runs scoped and cheap.
            update_chroma_db(
                reset=args.reset_chroma,
                include_audio=True,
                include_articles=False,
                include_youtube=False,
                confirm_reset=args.confirm_reset_chroma,
            )

    if not args.skip_neo4j:
        if no_new_daily and not args.run_neo4j_without_new_transcripts:
            msg = (
                "No new transcripts were created in this daily run; skipping Neo4j indexing. "
                "Re-run with --run-neo4j-without-new-transcripts to force a full scan."
            )
            if process_result and process_result.transcribe_failed:
                msg += f" (Transcribe failures: {process_result.transcribe_failed})"
            print(msg)
        else:
            if args.delete_audio_from_neo4j:
                delete_audio_from_neo4j()
            # Audio runs: keep graph indexing scoped to audio by default.
            update_knowledge_graph(
                include_audio=True,
                include_articles=False,
                audio_limit=args.neo4j_audio_limit,
                schema_profile=args.neo4j_schema_profile,
            )


if __name__ == "__main__":
    main()
