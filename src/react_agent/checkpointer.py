"""Durable SQLite checkpointer for the react agent.

Production wiring uses SqliteSaver on a gitignored path so the same
`thread_id` survives process restart. Tests may still inject MemorySaver.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.sqlite import SqliteSaver

CHECKPOINT_PATH_ENV = "REACT_AGENT_CHECKPOINT_PATH"
DEFAULT_CHECKPOINT_RELATIVE = Path(".checkpoints") / "react_agent.sqlite"

_savers: dict[str, SqliteSaver] = {}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_checkpoint_path(path: str | Path | None = None) -> Path:
    """Resolve the SQLite checkpoint file path.

    Precedence: explicit `path` → `REACT_AGENT_CHECKPOINT_PATH` →
    `<repo>/.checkpoints/react_agent.sqlite`.
    """
    if path is not None:
        return Path(path).expanduser().resolve()
    env = os.environ.get(CHECKPOINT_PATH_ENV, "").strip()
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (Path.cwd() / candidate).resolve()
    return (_repo_root() / DEFAULT_CHECKPOINT_RELATIVE).resolve()


class DurableSqliteSaver(SqliteSaver):
    """SqliteSaver with async methods delegated to the sync implementation.

    Upstream SqliteSaver raises on aget/aput, which breaks LangGraph `astream`.
    """

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        def _collect() -> list[CheckpointTuple]:
            return list(self.list(config, filter=filter, before=before, limit=limit))

        for item in await asyncio.to_thread(_collect):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


def get_sqlite_checkpointer(
    path: str | Path | None = None,
    *,
    cache: bool = True,
) -> DurableSqliteSaver:
    """Return a SqliteSaver for `path`, creating parent dirs and tables.

    Production callers should omit `path` so the gitignored default is used.
    Pass `cache=False` to open a fresh connection (cross-process / two-connection tests).
    """
    resolved = str(resolve_checkpoint_path(path))
    if cache and resolved in _savers:
        return _savers[resolved]  # type: ignore[return-value]

    Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved, check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    saver = DurableSqliteSaver(conn)
    saver.setup()
    if cache:
        _savers[resolved] = saver
    return saver
