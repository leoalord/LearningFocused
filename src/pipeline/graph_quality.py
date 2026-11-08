"""Schema filtering with drop accounting for graph extraction.

LangChain's `LLMGraphTransformer(strict_mode=True)` silently discards nodes and
relationships whose types fall outside the allowlist. That hides how much the
extractor actually proposed. We run the transformer with `strict_mode=False` and
apply the same filter here so every drop is counted with a reason.

What this filter can and cannot tell you:
- It counts claims rejected for using a type outside the vocabulary.
- It cannot catch a claim that uses an allowed but wrong type (a person tagged
  `Organization`). That needs the source-grounded audit in `evals/graph_audit`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass
class ExtractionStats:
    raw_nodes: int = 0
    kept_nodes: int = 0
    raw_rels: int = 0
    kept_rels: int = 0
    # Off-vocabulary node types the model proposed, e.g. {"Place": 3}.
    dropped_node_types: Counter[str] = field(default_factory=Counter)
    # Off-vocabulary relationship types the model proposed.
    dropped_rel_types: Counter[str] = field(default_factory=Counter)
    # Relationships whose own type was fine but an endpoint had an off-vocabulary type.
    rels_dropped_for_endpoint: int = 0

    @property
    def dropped_nodes(self) -> int:
        return self.raw_nodes - self.kept_nodes

    @property
    def dropped_rels(self) -> int:
        return self.raw_rels - self.kept_rels

    def add(self, other: "ExtractionStats") -> None:
        self.raw_nodes += other.raw_nodes
        self.kept_nodes += other.kept_nodes
        self.raw_rels += other.raw_rels
        self.kept_rels += other.kept_rels
        self.dropped_node_types.update(other.dropped_node_types)
        self.dropped_rel_types.update(other.dropped_rel_types)
        self.rels_dropped_for_endpoint += other.rels_dropped_for_endpoint

    def summary(self, top: int = 5) -> str:
        def _top(c: Counter[str]) -> str:
            return ", ".join(f"{k}x{v}" for k, v in c.most_common(top)) or "none"

        return (
            f"nodes {self.raw_nodes}->{self.kept_nodes} "
            f"(off-vocab types: {_top(self.dropped_node_types)}); "
            f"rels {self.raw_rels}->{self.kept_rels} "
            f"(off-vocab rel types: {_top(self.dropped_rel_types)}; "
            f"endpoint dropped: {self.rels_dropped_for_endpoint})"
        )

    def to_record(self) -> dict[str, Any]:
        """Flat, Neo4j-storable representation (scalars and string lists only)."""
        return {
            "raw_nodes": self.raw_nodes,
            "kept_nodes": self.kept_nodes,
            "raw_rels": self.raw_rels,
            "kept_rels": self.kept_rels,
            "rels_dropped_for_endpoint": self.rels_dropped_for_endpoint,
            "dropped_node_types": [f"{k}:{v}" for k, v in sorted(self.dropped_node_types.items())],
            "dropped_rel_types": [f"{k}:{v}" for k, v in sorted(self.dropped_rel_types.items())],
        }


def filter_to_schema(
    nodes: Sequence[Any],
    relationships: Sequence[Any],
    *,
    allowed_nodes: Sequence[str],
    allowed_relationships: Sequence[str],
) -> tuple[list[Any], list[Any], ExtractionStats]:
    """Apply LangChain strict-mode filtering, recording why each item was dropped.

    Mirrors `LLMGraphTransformer` strict mode for string relationship allowlists:
    case-insensitive node type match; relationships need both endpoint types and
    their own type to be allowed.
    """
    node_ok = {t.lower() for t in allowed_nodes}
    rel_ok = {t.lower() for t in allowed_relationships}
    stats = ExtractionStats(raw_nodes=len(nodes), raw_rels=len(relationships))

    kept_nodes: list[Any] = []
    for n in nodes:
        if not node_ok or n.type.lower() in node_ok:
            kept_nodes.append(n)
        else:
            stats.dropped_node_types[n.type] += 1

    kept_rels: list[Any] = []
    for r in relationships:
        endpoints_ok = not node_ok or (
            r.source.type.lower() in node_ok and r.target.type.lower() in node_ok
        )
        type_ok = not rel_ok or r.type.lower() in rel_ok
        if endpoints_ok and type_ok:
            kept_rels.append(r)
        elif not type_ok:
            stats.dropped_rel_types[r.type] += 1
        else:
            stats.rels_dropped_for_endpoint += 1

    stats.kept_nodes = len(kept_nodes)
    stats.kept_rels = len(kept_rels)
    return kept_nodes, kept_rels, stats
