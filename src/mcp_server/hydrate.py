"""Copy gs://<bucket>/chroma_db/ onto local disk. Does not reset or re-embed."""

from __future__ import annotations

import sys
from pathlib import Path

from src.gcs_corpus import download_prefix, local_dir_for_prefix


def hydrate(
    *,
    bucket: str | None = None,
    prefix: str = "chroma_db/",
    dest: Path | None = None,
    project: str | None = None,
) -> Path:
    dest_dir = Path(dest) if dest is not None else local_dir_for_prefix("chroma_db")
    try:
        download_prefix(
            bucket=bucket,
            prefix=prefix,
            dest=dest_dir,
            project=project,
            require_files=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
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
