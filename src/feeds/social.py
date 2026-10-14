"""Social-media sources (Instagram, X, LinkedIn, TikTok): pluggable stubs.

Each source lists its accounts in sources.json and names the env var holding its
credential. Until both are filled in, a check is recorded as skipped (not failed).
Once they are, the check fails loudly until a fetcher for that platform is written,
so a half-configured source never looks healthy.

To add a platform fetcher: implement `fetch(source, token) -> list[SocialPost]` and
register it in FETCHERS. Posts are written as text artifacts the indexers pick up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from src.feeds.registry import SourceConfig


@dataclass
class SocialPost:
    platform: str
    account: str
    post_id: str
    url: str
    published_at: str
    text: str
    media_type: str = "text"


Fetcher = Callable[[SourceConfig, str], list[SocialPost]]
FETCHERS: dict[str, Fetcher] = {}


class SourceNotImplemented(RuntimeError):
    """Accounts and credentials are set but nothing can fetch this platform yet."""


def run_social(source: SourceConfig) -> dict[str, Any]:
    accounts = [str(a) for a in source.config.get("accounts") or [] if str(a).strip()]
    if not accounts:
        return {"skipped": True, "reason": "no accounts configured in sources.json"}
    env = str(source.config.get("credential_env") or "")
    token = os.getenv(env) if env else None
    if not token:
        return {"skipped": True, "reason": f"credential {env or '(unset)'} missing", "accounts": accounts}
    fetcher = FETCHERS.get(source.kind)
    if fetcher is None:
        raise SourceNotImplemented(
            f"{source.kind}: {len(accounts)} account(s) and a credential are configured, "
            "but no fetcher exists yet (see src/feeds/social.py)"
        )
    posts = fetcher(source, token)
    return {"accounts": accounts, "posts_found": len(posts)}
