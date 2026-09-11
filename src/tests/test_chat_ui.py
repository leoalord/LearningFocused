"""Tests for the local chat UI adapter (no live LLM)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain.messages import AIMessage, HumanMessage, ToolMessage

from src.deep_research_agent.tools import SourceChunk
from src.ui.service import (
    SOURCE_CARD_FIELDS,
    ChatUIError,
    chroma_preflight_error,
    final_answer_from_messages,
    health_payload,
    llm_api_key_error,
    parse_structured_payload,
    preflight,
    source_chunk_to_card,
    sources_from_messages,
    tools_used_from_messages,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_HTML = REPO_ROOT / "src" / "ui" / "static" / "index.html"
UI_README = REPO_ROOT / "src" / "ui" / "README.md"
ROOT_README = REPO_ROOT / "README.md"


class TestSourceChunkMapping(unittest.TestCase):
    def test_card_fields_are_source_chunk_fields(self) -> None:
        model_fields = set(SourceChunk.model_fields)
        self.assertTrue(set(SOURCE_CARD_FIELDS).issubset(model_fields))
        for required in ("title", "canonical_url", "episode_id", "snippet"):
            self.assertIn(required, SOURCE_CARD_FIELDS)

    def test_source_chunk_to_card_copies_citation_fields(self) -> None:
        chunk = SourceChunk(
            kind="transcript_segment",
            title="Two Hour Learning",
            canonical_url="https://example.com/ep",
            episode_id="ep-42",
            snippet="Mastery in two hours a day.",
        )
        card = source_chunk_to_card(chunk)
        self.assertEqual(card["title"], "Two Hour Learning")
        self.assertEqual(card["canonical_url"], "https://example.com/ep")
        self.assertEqual(card["episode_id"], "ep-42")
        self.assertEqual(card["snippet"], "Mastery in two hours a day.")
        self.assertEqual(card["kind"], "transcript_segment")

    def test_parse_structured_search_json(self) -> None:
        payload = {
            "summaries": [
                {
                    "kind": "key_takeaway",
                    "title": "Alpha School overview",
                    "canonical_url": None,
                    "episode_id": "ep-1",
                    "snippet": "Two Hour Learning is the model.",
                    "metadata": {},
                }
            ],
            "segments": [],
        }
        chunks = parse_structured_payload(json.dumps(payload))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].title, "Alpha School overview")
        self.assertEqual(chunks[0].episode_id, "ep-1")

    def test_sources_from_tool_messages(self) -> None:
        payload = {
            "summaries": [],
            "segments": [
                {
                    "kind": "transcript_segment",
                    "title": "Future of Education",
                    "canonical_url": "https://example.com/a",
                    "episode_id": "abc",
                    "snippet": "quote",
                    "metadata": {},
                }
            ],
        }
        messages = [
            HumanMessage(content="What is Two Hour Learning?"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_knowledge_base_structured",
                        "args": {"query": "Two Hour Learning"},
                        "id": "call-1",
                    }
                ],
            ),
            ToolMessage(
                content=json.dumps(payload),
                name="search_knowledge_base_structured",
                tool_call_id="call-1",
            ),
            AIMessage(content="Two Hour Learning is a school model."),
        ]
        chunks = sources_from_messages(messages)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].title, "Future of Education")
        self.assertEqual(chunks[0].canonical_url, "https://example.com/a")
        self.assertIn("search_knowledge_base_structured", tools_used_from_messages(messages))
        self.assertEqual(
            final_answer_from_messages(messages),
            "Two Hour Learning is a school model.",
        )


class TestPreflightErrors(unittest.TestCase):
    def test_missing_google_api_key_is_readable(self) -> None:
        old = os.environ.pop("GOOGLE_API_KEY", None)
        try:
            err = llm_api_key_error()
            self.assertIsNotNone(err)
            assert err is not None
            self.assertIn("GOOGLE_API_KEY", err)
            self.assertIn("browser never", err.lower())
        finally:
            if old is not None:
                os.environ["GOOGLE_API_KEY"] = old

    def test_missing_chroma_dir_is_readable(self) -> None:
        missing = Path(tempfile.gettempdir()) / "lf-missing-chroma-task9"
        with patch("src.config.CHROMA_DIR", missing):
            err = chroma_preflight_error()
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("Chroma directory not found", err)

    def test_empty_collection_is_readable(self) -> None:
        fake_col = MagicMock()
        fake_col.count.return_value = 0
        fake_client = MagicMock()
        fake_client.get_collection.return_value = fake_col
        with tempfile.TemporaryDirectory() as tmp:
            chroma_dir = Path(tmp)
            with (
                patch("src.config.CHROMA_DIR", chroma_dir),
                patch("chromadb.PersistentClient", return_value=fake_client),
            ):
                err = chroma_preflight_error()
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("empty", err.lower())

    def test_health_payload_missing_key(self) -> None:
        old = os.environ.pop("GOOGLE_API_KEY", None)
        try:
            payload = health_payload()
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "missing_api_key")
            self.assertIn("GOOGLE_API_KEY", payload["error"])
        finally:
            if old is not None:
                os.environ["GOOGLE_API_KEY"] = old

    def test_preflight_empty_question_not_here(self) -> None:
        # empty question is validated in arun_chat, not preflight
        self.assertTrue(callable(preflight))
        self.assertTrue(issubclass(ChatUIError, Exception))


class TestFrontendHasNoSecrets(unittest.TestCase):
    def test_html_has_no_api_keys(self) -> None:
        html = UI_HTML.read_text(encoding="utf-8")
        lowered = html.lower()
        for token in (
            "google_api_key",
            "openai_api_key",
            "anthropic_api_key",
            "api_key",
            "sk-proj",
            "sk-ant",
            "aiza",
        ):
            self.assertNotIn(token, lowered)
        self.assertIn("source-card", html)
        self.assertIn("/api/chat", html)

    def test_start_command_is_documented(self) -> None:
        ui_readme = UI_README.read_text(encoding="utf-8")
        root_readme = ROOT_README.read_text(encoding="utf-8")
        self.assertIn("uv run python -m src.ui", ui_readme)
        self.assertIn("uv run python -m src.ui", root_readme)


class TestHttpErrorShape(unittest.TestCase):
    def test_chat_endpoint_returns_readable_missing_key(self) -> None:
        from fastapi.testclient import TestClient

        from src.ui.app import app

        old = os.environ.pop("GOOGLE_API_KEY", None)
        try:
            client = TestClient(app)
            health = client.get("/api/health").json()
            self.assertFalse(health["ok"])
            self.assertIn("GOOGLE_API_KEY", health["error"])
            res = client.post("/api/chat", json={"question": "What is Two Hour Learning?"})
            self.assertEqual(res.status_code, 503)
            detail = res.json()["detail"]
            self.assertEqual(detail["code"], "missing_api_key")
            self.assertIn("GOOGLE_API_KEY", detail["error"])
        finally:
            if old is not None:
                os.environ["GOOGLE_API_KEY"] = old
