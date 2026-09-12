"""FastAPI app for the local demo chat UI.

Start: `uv run python -m src.ui`
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from src.ui.service import ChatUIError, arun_chat, health_payload

load_dotenv()

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="LearningFocused Chat",
    description="Local demo UI for tool-backed Q&A with SourceChunk citation cards.",
    version="0.1.0",
)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1)
    thread_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[dict]
    thread_id: str
    tool_backed: bool
    tools_used: list[str]


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/health")
async def health() -> dict:
    return health_payload()


@app.post("/api/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    try:
        payload = await arun_chat(body.question, thread_id=body.thread_id)
    except ChatUIError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.message, "code": exc.code},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Chat request failed")
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Chat failed. Check the server logs for details.",
                "code": "chat_failed",
            },
        ) from exc
    return ChatResponse(**payload)
