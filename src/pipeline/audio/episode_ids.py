"""Episode identifiers derived from podcast filenames.

Filenames use two conventions: `S2E335 Alpha Anywhere...` and `S E18 Micro-School...`.
Splitting on the first space collapses every `S E###` episode to the literal `"S"`,
which both mislabels metadata and — because the Chroma document id embeds the
episode id — lets segments from different episodes overwrite each other.

Episode numbers are not unique on their own: the feed reuses `S E2` and `S2E215`
across different recordings, so document ids pair the episode id with a hash of
the episode title.
"""

from __future__ import annotations

import hashlib
import re

UNKNOWN_EPISODE_ID = "Unknown ID"

# `S2E335`, `S E18`, `SE7` — season digits and the separating space are both optional.
_EPISODE_TOKEN = re.compile(r"^\s*(S\d*\s?E\d+)", re.IGNORECASE)


def parse_episode_id(name: str) -> str:
    """Extract the episode token from a filename or title.

    Returns `UNKNOWN_EPISODE_ID` for artifacts with no episode number, such as the
    `Intro to Academics...` Gifted Minds trailers.
    """
    if not name:
        return UNKNOWN_EPISODE_ID
    match = _EPISODE_TOKEN.match(name)
    if not match:
        return UNKNOWN_EPISODE_ID
    return match.group(1).strip()


def parse_season_episode(name: str) -> tuple[int, int] | None:
    """Season and episode numbers from a filename, or None if it carries neither.

    `S E76` omits the season digit; those episodes predate the `S2E###` numbering
    and sort as season 1.
    """
    if not name:
        return None
    match = _EPISODE_TOKEN.match(name)
    if not match:
        return None
    token = match.group(1)
    parts = re.match(r"^S(\d*)\s?E(\d+)$", token, re.IGNORECASE)
    if not parts:
        return None
    season = int(parts.group(1)) if parts.group(1) else 1
    return season, int(parts.group(2))


def episode_key(episode_id: str, title: str) -> str:
    """Collision-free key for one episode, for use inside Chroma document ids."""
    digest = hashlib.sha256((title or "").encode("utf-8")).hexdigest()[:8]
    return f"{episode_id}_{digest}"
