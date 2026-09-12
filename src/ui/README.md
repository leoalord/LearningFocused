# Local chat UI

Thin FastAPI + single HTML page for demo Q&A. The server calls the react agent and renders `SourceChunk` fields from `search_knowledge_base_structured` as source cards. API keys stay in `.env` on the server; the page never receives them.

## Start

From the repo root (needs `uv`, `.env` with `GOOGLE_API_KEY`, and an indexed `chroma_db/`):

```bash
uv run python -m src.ui
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). Ask **What is Two Hour Learning?** — you should see an answer plus at least one source card (title + episode id or URL).

Optional bind overrides:

```bash
uv run python -m src.ui --port 9000
UI_HOST=127.0.0.1 UI_PORT=8765 uv run python -m src.ui
```

Chat threads use the same durable SQLite checkpointer as the react-agent CLI (`.checkpoints/react_agent.sqlite`).

## Error states

The UI shows a readable message (banner on load via `/api/health`, and on submit via `/api/chat`) when:

- **Missing LLM key** — e.g. no `GOOGLE_API_KEY` for the default `gemini-flash-latest` model
- **Missing Chroma** — `chroma_db/` is absent or the `education_knowledge_engine` collection is missing
- **Empty collection** — Chroma opens but has zero documents

The server still starts in these cases so the page can display the error instead of crashing at import.

## Layout

| Path | Role |
|------|------|
| `app.py` | FastAPI routes (`/`, `/api/health`, `/api/chat`) |
| `service.py` | Preflight + react-agent adapter + `SourceChunk` → cards |
| `static/index.html` | Question form, answer, source cards |
| `__main__.py` | `uv run python -m src.ui` |
