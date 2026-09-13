# syntax=docker/dockerfile:1
# FastMCP 4 Streamable HTTP (default entrypoint) and daily ingest job (command override).
# Hydrates chroma_db/ from GCS at MCP start. Does not re-embed.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MCP_HOST=0.0.0.0 \
    HYDRATE_CHROMA=1 \
    GCS_CHROMA_BUCKET=inferpoker-learningfocused \
    GCP_BACKUP_PROJECT=inferpoker \
    CHROMA_DIR=/data/chroma_db \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data/chroma_db

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY scripts ./scripts
RUN chmod +x /app/scripts/mcp_entrypoint.sh /app/scripts/job_entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8080

ENTRYPOINT ["/app/scripts/mcp_entrypoint.sh"]
