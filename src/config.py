import os
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables (no filesystem side-effects)
load_dotenv()

# Project Root
# src/config.py -> parent is src/ -> parent is project_root
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Data Directories
DOWNLOADS_DIR = PROJECT_ROOT / "podcast_downloads"
TRANSCRIPTS_DIR = PROJECT_ROOT / "transcripts"
SEGMENTED_DIR = PROJECT_ROOT / "segmented_transcripts"
METADATA_DIR = PROJECT_ROOT / "metadata_output"
COMBINED_DIR = PROJECT_ROOT / "combined_summaries"
# Local PersistentClient path. Cloud Run hydrates the GCS snapshot here (or
# to CHROMA_DIR if that env var is set). Not Chroma Cloud.
_chroma_override = os.getenv("CHROMA_DIR")
CHROMA_DIR = (
    Path(_chroma_override).expanduser() if _chroma_override else PROJECT_ROOT / "chroma_db"
)

# Daily ingest run ledger (TASK-19). Also mirrored to gs://.../ingest_ledger/.
LEDGER_DIR = PROJECT_ROOT / "ingest_ledger"

# Substack/article artifact directories (parallel to podcast artifacts)
SUBSTACK_DIR = PROJECT_ROOT / "substack_articles"
SUBSTACK_METADATA_DIR = SUBSTACK_DIR / "metadata"
SUBSTACK_HTML_DIR = SUBSTACK_DIR / "html"
SUBSTACK_TEXT_DIR = SUBSTACK_DIR / "text"
ARTICLE_SUMMARIES_DIR = PROJECT_ROOT / "article_summaries"

# YouTube artifact directories (gitignored; see docs/youtube_pipeline.md)
YOUTUBE_DIR = PROJECT_ROOT / "youtube_videos"
YOUTUBE_METADATA_DIR = YOUTUBE_DIR / "metadata"
YOUTUBE_CAPTIONS_DIR = YOUTUBE_DIR / "captions"
YOUTUBE_AUDIO_DIR = YOUTUBE_DIR / "audio"
YOUTUBE_TRANSCRIPTS_DIR = YOUTUBE_DIR / "transcripts"
YOUTUBE_SEGMENTED_DIR = YOUTUBE_DIR / "segmented"
YOUTUBE_SUMMARIES_DIR = PROJECT_ROOT / "youtube_summaries"

DATA_DIRS = [
    DOWNLOADS_DIR,
    TRANSCRIPTS_DIR,
    SEGMENTED_DIR,
    METADATA_DIR,
    COMBINED_DIR,
    SUBSTACK_METADATA_DIR,
    SUBSTACK_HTML_DIR,
    SUBSTACK_TEXT_DIR,
    ARTICLE_SUMMARIES_DIR,
    YOUTUBE_METADATA_DIR,
    YOUTUBE_CAPTIONS_DIR,
    YOUTUBE_AUDIO_DIR,
    YOUTUBE_TRANSCRIPTS_DIR,
    YOUTUBE_SEGMENTED_DIR,
    YOUTUBE_SUMMARIES_DIR,
    LEDGER_DIR,
]


def ensure_data_dirs() -> None:
    """Create expected data directories.

    Best practice: this should be called explicitly by entrypoints (run/process_all),
    not executed as a side-effect of importing `src.config`.
    """
    for directory in DATA_DIRS:
        directory.mkdir(parents=True, exist_ok=True)

# External Config
RSS_FEED_URL = "https://rss.art19.com/future-of-education"
SUBSTACK_FEED_URL = "https://futureofeducation.substack.com/feed"

# Database Config
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

