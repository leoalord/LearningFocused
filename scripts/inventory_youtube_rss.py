#!/usr/bin/env python3
"""Metadata-only inventory of Future of Education YouTube vs podcast RSS.

Never downloads audio/video. Uses yt-dlp --flat-playlist and feedparser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import feedparser

RSS_FEED_URL = "https://rss.art19.com/future-of-education"
FOE_HANDLE = "@future_of_education"
FOE_CHANNEL_ID = "UCKHZkY1J1NyKypLn80Pj19A"
FOE_CHANNEL_URL = "https://www.youtube.com/@future_of_education"
FOE_UPLOADS = f"https://www.youtube.com/playlist?list=UU{FOE_CHANNEL_ID[2:]}"

PLAYLISTS = {
    "shorts": "https://www.youtube.com/playlist?list=PL46RDqX0ZGh-pTUWEX9mPEuuLBuujHt-B",
    "gifted_minds_episodes": "https://www.youtube.com/playlist?list=PL46RDqX0ZGh_ncPKxFTxOSPF2cqpd7mvG",
    "appearances": "https://www.youtube.com/playlist?list=PL46RDqX0ZGh8bEmizco2UYHlKfWUb2-NZ",
    "about_gifted_minds": "https://www.youtube.com/playlist?list=PL46RDqX0ZGh9ez9WB9-FnbU1ax6ry9OMU",
    "students_of_the_future": "https://www.youtube.com/playlist?list=PL46RDqX0ZGh_z4dui0Gb8wQBsFVN1I1HQ",
    "future_of_education_clips": "https://www.youtube.com/playlist?list=PL46RDqX0ZGh-EmbaMa0mIKNGds916H_K7",
}

EPISODE_PREFIX = re.compile(
    r"^(?:s\s*\d*e\s*\d+|ep(?:isode)?\s*#?\d+)[:.\-\s]+",
    re.IGNORECASE,
)
INTRO_PREFIX = re.compile(r"^intro to\s+", re.IGNORECASE)
STOP = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "and",
    "for",
    "in",
    "on",
    "with",
    "your",
    "you",
    "is",
    "from",
    "how",
    "why",
    "we",
    "our",
    "this",
    "that",
    "are",
}


def yt_dlp_flat(url: str) -> list[dict]:
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--skip-download",
        "-j",
        "--no-warnings",
        url,
    ]
    try:
        timeout = max(1, int(os.environ.get("LF_YTDLP_TIMEOUT_SECONDS", "300") or "300"))
    except ValueError:
        timeout = 300
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"yt-dlp timed out after {timeout}s for {url}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed for {url}:\n{proc.stderr[-2000:]}")
    items = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


def normalize(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "")
    t = t.lower()
    t = EPISODE_PREFIX.sub("", t)
    t = t.replace("&", " and ")
    t = re.sub(r"[\"“”‘’]", "", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tokens(title: str) -> set[str]:
    return {w for w in normalize(title).split() if w not in STOP and len(w) > 2}


def after_colon(title: str) -> str:
    if ":" in title:
        return title.split(":", 1)[1]
    return title


def parse_hms(value) -> int | None:
    if value is None or value == "" or value == "NA":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    parts = text.split(":")
    if not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def score(rss_title: str, yt_title: str) -> float:
    na, nb = normalize(rss_title), normalize(yt_title)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    na2, nb2 = normalize(after_colon(rss_title)), normalize(yt_title)
    if na2 and na2 == nb2:
        return 0.99
    # Gifted Minds intros: "Intro to Academics" vs "Gifted Minds: Academics"
    na_intro = normalize(INTRO_PREFIX.sub("", rss_title))
    nb_gm = normalize(re.sub(r"^gifted minds\s+", "", yt_title, flags=re.I))
    if na_intro and na_intro == nb_gm:
        return 0.97
    ratio = SequenceMatcher(None, na, nb).ratio()
    ratio2 = SequenceMatcher(None, na2, nb).ratio() if na2 else 0.0
    ta, tb = tokens(rss_title), tokens(yt_title)
    jacc = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    contain = 0.0
    if na2 and len(na2) > 20 and (na2 in nb or nb in na2):
        contain = 0.92
    return max(ratio, ratio2, jacc, contain)


def combined_score(rss: dict, yt: dict) -> float:
    title_s = score(rss["title"], yt["title"])
    rss_dur = parse_hms(rss.get("itunes_duration"))
    yt_dur = parse_hms(yt.get("duration"))
    dur_bonus = 0.0
    if rss_dur is not None and yt_dur is not None:
        delta = abs(rss_dur - yt_dur)
        if delta <= 3:
            dur_bonus = 0.45
        elif delta <= 8:
            dur_bonus = 0.35
        elif delta <= 20:
            dur_bonus = 0.15
    return title_s + dur_bonus


def load_rss(url: str) -> list[dict]:
    feed = feedparser.parse(url)
    entries = []
    for e in feed.entries:
        entries.append(
            {
                "title": e.get("title") or "",
                "published": e.get("published") or "",
                "id": e.get("id") or "",
                "itunes_episode": e.get("itunes_episode"),
                "itunes_duration": e.get("itunes_duration"),
            }
        )
    return {
        "feed_title": feed.feed.get("title"),
        "feed_link": feed.feed.get("link"),
        "entries": entries,
    }


def compact_item(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "title": item.get("title") or "",
        "duration": item.get("duration"),
        "url": item.get("url")
        or item.get("webpage_url")
        or (f"https://www.youtube.com/watch?v={item['id']}" if item.get("id") else None),
        "channel": item.get("channel") or item.get("uploader"),
        "channel_id": item.get("channel_id") or item.get("uploader_id"),
    }


def match_all(
    rss_entries: list[dict], yt_items: list[dict], threshold: float
) -> tuple[list[dict], list[dict], list[dict]]:
    """Greedy one-to-one matching using title similarity plus duration proximity."""
    candidates: list[tuple[float, int, int]] = []
    for ri, rss in enumerate(rss_entries):
        for yi, yt in enumerate(yt_items):
            s = combined_score(rss, yt)
            title_s = score(rss["title"], yt["title"])
            rss_dur = parse_hms(rss.get("itunes_duration"))
            yt_dur = parse_hms(yt.get("duration"))
            close_dur = (
                rss_dur is not None
                and yt_dur is not None
                and abs(rss_dur - yt_dur) <= 8
            )
            if s >= threshold or (close_dur and title_s >= 0.22):
                candidates.append((s, ri, yi))
    candidates.sort(reverse=True)
    used_rss: set[int] = set()
    used_yt: set[int] = set()
    pairs = []
    for s, ri, yi in candidates:
        if ri in used_rss or yi in used_yt:
            continue
        rss = rss_entries[ri]
        yt = yt_items[yi]
        used_rss.add(ri)
        used_yt.add(yi)
        pairs.append(
            {
                "rss_title": rss["title"],
                "rss_published": rss["published"],
                "yt_title": yt["title"],
                "yt_id": yt["id"],
                "yt_url": yt["url"],
                "yt_duration": yt["duration"],
                "rss_duration": rss.get("itunes_duration"),
                "score": round(s, 3),
                "title_score": round(score(rss["title"], yt["title"]), 3),
            }
        )
    unmatched_yt = [yt for i, yt in enumerate(yt_items) if i not in used_yt]
    unmatched_rss = [rss for i, rss in enumerate(rss_entries) if i not in used_rss]
    return pairs, unmatched_yt, unmatched_rss


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--threshold", type=float, default=0.55)
    args = parser.parse_args()

    print("Fetching RSS (metadata only)...", file=sys.stderr)
    rss = load_rss(RSS_FEED_URL)
    print(f"RSS episodes: {len(rss['entries'])}", file=sys.stderr)

    print("Fetching YouTube uploads (flat playlist, no download)...", file=sys.stderr)
    uploads = [compact_item(x) for x in yt_dlp_flat(FOE_UPLOADS)]
    print(f"Uploads: {len(uploads)}", file=sys.stderr)

    print("Fetching videos tab...", file=sys.stderr)
    videos_tab = [compact_item(x) for x in yt_dlp_flat(f"{FOE_CHANNEL_URL}/videos")]
    print("Fetching shorts tab...", file=sys.stderr)
    shorts_tab = [compact_item(x) for x in yt_dlp_flat(f"{FOE_CHANNEL_URL}/shorts")]

    playlist_items: dict[str, list[dict]] = {}
    for name, url in PLAYLISTS.items():
        print(f"Fetching playlist {name}...", file=sys.stderr)
        playlist_items[name] = [compact_item(x) for x in yt_dlp_flat(url)]

    upload_ids = {x["id"] for x in uploads}
    video_ids = {x["id"] for x in videos_tab}
    shorts_ids = {x["id"] for x in shorts_tab}

    # Long-form candidates: videos tab, plus any upload not in shorts tab.
    longform = [x for x in uploads if x["id"] in video_ids or x["id"] not in shorts_ids]
    shorts = [x for x in uploads if x["id"] in shorts_ids]

    pairs, unmatched_yt_long, unmatched_rss = match_all(
        rss["entries"], longform, args.threshold
    )

    # Also report shorts that happen to match RSS (should be rare).
    short_pairs, unmatched_shorts, _ = match_all(rss["entries"], shorts, 0.75)

    matched_ids = {p["yt_id"] for p in pairs}
    unique_long = [x for x in unmatched_yt_long if x["id"] not in matched_ids]

    def tag_playlist(items: list[dict]) -> list[dict]:
        tagged = []
        for item in items:
            row = dict(item)
            row["rss_duplicate"] = item["id"] in matched_ids
            row["on_foe_channel"] = item["id"] in upload_ids
            tagged.append(row)
        return tagged

    gifted_tagged = tag_playlist(
        playlist_items["gifted_minds_episodes"] + playlist_items["about_gifted_minds"]
    )
    appearance_tagged = tag_playlist(playlist_items["appearances"])
    gifted_unique = [x for x in gifted_tagged if not x["rss_duplicate"]]
    appearance_unique = [x for x in appearance_tagged if not x["rss_duplicate"]]

    payload = {
        "rss_feed_url": RSS_FEED_URL,
        "rss_feed_title": rss["feed_title"],
        "rss_feed_link": rss["feed_link"],
        "rss_episode_count": len(rss["entries"]),
        "channel": {
            "handle": FOE_HANDLE,
            "channel_id": FOE_CHANNEL_ID,
            "url": FOE_CHANNEL_URL,
            "canonical": f"https://www.youtube.com/channel/{FOE_CHANNEL_ID}",
            "uploads_playlist": FOE_UPLOADS,
        },
        "counts": {
            "rss_episodes": len(rss["entries"]),
            "yt_uploads": len(uploads),
            "yt_videos_tab": len(videos_tab),
            "yt_shorts_tab": len(shorts_tab),
            "matched_duplicates": len(pairs),
            "unmatched_rss": len(unmatched_rss),
            "unique_longform": len(unique_long),
            "shorts_tab": len(shorts),
            "shorts_playlist": len(playlist_items["shorts"]),
            "gifted_minds_episodes": len(playlist_items["gifted_minds_episodes"]),
            "about_gifted_minds": len(playlist_items["about_gifted_minds"]),
            "appearances": len(playlist_items["appearances"]),
            "gifted_minds_unique": len(gifted_unique),
            "appearances_unique": len(appearance_unique),
            "short_pairs_high_conf": len(short_pairs),
        },
        "duplicate_pairs": pairs,
        "unique_longform": unique_long,
        "unmatched_rss_titles": [e["title"] for e in unmatched_rss],
        "playlists": {k: v for k, v in playlist_items.items()},
        "gifted_minds_tagged": gifted_tagged,
        "appearances_tagged": appearance_tagged,
        "threshold": args.threshold,
        "upload_ids": sorted(upload_ids),
    }

    if args.json_out:
        args.json_out.write_text(json.dumps(payload, indent=2))
        print(f"Wrote {args.json_out}", file=sys.stderr)

    print(json.dumps({"counts": payload["counts"], "example_pairs": pairs[:15]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
