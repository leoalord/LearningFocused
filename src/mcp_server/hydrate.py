"""Copy gs://<bucket>/chroma_db/ onto local disk. Does not reset or re-embed."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def hydrate(
    *,
    bucket: str | None = None,
    prefix: str = "chroma_db/",
    dest: Path | None = None,
    project: str | None = None,
) -> Path:
    from google.cloud import storage

    from src.config import CHROMA_DIR

    bucket_name = bucket or os.getenv("GCS_CHROMA_BUCKET") or os.getenv(
        "GCS_CORPUS_BUCKET", "inferpoker-learningfocused"
    )
    project_id = project or os.getenv("GCP_BACKUP_PROJECT") or os.getenv(
        "GCP_PROJECT_ID_OVERRIDE", "inferpoker"
    )
    dest_dir = Path(dest) if dest is not None else Path(os.getenv("CHROMA_DIR", str(CHROMA_DIR)))
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not prefix.endswith("/"):
        prefix = prefix + "/"

    client = storage.Client(project=project_id)
    blobs = list(client.list_blobs(bucket_name, prefix=prefix))
    files = [b for b in blobs if not b.name.endswith("/")]
    if not files:
        raise SystemExit(f"No objects under gs://{bucket_name}/{prefix}")

    print(f"Hydrating {len(files)} objects from gs://{bucket_name}/{prefix} -> {dest_dir}")
    for blob in files:
        rel = blob.name[len(prefix) :]
        if not rel:
            continue
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(target))
        print(f"  {blob.name} ({blob.size} bytes)")

    sqlite = dest_dir / "chroma.sqlite3"
    if not sqlite.is_file():
        raise SystemExit(f"Hydrate finished but {sqlite} is missing")
    print(f"Chroma snapshot ready at {dest_dir} ({sqlite.stat().st_size} bytes)")
    return dest_dir


def main(argv: list[str] | None = None) -> None:
    del argv
    hydrate()


if __name__ == "__main__":
    main(sys.argv[1:])
