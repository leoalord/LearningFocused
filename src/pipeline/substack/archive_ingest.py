"""Substack archive backfill (beyond RSS limits).

Why this exists:
- Keep `download_articles.py` small and focused (RSS ingest + shared primitives).
- Keep archive crawling/pagination logic isolated (it can evolve independently).

This module is intentionally best-effort: it tries lightweight JSON endpoints first,
and falls back to scraping `/archive` pages to discover post URLs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import SUBSTACK_FEED_URL, SUBSTACK_HTML_DIR, SUBSTACK_METADATA_DIR, SUBSTACK_TEXT_DIR
from src.pipeline.substack.download_articles import (
    IngestResult,
    _load_existing_index,
    _sha256_text,
    canonicalize_url,
    doc_id_from_canonical_url,
    html_to_markdownish_text,
)


_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def _get_session() -> requests.Session:
    """Create a requests session with basic retries (same pattern as audio pipeline)."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": _BROWSER_UA,
            "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
        }
    )
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _base_url_from_feed_url(feed_url: str) -> str:
    parsed = urlparse(feed_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _try_fetch_json_list(session: requests.Session, url: str) -> list[dict[str, Any]] | None:
    """Best-effort fetch of a JSON list endpoint."""
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").lower()
        if "json" not in ctype:
            return None
        data = resp.json()
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("posts", "items", "data"):
                val = data.get(key)
                if isinstance(val, list):
                    return [d for d in val if isinstance(d, dict)]
        return None
    except Exception:
        return None


def _extract_candidate_from_item(item: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    canonical_url = item.get("canonical_url") or item.get("url")
    title = item.get("title")
    raw_html = item.get("body_html") or item.get("post_html") or item.get("content_html") or item.get("html")
    return (
        str(canonical_url) if canonical_url else None,
        str(title) if title else None,
        str(raw_html) if raw_html else None,
    )


def _slug_from_canonical_url(canonical_url: str) -> str:
    return Path(urlparse(canonical_url).path).name


def _fetch_post_html(
    session: requests.Session,
    *,
    base_url: str,
    canonical_url: str,
    slug: str | None = None,
) -> str | None:
    """Prefer the unofficial post JSON API (includes body_html); fall back to the HTML page."""
    slug = slug or _slug_from_canonical_url(canonical_url)
    if slug:
        try:
            resp = session.get(f"{base_url}/api/v1/posts/{slug}", timeout=20)
            ctype = (resp.headers.get("content-type") or "").lower()
            if resp.ok and "json" in ctype:
                data = resp.json()
                if isinstance(data, dict):
                    html = data.get("body_html") or data.get("post_html") or data.get("html")
                    if html:
                        return str(html)
        except Exception:
            pass

    try:
        resp = session.get(canonical_url, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


def _extract_urls_from_archive_html(html: str, *, base_url: str) -> list[str]:
    """Extract candidate post URLs from Substack /archive HTML (no external deps)."""
    urls: set[str] = set()
    for m in re.finditer(r'href=["\'](?P<href>[^"\']+)["\']', html, flags=re.IGNORECASE):
        href = m.group("href").strip()
        if not href:
            continue
        if href.startswith("/p/"):
            urls.add(base_url + href)
        elif "/p/" in href and "substack.com" in href:
            urls.add(href)
    return sorted(urls)


def _fetch_archive_post_urls(
    *,
    session: requests.Session,
    base_url: str,
    max_pages: int | None = None,
) -> list[str]:
    """Scrape `/archive` pages to discover post URLs."""
    urls: list[str] = []
    seen: set[str] = set()
    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            break
        archive_url = f"{base_url}/archive" if page == 1 else f"{base_url}/archive?{urlencode({'page': page})}"
        resp = session.get(archive_url, timeout=20)
        resp.raise_for_status()
        page_urls = _extract_urls_from_archive_html(resp.text, base_url=base_url)
        new = [u for u in page_urls if u not in seen]
        for u in new:
            seen.add(u)
        if not new:
            break
        urls.extend(new)
        page += 1
    return urls


def ingest_substack_archive(
    *,
    feed_url: str = SUBSTACK_FEED_URL,
    source_name: str = "future_of_education_substack",
    source_type: str = "substack_article",
    target_new: int | None = None,
    allow_updates: bool = True,
    metadata_dir=SUBSTACK_METADATA_DIR,
    html_dir=SUBSTACK_HTML_DIR,
    text_dir=SUBSTACK_TEXT_DIR,
    page_size: int = 20,
    max_pages: int | None = None,
    fetch_full_html: bool = True,
) -> list[IngestResult]:
    """Backfill older Substack posts by querying/scraping the archive (beyond RSS limits)."""
    metadata_dir.mkdir(parents=True, exist_ok=True)
    html_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)

    session = _get_session()
    base_url = _base_url_from_feed_url(feed_url)

    existing = _load_existing_index(metadata_dir)
    results: list[IngestResult] = []
    new_count = 0

    # 1) Try unofficial JSON endpoints for archive listing (best-effort).
    offset = 0
    all_items: list[dict[str, Any]] = []
    while True:
        if max_pages is not None and (offset // max(1, page_size)) >= max_pages:
            break
        archive_endpoint = f"{base_url}/api/v1/archive?sort=new&offset={offset}&limit={page_size}"
        posts_endpoint = f"{base_url}/api/v1/posts?offset={offset}&limit={page_size}"
        items = _try_fetch_json_list(session, archive_endpoint) or _try_fetch_json_list(session, posts_endpoint)
        if not items:
            break
        all_items.extend(items)
        if len(items) < page_size:
            break
        offset += page_size

    if all_items:
        for item in all_items:
            canonical_url, title_from_item, raw_html = _extract_candidate_from_item(item)
            if not canonical_url:
                continue
            canonical_url = canonicalize_url(canonical_url)
            doc_id = doc_id_from_canonical_url(canonical_url)

            if (not raw_html) and fetch_full_html:
                raw_html = _fetch_post_html(
                    session,
                    base_url=base_url,
                    canonical_url=canonical_url,
                    slug=item.get("slug") if isinstance(item.get("slug"), str) else None,
                )
                if not raw_html:
                    results.append(
                        IngestResult(
                            status="failed",
                            doc_id=doc_id,
                            canonical_url=canonical_url,
                            reason="Fetch post HTML failed",
                        )
                    )
                    continue

            if not raw_html:
                results.append(
                    IngestResult(status="failed", doc_id=doc_id, canonical_url=canonical_url, reason="Missing HTML")
                )
                continue

            text = html_to_markdownish_text(raw_html)
            content_hash = _sha256_text(text)

            prev = existing.get(canonical_url)
            if prev and prev.get("content_hash") == content_hash:
                results.append(IngestResult(status="skipped", doc_id=doc_id, canonical_url=canonical_url, reason="Unchanged"))
                continue
            if prev and not allow_updates:
                results.append(
                    IngestResult(status="skipped", doc_id=doc_id, canonical_url=canonical_url, reason="Updates disabled")
                )
                continue

            meta: dict[str, Any] = {
                "doc_id": doc_id,
                "source": source_name,
                "source_type": source_type,
                "type": source_type,
                "title": title_from_item or doc_id,
                "url": canonical_url,
                "canonical_url": canonical_url,
                "published": None,
                "published_at": None,
                "author": None,
                "guid": item.get("id") or item.get("post_id"),
                "summary_html": None,
                "content_hash": content_hash,
                "ingested_at": datetime.now(timezone.utc).isoformat(),
            }

            (metadata_dir / f"{doc_id}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            (html_dir / f"{doc_id}.html").write_text(raw_html, encoding="utf-8")
            (text_dir / f"{doc_id}.md").write_text(text + "\n", encoding="utf-8")

            status = "updated" if prev else "created"
            results.append(IngestResult(status=status, doc_id=doc_id, canonical_url=canonical_url))
            new_count += 1
            if target_new is not None and new_count >= max(0, target_new):
                break
        return results

    # 2) Fallback: scrape `/archive` pages to discover post URLs, then fetch.
    try:
        urls = _fetch_archive_post_urls(session=session, base_url=base_url, max_pages=max_pages)
    except Exception as e:
        return [IngestResult(status="failed", doc_id=None, canonical_url=None, reason=f"Archive scrape failed: {e}")]

    for url in urls:
        canonical_url = canonicalize_url(url)
        doc_id = doc_id_from_canonical_url(canonical_url)

        raw_html = _fetch_post_html(session, base_url=base_url, canonical_url=canonical_url)
        if not raw_html:
            results.append(
                IngestResult(
                    status="failed",
                    doc_id=doc_id,
                    canonical_url=canonical_url,
                    reason="Fetch post HTML failed",
                )
            )
            continue

        text = html_to_markdownish_text(raw_html)
        content_hash = _sha256_text(text)

        prev = existing.get(canonical_url)
        if prev and prev.get("content_hash") == content_hash:
            results.append(IngestResult(status="skipped", doc_id=doc_id, canonical_url=canonical_url, reason="Unchanged"))
            continue
        if prev and not allow_updates:
            results.append(IngestResult(status="skipped", doc_id=doc_id, canonical_url=canonical_url, reason="Updates disabled"))
            continue

        meta_fallback: dict[str, Any] = {
            "doc_id": doc_id,
            "source": source_name,
            "source_type": source_type,
            "type": source_type,
            "title": doc_id,
            "url": canonical_url,
            "canonical_url": canonical_url,
            "published": None,
            "published_at": None,
            "author": None,
            "guid": None,
            "summary_html": None,
            "content_hash": content_hash,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

        (metadata_dir / f"{doc_id}.json").write_text(
            json.dumps(meta_fallback, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (html_dir / f"{doc_id}.html").write_text(raw_html, encoding="utf-8")
        (text_dir / f"{doc_id}.md").write_text(text + "\n", encoding="utf-8")

        status = "updated" if prev else "created"
        results.append(IngestResult(status=status, doc_id=doc_id, canonical_url=canonical_url))
        new_count += 1
        if target_new is not None and new_count >= max(0, target_new):
            break

    return results


