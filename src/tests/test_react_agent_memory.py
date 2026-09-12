"""Durable SQLite checkpointer tests for the react agent (no live LLM)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from src.react_agent.checkpointer import (
    DurableSqliteSaver,
    get_sqlite_checkpointer,
    resolve_checkpoint_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class ProbeState(TypedDict):
    notes: list[str]


def compile_probe_graph(checkpointer: SqliteSaver):
    """Minimal graph so checkpoints can be written without Gemini."""

    def add_note(state: ProbeState) -> dict:
        return {"notes": list(state.get("notes") or []) + ["remembered"]}

    builder = StateGraph(ProbeState)
    builder.add_node("add_note", add_note)
    builder.add_edge(START, "add_note")
    builder.add_edge("add_note", END)
    return builder.compile(checkpointer=checkpointer)


def write_probe_checkpoint(db_path: str, thread_id: str) -> None:
    saver = get_sqlite_checkpointer(db_path, cache=False)
    app = compile_probe_graph(saver)
    app.invoke(
        {"notes": ["hello-from-proc-1"]},
        {"configurable": {"thread_id": thread_id}},
    )


def read_probe_checkpoint(db_path: str, thread_id: str) -> list[str]:
    saver = get_sqlite_checkpointer(db_path, cache=False)
    app = compile_probe_graph(saver)
    snap = app.get_state({"configurable": {"thread_id": thread_id}})
    values = snap.values or {}
    return list(values.get("notes") or [])


class TestSqliteCheckpointerPath(unittest.TestCase):
    def test_default_path_is_gitignored_repo_checkpoints(self) -> None:
        path = resolve_checkpoint_path()
        self.assertEqual(path.name, "react_agent.sqlite")
        self.assertEqual(path.parent.name, ".checkpoints")
        gitignore = (REPO_ROOT / ".gitignore").read_text()
        self.assertIn(".checkpoints/", gitignore)

    def test_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "custom.sqlite"
            old = os.environ.get("REACT_AGENT_CHECKPOINT_PATH")
            os.environ["REACT_AGENT_CHECKPOINT_PATH"] = str(override)
            try:
                self.assertEqual(resolve_checkpoint_path(), override.resolve())
            finally:
                if old is None:
                    os.environ.pop("REACT_AGENT_CHECKPOINT_PATH", None)
                else:
                    os.environ["REACT_AGENT_CHECKPOINT_PATH"] = old


class TestProductionWiring(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        os.environ["OPENAI_API_KEY"] = "test-openai"
        os.environ["GOOGLE_API_KEY"] = "test-google"
        os.environ["ANTHROPIC_API_KEY"] = "test-anthropic"
        os.environ["FIREWORKS_API_KEY"] = "test-fireworks"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_default_checkpointer_is_sqlite_not_memory(self) -> None:
        from src.react_agent.graph import get_react_agent, memory, react_agent

        self.assertIsInstance(memory, SqliteSaver)
        self.assertIsInstance(memory, DurableSqliteSaver)
        self.assertNotIsInstance(memory, MemorySaver)
        self.assertIsInstance(react_agent.checkpointer, SqliteSaver)
        self.assertNotIsInstance(react_agent.checkpointer, MemorySaver)

        injected = MemorySaver()
        agent = get_react_agent(checkpointer=injected)
        self.assertIs(agent.checkpointer, injected)


class TestCrossProcessMemory(unittest.TestCase):
    def test_second_connection_reads_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "react_agent.sqlite")
            thread_id = "thread-same-process-two-conns"
            write_probe_checkpoint(db, thread_id)
            notes = read_probe_checkpoint(db, thread_id)
            self.assertIn("hello-from-proc-1", notes)
            self.assertIn("remembered", notes)

    def test_checkpoint_survives_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "react_agent.sqlite")
            thread_id = "thread-cross-process"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(REPO_ROOT)
            write = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from src.tests.test_react_agent_memory import write_probe_checkpoint; "
                        f"write_probe_checkpoint({db!r}, {thread_id!r})"
                    ),
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                write.returncode,
                0,
                msg=f"writer failed: stdout={write.stdout!r} stderr={write.stderr!r}",
            )
            read = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from src.tests.test_react_agent_memory import read_probe_checkpoint; "
                        f"print(read_probe_checkpoint({db!r}, {thread_id!r}))"
                    ),
                ],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                read.returncode,
                0,
                msg=f"reader failed: stdout={read.stdout!r} stderr={read.stderr!r}",
            )
            self.assertIn("hello-from-proc-1", read.stdout)
            self.assertIn("remembered", read.stdout)


class TestThreadIdFlags(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        os.environ.setdefault("GOOGLE_API_KEY", "test-google")
        os.environ.setdefault("OPENAI_API_KEY", "test-openai")
        os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic")
        os.environ.setdefault("FIREWORKS_API_KEY", "test-fireworks")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_parser_accepts_thread_id(self) -> None:
        from src.react_agent.chat_cli import build_parser

        args = build_parser().parse_args(["--thread-id", "abc-123"])
        self.assertEqual(args.thread_id, "abc-123")

    def test_cli_overrides_env(self) -> None:
        from src.react_agent.chat_cli import resolve_thread_id

        os.environ["REACT_AGENT_THREAD_ID"] = "from-env"
        self.assertEqual(resolve_thread_id("from-cli"), "from-cli")
        self.assertEqual(resolve_thread_id(None), "from-env")

    def test_generates_uuid_when_unset(self) -> None:
        from src.react_agent.chat_cli import resolve_thread_id

        os.environ.pop("REACT_AGENT_THREAD_ID", None)
        generated = resolve_thread_id(None)
        self.assertGreaterEqual(len(generated), 8)
        self.assertNotEqual(generated, resolve_thread_id(None))


class TestStreamEnvelope(unittest.TestCase):
    def test_default_tuple_values_payload_is_unpacked(self) -> None:
        from langchain.messages import AIMessage, HumanMessage

        from src.react_agent.chat_cli import _first_unseen_index, _values_messages

        history = [
            HumanMessage(content="old question"),
            AIMessage(content="old answer"),
            HumanMessage(content="new question"),
            AIMessage(content="new answer"),
        ]
        self.assertIsNone(_values_messages(("updates", {"model": {"messages": history}})))
        values = _values_messages(("values", {"messages": history}))
        self.assertIsNotNone(values)
        assert values is not None
        start = _first_unseen_index(values)
        printed = [m.content for m in values[start:]]
        self.assertEqual(printed, ["new answer"])
        self.assertNotIn("old answer", printed)

    def test_bare_values_dict_still_works(self) -> None:
        from langchain.messages import AIMessage, HumanMessage

        from src.react_agent.chat_cli import _values_messages

        messages = [HumanMessage(content="hi"), AIMessage(content="hello")]
        self.assertEqual(
            [m.content for m in (_values_messages({"messages": messages}) or [])],
            ["hi", "hello"],
        )
