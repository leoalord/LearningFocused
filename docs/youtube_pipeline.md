# YouTube ingest pipeline (design)

Design for TASK-4 / FEAT-2. **This task does not download videos, write Chroma, call `audio.run`, or `--reset-chroma`.** TASK-5 implements ingest against this contract.

Trust `docs/media_inventory.md` for unique-vs-duplicate counts. Channel in scope: `@future_of_education` (`UCKHZkY1J1NyKypLn80Pj19A`). **Do not ingest `@thealphaschool` in the same pass.**

## Why types matter

Agents call `search_knowledge_base` / `search_knowledge_base_structured` in `src/deep_research_agent/tools.py`. Those tools call `query_segments` / `query_summaries` in `src/database/chroma_manager.py` **without** passing `allowed_types`, so they use the defaults:

- `query_segments` → `["transcript_segment", "article_text"]`
- `query_summaries` → `["series_overview", "series_motivation", "key_takeaway", "article_summary_overview"]`

A new YouTube vector whose `metadata.type` is not in those lists is **invisible** to agents. TASK-5 must extend the defaults (and the string formatter in the tools module). This task only names the change.

Audio uses `type` like `transcript_segment`. Substack uses `type` (`article_text`, `article_summary_overview`) plus `source_type` (`substack_article`). Document IDs are deterministic (`transcript_segment_{episode_id}_…`, `article_text_{doc_id}`).

## Artifact layout on disk

Mirror Substack (`substack_articles/…`) rather than mixing into podcast dirs. All of this is **gitignored**; TASK-5 creates dirs at runtime (planned `src/config.py` constants — not added in TASK-4).

```
youtube_videos/                      # gitignored root
  metadata/{video_id}.json           # yt-dlp / inventory sidecar (title, url, duration, bucket, skip decision)
  captions/{video_id}.vtt          # official captions when present
  audio/{video_id}.m4a            # optional audio extract for transcription (not full MP4 dumps)
  transcripts/{video_id}.json      # ASR / caption-normalized transcript
  segmented/{video_id}.json       # topic segments (same shape as podcast segmented_transcripts)
youtube_summaries/{video_id}.json   # per-video overview (not series rollups)
```

Do **not** commit media. Do **not** download the 188 Shorts (or the full UU uploads playlist) in TASK-4. TASK-5 may extract audio for unique items only.

Skip-decision logs (which `video_id`s were skipped and why) belong under `youtube_videos/metadata/` or a sibling `youtube_videos/skip_log.json` — also gitignored.

## Chroma `type` and `source_type`

Same split as Substack: **`type` is the retrieval role** (what `allowed_types` filters); **`source_type` is the asset class**.

| Field | Values | Used by |
|---|---|---|
| `type` | `youtube_transcript_segment` | `query_segments` (detailed / citeable chunks) |
| `type` | `youtube_summary_overview` | `query_summaries` (one overview vector per unique video) |
| `source_type` | `youtube_video` | Unique long-form: explainers, essays, appearances, Gifted Minds branding |
| `source_type` | `youtube_short` | Shorts tab / Shorts playlist |

`youtube_video` vs `youtube_short` is **`source_type`**, not `type`. Putting format on `type` would force `query_segments` to list every format and would collide with the summary/segment split.

Do **not** reuse `transcript_segment` for YouTube. Podcast duplicates must not become a second blob under the same type; unique YT must be filterable.

### `bucket` (ingest cohort, not a Chroma type)

Inventory buckets from `docs/media_inventory.md`. Store as metadata string `bucket`:

| `bucket` | `source_type` | Notes |
|---|---|---|
| `unique_longform` | `youtube_video` | Teachers 2.0, overviews, YouTube-native essays, campus/explainer clips |
| `appearance` | `youtube_video` | Playlist `PL46RDqX0ZGh8bEmizco2UYHlKfWUb2-NZ` (6 items; 5 off-channel) |
| `gifted_minds_branding` | `youtube_video` | Three unique About Gifted Minds clips (not RSS intros, not Episodes playlist) |
| `short` | `youtube_short` | Shorts; curated 22-item playlist before the rest of the 188 |

## Metadata keys

Chroma metadata must be scalars (stringify lists). Every indexed YouTube document sets:

| Key | Required | Example / notes |
|---|---|---|
| `type` | yes | `youtube_transcript_segment` or `youtube_summary_overview` |
| `source_type` | yes | `youtube_video` or `youtube_short` |
| `video_id` | yes | YouTube id (`L69E2p56cJw`); **idempotency key** |
| `title` | yes | Watch-page title (also used by `SourceChunk.title`) |
| `url` | yes | `https://www.youtube.com/watch?v={video_id}` |
| `canonical_url` | yes | Same as `url` so `search_knowledge_base_structured` citations work without a schema change |
| `duration_seconds` | yes | Integer seconds from yt-dlp |
| `bucket` | yes | One of the four values above |
| `channel_id` | yes | Uploader channel id (off-channel appearances are not `UCKHZkY1J1NyKypLn80Pj19A`) |
| `channel_handle` | yes | e.g. `@future_of_education`, `@kgwnews` |
| `chroma_content_hash` | yes | SHA-256 of embedded `page_content` (same skip-unchanged pattern as audio/Substack) |

Optional / segment-only:

| Key | When |
|---|---|
| `topic` | Segment label |
| `start_time`, `end_time` | Seconds; citation timestamps |
| `playlist_id` | If the item was selected via a named playlist |
| `published_at` | ISO date if known |
| `is_short` | `true`/`false` string (Chroma bools are fine if the backend stores them) |
| `doc_id` | Set equal to `video_id` so `SourceChunk.doc_id` is populated |

Skip logs (not indexed): `skip_reason` ∈ `rss_title_match` | `same_recording` | `gifted_minds_episodes_playlist` | `rss_intro_twin`.

## Document id scheme (idempotency)

Stable `_chroma_id` **must include the YouTube `video_id`**. Upsert on that id; skip re-embed when `chroma_content_hash` is unchanged (`src/pipeline/index_chroma.py`).

| Role | `_chroma_id` |
|---|---|
| Transcript / caption chunk | `youtube_transcript_segment_{video_id}_{start_seconds_or_chunk}` |
| Per-video overview | `youtube_summary_overview_{video_id}` |

`start_seconds_or_chunk` is the integer start time when segments have timestamps, else a zero-padded chunk index (`000`, `001`, …). Do not hash the title into the id (titles rewrite). Re-running TASK-5 on the same `video_id` overwrites the same vectors.

## Duplicate-podcast skip rule

**One-liner:** Skip RSS title matches, duration±5s same-recording (≥8 min **with a title cue**), the Gifted Minds Episodes playlist, and RSS intro twins; never skip on duration alone; never ingest `@thealphaschool` in this pass.

Counts to plan against (`docs/media_inventory.md`): skip **~102 / 132** long-form Videos-tab uploads as podcast duplicates; unique corpus is Shorts **188** + appearances **6** + Gifted Minds branding **~4** + other unique long-form. RSS has **332** episodes; YouTube did **not** upload all of them — do not treat 332 as the duplicate-video count.

### Algorithm (TASK-5)

Inputs: Videos-tab items on `@future_of_education`, named playlists in the inventory, RSS from `RSS_FEED_URL` (`https://rss.art19.com/future-of-education`). Reuse title normalization in `scripts/inventory_youtube_rss.py` (`normalize`, `score`, `parse_hms`).

**Skip** (do not download, transcribe, or index as new corpus) if **any**:

1. **RSS title match.** After stripping `S2E###` / `S E##` / `Ep #N` prefixes and punctuation, `normalize(yt_title) == normalize(rss_title)` or `normalize(after_colon(rss_title)) == normalize(yt_title)`. Includes Gifted Minds intro mapping: RSS `Intro to {X}` ↔ YT `Gifted Minds: {X}`.
2. **Same-recording, rewritten title.** `duration_seconds >= 480` **and** `|yt_duration − rss itunes:duration| ≤ 5` **and** a **title cue** (inventory helper `score(rss, yt) >= 0.22`, or an explicit rewritten pair such as Stock Market / 9 Models / Pay to Read). Duration-only collisions are **not** duplicates (e.g. *Here's How I'm Fixing School* vs an unrelated RSS item at the same length).
3. **Gifted Minds Episodes playlist** `PL46RDqX0ZGh_ncPKxFTxOSPF2cqpd7mvG` (14 items; already in RSS). Skip the entire playlist by `video_id`.
4. **RSS intro twins** on About Gifted Minds: *Gifted Minds: Academics*, *Gifted Minds: Emotional Health*, *Gifted Minds: Life Skills*.

**Do not skip** Shorts solely because they clip an RSS episode (different format). Optionally skip a Short later if TASK-5 chooses to treat it as a clip of an already-indexed unique long-form parent; that is an extra heuristic, not required to avoid podcast duplication.

**Out of scope this feature pass:** `@thealphaschool`; full-channel / full UU-playlist ingest.

### Ingest order (TASK-5 only)

1. Unique long-form with no RSS twin.
2. Appearances (five off-channel, then on-channel Jenga clip).
3. Gifted Minds branding: *Meet Gifted Minds*, *The story behind the idea*, *What's in a name?*
4. Shorts last — curated playlist `PL46RDqX0ZGh-pTUWEX9mPEuuLBuujHt-B` (22) before the rest of the Shorts tab (188). Do not bulk-download 188 Shorts in the first TASK-5 run unless explicitly expanded.

## `allowed_types` files TASK-5 must change

Retrieval works **only** if defaults include the new `type` values. Tools do not pass `allowed_types` today.

| File | What to change | Why |
|---|---|---|
| `src/database/chroma_manager.py` | `query_segments` default list: add `youtube_transcript_segment`. `query_summaries` default list: add `youtube_summary_overview`. | **Required.** Agents never see YT docs otherwise. |
| `src/deep_research_agent/tools.py` | `search_knowledge_base` string formatter currently branches on `article_*` / `article_text` vs “Episode”. Add a YouTube branch (title + `canonical_url` / `url`, not “Episode”). Docstrings that say “podcast transcripts only” should mention YouTube. | **Required for readable hits.** `search_knowledge_base_structured` already copies `title` / `canonical_url` / `doc_id` from metadata — set those keys on YT docs and structured search works without a new `SourceChunk` field. |
| `src/react_agent/tools.py` | No type list (re-exports). Optional docstring tweak only. | Not a filter site. |
| `src/pipeline/index_chroma.py` | Add `include_youtube`, `collect_youtube_documents()`, and a `YOUTUBE_CHROMA_TYPES` delete helper analogous to audio/Substack. Do **not** fold YouTube into `include_audio`. | TASK-5 indexing. |
| `src/pipeline/youtube/index_chroma.py` | New (TASK-5): build Documents with the ids/metadata above. | Parallel to `audio/index_chroma.py` and `substack/index_chroma.py`. |
| `src/config.py` | Path constants for `youtube_videos/` and `youtube_summaries/`. | TASK-5, not this task. |
| `src/analysis/visualize_chroma.py` | Optional: display `source_type` / `video_id`. | Not required for agent retrieval. |

`src/react_agent` and `src/deep_research_agent` both import the same tools. Changing `chroma_manager.py` + the formatter in `deep_research_agent/tools.py` is the complete agent-facing `allowed_types` surface.

Do **not** change `allowed_types` in TASK-4 (no Chroma writes; nothing to retrieve yet).

## What TASK-5 will run vs what TASK-4 will not do

| | TASK-4 (this) | TASK-5 |
|---|---|---|
| Design (`docs/youtube_pipeline.md`) | yes | — |
| CLI stub `--help` / refuse ingest | optional yes | replace stub with real runner |
| Inventory (`docs/media_inventory.md`) | already Done (TASK-3) | consume it |
| Download video/audio | **no** | unique items only, ingest order above |
| Full-channel / UU playlist dump | **no** | **no** |
| 188 Shorts bulk download | **no** | not in the first pass |
| `@thealphaschool` | **no** | **no** (same pass) |
| Transcribe / segment / summarize | **no** | yes, unique items |
| Write Chroma | **no** | yes, new types + ids |
| `--reset-chroma` / wipe `chroma_db` | **no** | **no** unless a later explicit cleanup task |
| `audio.run` / podcast re-ingest | **no** | **no** |
| Extend `allowed_types` + tool formatter | document only | **yes** |
| Neo4j | **no** | out of scope unless a later task |

## Planned TASK-5 modules (not implemented here)

```
src/pipeline/youtube/
  run.py              # orchestrator (TASK-5)
  skip.py             # skip-rule matcher over inventory + RSS
  download.py         # unique video_ids only; refuse channel URLs
  transcribe.py
  segment.py
  summarize.py
  index_chroma.py
```

Entrypoint: `uv run python -m src.pipeline.youtube.run --help`
