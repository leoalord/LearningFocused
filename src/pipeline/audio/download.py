import os
import time
import re
import json
import feedparser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def get_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def sanitize_filename(filename: str) -> str:
    r"""
    Sanitize the filename by removing or replacing invalid characters.
    Removes /, ?, :, |, <, >, *, ", \ and checks for reserved names.
    """

    clean_name = re.sub(r'[\\/*?:"<>|]', "", filename)
    clean_name = "".join(ch for ch in clean_name if ord(ch) >= 32)
    return clean_name.strip()


def extract_episode_metadata(entry) -> dict:
    """
    Extract relevant metadata from an RSS feed entry.
    """
    metadata = {
        "title": entry.get("title"),
        "summary": entry.get("summary"),
        "published": entry.get("published"),
        "links": entry.get("links", []),
        "id": entry.get("id"),
    }

    image = entry.get("image")
    if isinstance(image, dict):
        metadata["image"] = image.get("href")
    else:
        metadata["image"] = image

    if "tags" in entry:
        metadata["tags"] = [t.get("term") for t in entry.tags]

    if "content" in entry:
        metadata["content"] = [c.get("value") for c in entry.content]

    optional_fields = [
        "itunes_episode",
        "itunes_season",
        "itunes_duration",
        "itunes_explicit",
    ]

    for field in optional_fields:
        if field in entry:
            metadata[field] = entry[field]

    return metadata


def download_podcasts(
    rss_feed_url: str,
    output_dir: str,
    metadata_dir: str = "metadata_output",
    limit: int | None = None,
    transcripts_dir: str | None = None,
) -> dict[str, int]:
    """
    Download podcasts from an RSS feed.

    Args:
        rss_feed_url: The URL of the RSS feed.
        output_dir: The directory to save downloaded episodes.
        metadata_dir: The directory to save episode metadata.
        limit: The maximum number of episodes to download. If None, downloads all.
        transcripts_dir: If set, skip the MP3 when a matching transcript already
            exists. Cloud Run jobs hydrate transcripts without the 6GB audio
            dump; this avoids re-downloading every episode.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created directory: {output_dir}")

    if not os.path.exists(metadata_dir):
        os.makedirs(metadata_dir)
        print(f"Created directory: {metadata_dir}")

    print(f"Parsing RSS feed: {rss_feed_url}")
    feed = feedparser.parse(rss_feed_url)

    if feed.bozo:
        print("Warning: There might be an issue with the RSS feed formatting, but continuing...")

    print(f"Found {len(feed.entries)} episodes.")

    session = get_session()
    downloaded = 0
    skipped_existing = 0
    failed = 0

    # `limit` is a cap on NEW downloads, not how many RSS entries to scan.
    # After a long gap that matters: the feed is newest-first, but skipping
    # existing files is cheap, so we walk the whole feed until the cap is hit.
    if limit is not None:
        print(f"Scanning full feed; will download at most {limit} new episode(s).")
    else:
        print("Processing all episodes (skipping files that already exist)...")

    for entry in feed.entries:
        if limit is not None and downloaded >= limit:
            print(f"Reached new-download limit ({limit}).")
            break
        try:
            title = entry.title
            clean_title = sanitize_filename(title)
            filename = f"{clean_title}.mp3"
            filepath = os.path.join(output_dir, filename)

            metadata = extract_episode_metadata(entry)
            metadata_filename = f"{clean_title}.json"
            metadata_filepath = os.path.join(metadata_dir, metadata_filename)

            with open(metadata_filepath, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)

            if os.path.exists(filepath):
                skipped_existing += 1
                continue

            if transcripts_dir:
                transcript_path = os.path.join(transcripts_dir, f"{clean_title}.json")
                if os.path.exists(transcript_path):
                    skipped_existing += 1
                    continue

            audio_url = None
            if hasattr(entry, "links"):
                for link in entry.links:
                    if link.type == "audio/mpeg":
                        audio_url = link.href
                        break

            if not audio_url and hasattr(entry, "enclosures"):
                for enclosure in entry.enclosures:
                    if enclosure.type == "audio/mpeg":
                        audio_url = enclosure.href
                        break

            if not audio_url:
                print(f"No audio link found for: {title}")
                failed += 1
                continue

            print(f"Downloading: {filename}")
            response = session.get(audio_url, stream=True, timeout=30)
            response.raise_for_status()

            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            downloaded += 1
            time.sleep(1)

        except Exception as e:
            failed += 1
            print(f"Error downloading '{entry.get('title', 'Unknown')}': {e}")
            continue

    print(
        "Download process completed. "
        f"new={downloaded} skipped_existing={skipped_existing} failed={failed}"
    )
    return {"new": downloaded, "skipped_existing": skipped_existing, "failed": failed}


def main() -> None:
    from src.config import RSS_FEED_URL, DOWNLOADS_DIR, METADATA_DIR, TRANSCRIPTS_DIR

    download_podcasts(
        RSS_FEED_URL,
        str(DOWNLOADS_DIR),
        metadata_dir=str(METADATA_DIR),
        limit=15,
        transcripts_dir=str(TRANSCRIPTS_DIR),
    )


if __name__ == "__main__":
    main()


