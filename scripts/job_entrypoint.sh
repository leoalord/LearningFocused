#!/bin/sh
# Cloud Run Job entrypoint: isolated daily ingest. Does not start the MCP server.
# Does not --reset-chroma, re-transcribe existing files, or touch Neo4j.
set -eu
echo "learningfocused daily ingest starting $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exec python -m src.pipeline.daily_ingest "$@"
