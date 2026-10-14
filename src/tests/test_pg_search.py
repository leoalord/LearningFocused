"""Postgres retrieval: keyword, vector, hybrid (RRF), fetch by id, graph templates, MCP wiring."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.database.pg_search import Hit, parse_agtype, rrf_fuse
from src.tests.pg_testutil import PG_TEST_DSN, SKIP_REASON, TempDatabase


def _hit(i: str) -> Hit:
    return Hit(i, "transcript_segment", i, {}, 0.0)


class TestPure(unittest.TestCase):
    def test_rrf_rewards_agreement(self) -> None:
        fused = rrf_fuse([[_hit("a"), _hit("b"), _hit("c")], [_hit("b"), _hit("d"), _hit("a")]], 3)
        self.assertEqual([h.id for h in fused], ["b", "a", "d"])
        self.assertAlmostEqual(fused[0].score, 1 / 62 + 1 / 61)

    def test_parse_agtype(self) -> None:
        self.assertEqual(parse_agtype('"Alpha"'), "Alpha")
        self.assertEqual(parse_agtype('{"id": 1, "label": "Person", "properties": {"id": "x::vertex"}}::vertex'),
                         {"id": 1, "label": "Person", "properties": {"id": "x::vertex"}})
        self.assertEqual(parse_agtype('[{"id": 1}::vertex, {"id": 2}::edge]::path'), [{"id": 1}, {"id": 2}])
        self.assertIsNone(parse_agtype(None))


def _vec(i: int) -> list[float]:
    v = [0.0] * 1536
    v[i] = 1.0
    return v


def _seed(dsn: str) -> None:
    import numpy as np

    from src.database import pg_store
    from src.database.pg_sync import SyncStats, upsert_batch

    pg_store.migrate(dsn)
    rows = [
        ("seg-1", "Mastery learning lets students move on when ready.",
         {"type": "transcript_segment", "episode_id": "S2E7", "title": "Ep 7", "start_time": 120.0}, _vec(0)),
        ("seg-2", "Two hour learning frees the afternoon for life skills.",
         {"type": "transcript_segment", "episode_id": "S2E7", "title": "Ep 7", "start_time": 5.5}, _vec(1)),
        ("art-1", "Alpha School guides replace lecturing teachers.",
         {"type": "article_text", "doc_id": "post-1", "title": "Guides"}, _vec(2)),
        ("sum-1", "Overview of the AI tutor series.",
         {"type": "series_overview", "group_title": "AI tutors"}, _vec(3)),
    ]
    with pg_store.session(dsn) as conn:
        upsert_batch(conn, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
                     [np.asarray(r[3], dtype=np.float32) for r in rows], SyncStats())
        # name_lc as neo4j_to_age writes it. The odd nodes are what Neo4j can hold: a
        # passage whose id is a content hash, a numeric id, and a node with no name.
        conn.execute("""SELECT * FROM ag_catalog.cypher('knowledge', $$
            CREATE (p:Person {id: 'MacKenzie Price', name_lc: 'mackenzie price'})
                     -[:FOUNDED]->(o:Organization {id: 'Alpha School', name_lc: 'alpha school'}),
                   (d:Document {id: 'a1b2c3', name_lc: 'a1b2c3', text: 'Alpha School uses two hour learning',
                                title: 'Ep 7'})-[:MENTIONS]->(o),
                   (d)-[:MENTIONS]->(p),
                   (o)-[:IMPLEMENTS]->(c:Concept {id: 'Two Hour Learning', name_lc: 'two hour learning'}),
                   (:Tool {id: 42, name_lc: '42'}),
                   (:Event {title: 'no name'})
            $$) AS (x ag_catalog.agtype)""")


def _fake_embed(text: str):
    import numpy as np

    # "two hour" queries land on seg-2's vector; anything else on seg-1's.
    return np.asarray(_vec(1 if "two hour" in text.lower() else 0), dtype=np.float32)


@unittest.skipUnless(PG_TEST_DSN, SKIP_REASON)
class TestPostgresRetrieval(unittest.TestCase):
    db: TempDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = TempDatabase().__enter__()
        cls.addClassCleanup(cls.db.__exit__, None, None, None)
        _seed(cls.db.dsn)

    def test_keyword_vector_hybrid(self) -> None:
        from src.database import pg_search

        with patch.object(pg_search, "embed_query", _fake_embed):
            _, kw = pg_search.retrieve('"life skills"', mode="keyword", max_segments=5, max_summaries=0,
                                       dsn=self.db.dsn)
            summaries, vec = pg_search.retrieve("two hour day", mode="vector", max_segments=2, max_summaries=1,
                                                dsn=self.db.dsn)
            _, hyb = pg_search.retrieve("two hour learning", mode="hybrid", max_segments=3, max_summaries=0,
                                        dsn=self.db.dsn)
        self.assertEqual([d.metadata["chunk_id"] for d in kw], ["seg-2"])
        self.assertEqual(vec[0].metadata["chunk_id"], "seg-2")
        self.assertEqual([d.metadata["type"] for d in summaries], ["series_overview"])
        self.assertEqual(hyb[0].metadata["chunk_id"], "seg-2")  # first in both lists

    def test_fetch_by_episode_or_doc_id_in_transcript_order(self) -> None:
        from src.database import pg_search

        _, segs = pg_search.fetch_documents("S2E7", max_segments=5, max_summaries=3, dsn=self.db.dsn)
        self.assertEqual([d.metadata["chunk_id"] for d in segs], ["seg-2", "seg-1"])
        _, arts = pg_search.fetch_documents("post-1", max_segments=5, max_summaries=3, dsn=self.db.dsn)
        self.assertEqual([d.metadata["chunk_id"] for d in arts], ["art-1"])

    def test_graph_templates(self) -> None:
        from src.database import pg_search

        dsn = self.db.dsn
        schema = {r["name"]: r["kind"] for r in pg_search.graph_query("schema", dsn=dsn)["rows"]}
        self.assertEqual(schema["Person"], "node")
        self.assertEqual(schema["FOUNDED"], "relationship")
        found = pg_search.graph_query("find_entities", entity="alpha", dsn=dsn)["rows"]
        self.assertEqual([r["name"] for r in found], ["Alpha School"])
        # Source passages aren't entities: a hash id never matches a short query.
        self.assertEqual(pg_search.graph_query("find_entities", entity="a1", dsn=dsn)["rows"], [])
        self.assertEqual([r["name"] for r in pg_search.graph_query("find_entities", entity="42", dsn=dsn)["rows"]],
                         [42])
        self.assertEqual(pg_search.graph_query("find_entities", entity="alpha", label="Person", dsn=dsn)["rows"], [])
        near = pg_search.graph_query("neighbors", entity="ALPHA SCHOOL", dsn=dsn)["rows"]
        self.assertIn({"direction": "in", "relationship": "FOUNDED",
                       "other": {"label": "Person", "name": "MacKenzie Price", "properties": {}}}, near)
        paths = pg_search.graph_query("connections", entity="mackenzie price", other_entity="two hour learning",
                                      max_hops=3, dsn=dsn)["rows"]
        self.assertEqual([p["relationships"] for p in paths], [["FOUNDED", "IMPLEMENTS"]])  # not via the passage
        direct = pg_search.graph_query("connections", entity="MacKenzie Price", other_entity="Alpha School",
                                       max_hops=3, dsn=dsn)["rows"]
        self.assertEqual(direct[0]["relationships"], ["FOUNDED"])  # shortest first
        mentions = pg_search.graph_query("mentions", entity="Alpha School", dsn=dsn)["rows"]
        self.assertEqual(mentions[0]["properties"]["text"], "Alpha School uses two hour learning")

    def test_graph_rejects_bad_input(self) -> None:
        from src.database import pg_search

        with self.assertRaises(ValueError):
            pg_search.graph_query("find_entities", entity="x", label="Person) DETACH DELETE (n", dsn=self.db.dsn)
        with self.assertRaises(ValueError):
            pg_search.graph_query("raw", entity="x", dsn=self.db.dsn)
        with self.assertRaises(ValueError):
            pg_search.graph_query("connections", entity="x", dsn=self.db.dsn)
        # Values are bound parameters, never query text.
        rows = pg_search.graph_query("find_entities", entity="' }) MATCH (n) DETACH DELETE n //", dsn=self.db.dsn)
        self.assertEqual(rows["rows"], [])
        self.assertEqual(len(pg_search.graph_query("find_entities", entity="a", dsn=self.db.dsn)["rows"]), 3)

    def test_reader_role_can_search_but_never_write(self) -> None:
        import psycopg

        from src.database import pg_search, pg_store
        from src.tests.pg_testutil import with_credentials

        pg_store.migrate(self.db.dsn, app_role="lf_app_t", app_password="app-pw",
                         reader_role="lf_reader_t", reader_password="reader-pw")
        reader = with_credentials(self.db.dsn, "lf_reader_t", "reader-pw")
        self.assertEqual(len(pg_search.graph_query("neighbors", entity="alpha school", dsn=reader)["rows"]), 3)
        _, segs = pg_search.fetch_documents("post-1", max_segments=5, max_summaries=0, dsn=reader)
        self.assertEqual(len(segs), 1)
        conn = psycopg.connect(reader, autocommit=True)
        try:
            conn.execute("SET default_transaction_read_only = off")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM chunks")
        finally:
            conn.close()

    def test_connection_slots_are_capped(self) -> None:
        from src.database import pg_search

        with patch.object(pg_search, "_SLOTS", __import__("threading").BoundedSemaphore(1)), \
                patch.object(pg_search, "SLOT_WAIT_SECONDS", 0.05):
            with pg_search.read_only(self.db.dsn):
                with self.assertRaises(pg_search.StoreBusy):
                    with pg_search.read_only(self.db.dsn):
                        pass
            with pg_search.read_only(self.db.dsn):  # the slot came back
                pass

    def test_connections_are_read_only(self) -> None:
        import psycopg

        from src.database import pg_search

        with pg_search.read_only(self.db.dsn) as conn:
            with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("DELETE FROM chunks")

    def test_mcp_tools_on_postgres_backend(self) -> None:
        import asyncio

        from fastmcp import Client, FastMCP

        from src.database import pg_search
        from src.mcp_server.server import register_postgres_tools

        server = FastMCP("test")
        register_postgres_tools(server)

        async def go():
            async with Client(server) as client:
                names = sorted(t.name for t in await client.list_tools())
                kw = await client.call_tool("keyword_search", {"query": "lecturing", "max_summaries": 1})
                graph = await client.call_tool("graph_query", {"template": "neighbors", "entity": "Alpha School"})
                bad = await client.call_tool("graph_query", {"template": "find_entities"})
            return names, kw, graph, bad

        env = {"KB_BACKEND": "postgres", "PG_DSN": self.db.dsn}
        down = {"KB_BACKEND": "postgres", "PG_DSN": "postgresql://nobody@127.0.0.1:1/x"}
        with patch.dict(os.environ, env), patch.object(pg_search, "embed_query", _fake_embed):
            names, kw, graph, bad = asyncio.run(go())

        async def unreachable():
            async with Client(server) as client:
                return await client.call_tool("keyword_search", {"query": "x"}, raise_on_error=False)

        with patch.dict(os.environ, down):
            failed = asyncio.run(unreachable())
        self.assertTrue(failed.is_error)
        text = failed.content[0].text
        self.assertIn("unavailable", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertEqual(names, ["graph_query", "hybrid_search", "keyword_search", "vector_search"])
        self.assertEqual([s.metadata["chunk_id"] for s in kw.data.segments], ["art-1"])
        self.assertEqual(len(graph.structured_content["rows"]), 3)
        self.assertIn("needs `entity`", bad.structured_content["error"])


class TestBackendSwitch(unittest.TestCase):
    def test_backend_validation(self) -> None:
        from src.mcp_server import retrieval

        with patch.dict(os.environ, {"KB_BACKEND": "neo4j"}):
            with self.assertRaises(ValueError):
                retrieval.backend()
        with patch.dict(os.environ, {"KB_BACKEND": "chroma"}):
            with self.assertRaises(ValueError):
                retrieval.retrieve_documents("q", mode="keyword")

    def test_postgres_backend_routes_search_and_fetch(self) -> None:
        from src.mcp_server import retrieval

        with patch.dict(os.environ, {"KB_BACKEND": "postgres", "KB_SEARCH_MODE": "hybrid"}), \
                patch("src.database.pg_search.retrieve", return_value=([], [])) as retrieve, \
                patch("src.database.pg_search.fetch_documents", return_value=([], [])) as fetch:
            retrieval.search("alpha", max_segments=99)
            retrieval.fetch("S2 E7")
        self.assertEqual(retrieve.call_args.kwargs["mode"], "hybrid")
        self.assertLessEqual(retrieve.call_args.kwargs["max_segments"], 8)
        self.assertEqual(fetch.call_args.args[0], "S2 E7")

    def test_daily_job_does_not_roll_a_postgres_mcp(self) -> None:
        from unittest.mock import MagicMock

        from src.pipeline import daily_ingest

        self.assertEqual(daily_ingest.mcp_backend({"env": []}), "chroma")
        service = {"template": {"containers": [{"env": [{"name": "KB_BACKEND", "value": "postgres"}]}],
                                "scaling": {"minInstanceCount": 0}}}
        session = MagicMock()
        session.get.return_value = MagicMock(ok=True, json=lambda: service)
        with patch("google.auth.default", return_value=(MagicMock(spec=[]), "p")), \
                patch("google.auth.transport.requests.AuthorizedSession", return_value=session):
            result = daily_ingest.roll_mcp_service(project="p", region="r", service="s", snapshot_at="t")
        self.assertTrue(result["skipped"])  # min instances 0 is fine: nothing to roll
        session.patch.assert_not_called()

    def test_postgres_mcp_without_a_fresh_copy_fails_the_run(self) -> None:
        from src.feeds.registry import SourceConfig
        from src.pipeline import daily_ingest

        skipped = {"service": "s", "skipped": True, "reason": "KB_BACKEND=postgres reads live data"}
        registry = [SourceConfig(key="art19", kind="podcast_rss", name="a")]
        with patch.object(daily_ingest, "roll_mcp_service", return_value=skipped), \
                patch("src.feeds.state.is_enabled", return_value=False):
            ledger = daily_ingest.run(hydrate=False, sync=False, roll=True, registry=registry,
                                      source_runners={"art19": lambda: {}})
        self.assertFalse(ledger["mcp_roll"]["ok"])
        self.assertIn("no PG_DSN", ledger["mcp_roll"]["reason"])
        self.assertFalse(ledger["ok"])


if __name__ == "__main__":
    unittest.main()
