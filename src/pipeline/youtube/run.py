"""YouTube pipeline stub (TASK-4).

Prints the planned ingest contract and refuses downloads, full-channel ingest,
and Chroma writes. TASK-5 implements skip/download/index against
`docs/youtube_pipeline.md`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

PIPELINE_PLAN = """
YouTube pipeline (TASK-4 stub — no network, no downloads, no Chroma)

Channel in scope: @future_of_education (UCKHZkY1J1NyKypLn80Pj19A)
Out of scope this pass: @thealphaschool, full UU-playlist / full-channel ingest

Chroma type (retrieval role):
  youtube_transcript_segment   -> query_segments
  youtube_summary_overview     -> query_summaries

source_type (asset class):
  youtube_video | youtube_short

Idempotency: _chroma_id includes YouTube video_id
  youtube_transcript_segment_{video_id}_{start_or_chunk}
  youtube_summary_overview_{video_id}

Skip (do not ingest as new corpus):
  RSS title matches (strip S2E### / Ep #N) OR duration±5s same-recording
  (≥8 min WITH a title cue) OR Gifted Minds Episodes playlist OR RSS intro twins.
  Never skip on duration alone.

Ingest order (TASK-5): unique long-form -> appearances -> Gifted Minds branding -> Shorts last.

allowed_types (TASK-5, not this stub):
  src/database/chroma_manager.py  (query_segments + query_summaries defaults)
  src/deep_research_agent/tools.py  (search_knowledge_base formatter; tools do not pass allowed_types)

This stub will not: download videos, write chroma_db, run audio.run, or --reset-chroma.
""".strip()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.pipeline.youtube.run",
        description=(
            "YouTube ingest stub (TASK-4). Prints the pipeline contract from "
            "docs/youtube_pipeline.md. Refuses downloads, full-channel ingest, and Chroma writes."
        ),
        epilog=PIPELINE_PLAN,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--print-plan",
        action="store_true",
        help="Print the ingest plan and exit (default action when no refused flags are set).",
    )
    # Refused flags exist so --help documents them and so accidental TASK-5-shaped
    # invocations fail closed instead of doing work.
    p.add_argument(
        "--download",
        action="store_true",
        help="REFUSED. TASK-5 downloads unique video_ids only; this stub never downloads.",
    )
    p.add_argument(
        "--ingest",
        action="store_true",
        help="REFUSED. TASK-5 runs unique-item ingest; this stub never ingests.",
    )
    p.add_argument(
        "--channel",
        metavar="URL",
        default=None,
        help="REFUSED. Full-channel ingest is out of scope (including this stub).",
    )
    p.add_argument(
        "--reset-chroma",
        action="store_true",
        help="REFUSED. This stub never writes or resets Chroma.",
    )
    return p


def _refused_reasons(args: argparse.Namespace) -> list[str]:
    reasons: list[str] = []
    if args.download:
        reasons.append("--download is refused (no video/audio downloads in TASK-4)")
    if args.ingest:
        reasons.append("--ingest is refused (no unique-item ingest in TASK-4)")
    if args.channel:
        reasons.append("--channel is refused (no full-channel ingest)")
    if args.reset_chroma:
        reasons.append("--reset-chroma is refused (no Chroma writes)")
    return reasons


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    refused = _refused_reasons(args)
    if refused:
        print("Refusing to run ingest. TASK-4 is design-only:", file=sys.stderr)
        for reason in refused:
            print(f"  - {reason}", file=sys.stderr)
        print("See docs/youtube_pipeline.md. Use --help to print the pipeline.", file=sys.stderr)
        return 2

    print(PIPELINE_PLAN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
