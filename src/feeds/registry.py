"""Load the feed registry and decide which sources are due today.

`sources.json` is the only place to edit sources. When Postgres is configured the daily
job mirrors it into the `sources` table (overwriting any edits there) and reads back
last-check times from there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).with_name("sources.json")
CADENCES = ("daily", "weekly", "manual")
SOCIAL_KINDS = ("instagram", "x", "linkedin", "tiktok")
KNOWN_KINDS = ("podcast_rss", "substack", "youtube_channel", *SOCIAL_KINDS)
WEEKLY_INTERVAL = timedelta(days=6, hours=12)


@dataclass(frozen=True)
class SourceConfig:
    key: str
    kind: str
    name: str
    url: str | None = None
    cadence: str = "daily"
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SourceConfig":
        src = cls(
            key=str(raw["key"]),
            kind=str(raw["kind"]),
            name=str(raw.get("name") or raw["key"]),
            url=raw.get("url"),
            cadence=str(raw.get("cadence") or "daily"),
            enabled=bool(raw.get("enabled", True)),
            config=dict(raw.get("config") or {}),
        )
        if src.cadence not in CADENCES:
            raise ValueError(f"source {src.key}: cadence {src.cadence!r} not in {CADENCES}")
        if src.kind not in KNOWN_KINDS:
            raise ValueError(f"source {src.key}: unknown kind {src.kind!r}")
        return src

    def as_row(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "name": self.name,
            "url": self.url,
            "cadence": self.cadence,
            "enabled": self.enabled,
            "config": self.config,
        }


def load_sources(path: Path | None = None) -> list[SourceConfig]:
    data = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8"))
    sources = [SourceConfig.from_dict(raw) for raw in data["sources"]]
    keys = [s.key for s in sources]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate source keys in registry: {keys}")
    return sources


def is_due(
    source: SourceConfig,
    *,
    now: datetime,
    last_checked_at: datetime | None,
    has_state: bool = False,
) -> bool:
    """Daily: always. Manual: never. Weekly: ~7 days since the last check.

    `has_state` means last-check times were read from Postgres: a weekly source with no
    check yet is then due now. Without state at all, weekly sources run on Mondays.
    """
    if not source.enabled or source.cadence == "manual":
        return False
    if source.cadence == "daily":
        return True
    if last_checked_at is None:
        return has_state or now.weekday() == 0
    return now - last_checked_at >= WEEKLY_INTERVAL


def due_sources(
    sources: list[SourceConfig],
    *,
    now: datetime,
    last_checked: dict[str, datetime] | None = None,
) -> list[SourceConfig]:
    """`last_checked=None` means no Postgres state; `{}` means state with no checks yet."""
    last = last_checked or {}
    return [
        s for s in sources
        if is_due(s, now=now, last_checked_at=last.get(s.key), has_state=last_checked is not None)
    ]
