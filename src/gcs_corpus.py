"""GCS corpus snapshot helpers (hydrate + checksum upload).

Used by the public MCP hydrate path and the daily ingest job. Does not
--reset-chroma, touch Neo4j, or upload .env / pem / SA JSON.
"""

from __future__ import annotations

import base64
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Derived artifacts + chroma snapshot. Never the 6GB podcast_downloads dump.
ARTIFACT_PREFIXES = (
    "chroma_db",
    "transcripts",
    "segmented_transcripts",
    "combined_summaries",
    "substack_articles",
    "article_summaries",
    "youtube_videos",
    "youtube_summaries",
)

LEDGER_PREFIX = "ingest_ledger"
DEFAULT_BUCKET = "inferpoker-learningfocused"
DEFAULT_PROJECT = "inferpoker"
_EXCLUDE_NAMES = {".DS_Store"}
_DOWNLOAD_WORKERS = 8


def default_project() -> str:
    return os.getenv("GCP_BACKUP_PROJECT") or os.getenv("GCP_PROJECT_ID_OVERRIDE") or DEFAULT_PROJECT


def default_bucket() -> str:
    return os.getenv("GCS_CHROMA_BUCKET") or os.getenv("GCS_CORPUS_BUCKET") or DEFAULT_BUCKET


def local_dir_for_prefix(prefix: str) -> Path:
    from src.config import CHROMA_DIR, PROJECT_ROOT

    name = prefix.strip("/")
    if name == "chroma_db":
        return Path(os.getenv("CHROMA_DIR", str(CHROMA_DIR)))
    return PROJECT_ROOT / name


def _storage_client(project: str | None = None):
    from google.cloud import storage

    return storage.Client(project=project or default_project())


def _normalize_prefix(prefix: str) -> str:
    prefix = prefix.strip().lstrip("/")
    if not prefix.endswith("/"):
        prefix = prefix + "/"
    return prefix


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def download_prefix(
    *,
    bucket: str | None = None,
    prefix: str,
    dest: Path | None = None,
    project: str | None = None,
    require_files: bool = True,
) -> int:
    """Copy gs://bucket/prefix onto dest. Returns the number of files written."""
    bucket_name = bucket or default_bucket()
    project_id = project or default_project()
    prefix_n = _normalize_prefix(prefix)
    dest_dir = Path(dest) if dest is not None else local_dir_for_prefix(prefix_n)
    dest_dir.mkdir(parents=True, exist_ok=True)

    client = _storage_client(project_id)
    files = [b for b in client.list_blobs(bucket_name, prefix=prefix_n) if not b.name.endswith("/")]
    if not files:
        if require_files:
            raise FileNotFoundError(f"No objects under gs://{bucket_name}/{prefix_n}")
        print(f"No objects under gs://{bucket_name}/{prefix_n}")
        return 0

    print(f"Hydrating {len(files)} objects from gs://{bucket_name}/{prefix_n} -> {dest_dir}")

    def _one(blob) -> str:
        rel = blob.name[len(prefix_n) :]
        if not rel:
            return ""
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(target))
        return blob.name

    written = 0
    with ThreadPoolExecutor(max_workers=_DOWNLOAD_WORKERS) as pool:
        futures = [pool.submit(_one, blob) for blob in files]
        for fut in as_completed(futures):
            name = fut.result()
            if name:
                written += 1
    print(f"  hydrated {written} files into {dest_dir}")
    return written


def hydrate_artifacts(*, bucket: str | None = None, project: str | None = None) -> dict[str, int]:
    """Pull chroma + derived text artifacts. Does not pull podcast_downloads."""
    counts: dict[str, int] = {}
    for prefix in ARTIFACT_PREFIXES:
        require = prefix == "chroma_db"
        counts[prefix] = download_prefix(
            bucket=bucket,
            prefix=prefix,
            dest=local_dir_for_prefix(prefix),
            project=project,
            require_files=require,
        )
    chroma = local_dir_for_prefix("chroma_db") / "chroma.sqlite3"
    if not chroma.is_file():
        raise FileNotFoundError(f"Hydrate finished but {chroma} is missing")
    print(f"Chroma snapshot ready at {chroma} ({chroma.stat().st_size} bytes)")
    return counts


def upload_prefix(
    *,
    bucket: str | None = None,
    prefix: str,
    src: Path | None = None,
    project: str | None = None,
) -> dict[str, int]:
    """Checksum-upload local files under src to gs://bucket/prefix.

    Skips unchanged objects (MD5 match). Does not delete remote-only objects.
    """
    bucket_name = bucket or default_bucket()
    project_id = project or default_project()
    prefix_n = _normalize_prefix(prefix)
    src_dir = Path(src) if src is not None else local_dir_for_prefix(prefix_n)
    stats = {"uploaded": 0, "skipped": 0, "missing_local": 0}
    if not src_dir.is_dir():
        print(f"SKIP missing local dir: {src_dir}")
        stats["missing_local"] = 1
        return stats

    client = _storage_client(project_id)
    bucket_obj = client.bucket(bucket_name)
    existing_md5: dict[str, str | None] = {}
    for blob in client.list_blobs(bucket_name, prefix=prefix_n):
        if blob.name.endswith("/"):
            continue
        existing_md5[blob.name] = blob.md5_hash

    for path in src_dir.rglob("*"):
        if not path.is_file() or path.name in _EXCLUDE_NAMES:
            continue
        rel = path.relative_to(src_dir).as_posix()
        object_name = prefix_n + rel
        local_md5 = _md5_file(path)
        if existing_md5.get(object_name) == local_md5:
            stats["skipped"] += 1
            continue
        blob = bucket_obj.blob(object_name)
        blob.upload_from_filename(str(path))
        stats["uploaded"] += 1
        print(f"  uploaded gs://{bucket_name}/{object_name} ({path.stat().st_size} bytes)")
    print(
        f"rsync {src_dir} -> gs://{bucket_name}/{prefix_n} "
        f"uploaded={stats['uploaded']} skipped={stats['skipped']}"
    )
    return stats


def upload_artifacts(*, bucket: str | None = None, project: str | None = None) -> dict[str, dict[str, int]]:
    results: dict[str, dict[str, int]] = {}
    for prefix in ARTIFACT_PREFIXES:
        results[prefix] = upload_prefix(
            bucket=bucket,
            prefix=prefix,
            src=local_dir_for_prefix(prefix),
            project=project,
        )
    return results


def upload_bytes(
    *,
    object_name: str,
    data: bytes,
    bucket: str | None = None,
    project: str | None = None,
    content_type: str = "application/json",
) -> str:
    bucket_name = bucket or default_bucket()
    client = _storage_client(project)
    blob = client.bucket(bucket_name).blob(object_name.lstrip("/"))
    blob.upload_from_string(data, content_type=content_type)
    uri = f"gs://{bucket_name}/{blob.name}"
    print(f"  wrote {uri} ({len(data)} bytes)")
    return uri


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)
