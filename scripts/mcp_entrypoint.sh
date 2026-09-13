#!/bin/sh
# Cloud Run entrypoint: hydrate chroma_db/ from GCS onto instance disk, then serve /mcp.
# Does not --reset-chroma, re-embed, or touch Neo4j.
set -eu

if [ "${HYDRATE_CHROMA:-1}" = "1" ]; then
  python -m src.mcp_server.hydrate
fi

exec python -m src.mcp_server
