# Media inventory: podcast RSS vs YouTube

Inventory for TASK-3 / FEAT-2 (YouTube ingest). **Metadata only** — no audio/video downloads, no Chroma writes.

Pulled **2026-09-10 (PDT) / 2026-09-11 (UTC)**.

## Channel URLs and how they were sourced

### Primary: Future of Education (podcast channel)

| | |
|---|---|
| Handle | [youtube.com/@future_of_education](https://www.youtube.com/@future_of_education) |
| Channel ID | `UCKHZkY1J1NyKypLn80Pj19A` |
| Canonical | [youtube.com/channel/UCKHZkY1J1NyKypLn80Pj19A](https://www.youtube.com/channel/UCKHZkY1J1NyKypLn80Pj19A) |
| Uploads playlist | `https://www.youtube.com/playlist?list=UUKHZkY1J1NyKypLn80Pj19A` |

**Sourcing**

- Repo RSS is `RSS_FEED_URL` in `src/config.py`: `https://rss.art19.com/future-of-education` (Art19 show page: [art19.com/shows/future-of-education](https://art19.com/shows/future-of-education)). Host: MacKenzie Price.
- Public show notes and 2hourlearning.com founder page point at the same YouTube handle (`@future_of_education`).
- Channel About / keywords still say “Gifted Minds” (the podcast’s former name). Channel ID matches the older Gifted Minds YouTube URL.
- Confirmed by matching current RSS episode titles to the Videos tab (see pairs below).

This is the channel that carries podcast episode videos, Shorts, Gifted Minds archives, and a media-appearances playlist.

### Related, not the podcast channel: Alpha School

| | |
|---|---|
| Handle | [youtube.com/@thealphaschool](https://www.youtube.com/@thealphaschool) |
| Videos tab | 164 videos |
| Shorts tab | 21 Shorts |

Campus / marketing channel (founding-family spots, “Future of Education is Now in …”). **Not** used for RSS duplicate matching. Out of scope for TASK-5 podcast-duplicate skip logic unless a later task wants campus-only content.

## Counts

| Source | Count | Notes |
|---|---:|---|
| RSS episodes (`https://rss.art19.com/future-of-education`) | **332** | Full Art19 feed via feedparser |
| FoE YouTube uploads (UU playlist) | **320** | Videos tab 132 + Shorts tab 188 |
| FoE Videos tab (long-form) | **132** | Episode uploads + unique long-form |
| FoE Shorts tab | **188** | Includes items not in the 22-item Shorts playlist |
| High-confidence RSS↔YouTube duplicates (title) | **80** | After stripping `S2E###` / `S E##` prefixes; plus Gifted Minds intro mapping |
| Additional likely same-recording (rewritten YT title, duration ±5s, ≥8 min) | **~22** | Same audio length as an RSS episode; titles often clickbait |
| Long-form to **skip** on ingest (title match ∪ duration-near episode) | **~102 / 132** | Would duplicate the podcast corpus |
| RSS-only (no YouTube episode video) | **~230** | Most of the 332 RSS items were never uploaded as long-form YT |
| Unique YouTube vs RSS (not a podcast episode) | Shorts **188** + appearances **6** + Gifted Minds branding **~4** + other unique long-form (~Teachers 2.0, overviews, clips) | See buckets |

**Correction vs feature why-text (~332 duplicate episode videos):** the RSS has 332 episodes, but YouTube did **not** upload all of them. Duplicate *pollution risk* is the ~100 long-form episode videos on `@future_of_education`, not 332 separate YT copies.

## Example podcast–YouTube duplicate pairs (≥10)

High-confidence title matches first; then rewritten-title pairs confirmed by duration (RSS `itunes:duration` vs yt-dlp duration, Δ≤2s).

| # | RSS (podcast) | YouTube | Evidence |
|---|---|---|---|
| 1 | S2E354: Are You Passing Limiting Beliefs to Your Kid? (6 Simple Fixes) | [Are You Passing Limiting Beliefs to Your Kid? (6 Simple Fixes)](https://www.youtube.com/watch?v=L69E2p56cJw) | Title match after prefix strip |
| 2 | S2E335: Alpha Anywhere: Elite At-Home Academics, Worldwide | [Alpha Anywhere: Elite At-Home Academics, Worldwide](https://www.youtube.com/watch?v=i9mL6DbSRaA) | Title match |
| 3 | S2E332: 10 AI Tools Your Kids Can Use Today (Alpha-Tested and Vetted) | [10 AI Tools Your Kids Can Use Today (Alpha-Tested and Vetted)](https://www.youtube.com/watch?v=D5csZVgjiyo) | Title match |
| 4 | S2E350: Philosophy is the New Computer Science (Why the AI Era Needs Deep Thinkers) | [Is Philosophy the New Computer Science? (Why the AI Era Needs Deep Thinkers)](https://www.youtube.com/watch?v=JZnhHb725sc) | Title match |
| 5 | S2E346: She Was Our Guinea Pig. Did It Work? | [She was our guinea pig...did it work?](https://www.youtube.com/watch?v=eG-bLMi9-TI) | Title match |
| 6 | S2E351: Memory Palaces, Spaced Repetition, and Forced Recall: Memorization Tricks from an 11-Year-Old History Expert | [Memorization Tricks from an 11-Year-Old History Expert](https://www.youtube.com/watch?v=Ac7xAkNrOJ4) | RSS subtitle = YT title |
| 7 | S2E344: Does AI Belong in a Montessori Classroom? | [Does AI Belong in Montessori?](https://www.youtube.com/watch?v=UwgDWU-6vXM) | Title match |
| 8 | S2E356: "Liar, Liar, Pants on Fire": A Skeptical Educator Tested Our Learning Model in India | ["You're a Liar" — This Veteran Teacher Tested Our AI Model in India](https://www.youtube.com/watch?v=mXejTKniSfY) | Same episode; YT rewritten. RSS 00:45:36, YT 2737s |
| 9 | S2E355: Steal the Stock Market Simulation We Use to Build Young Investors | [How We Teach 7-Year-Olds the Stock Market](https://www.youtube.com/watch?v=49GX6Zw5les) | RSS 00:36:42 (2202s), YT 2203s |
| 10 | S2E352: 40+ Cities, 9 Models: All of Our Schools Explained | [We Built 9 Different Alpha Schools in 50+ Cities](https://www.youtube.com/watch?v=GpdsHttKDFQ) | RSS 00:51:33 (3093s), YT 3095s |
| 11 | S2E343: The "Pay to Read" Method (+ 4 Other Unconventional Ways to Raise Bookworms) | [According to Science, You Should Pay Your Kids To Read](https://www.youtube.com/watch?v=04q7NhwWaIE) | RSS 00:10:04 (604s), YT 605s |
| 12 | S2E348: Goodbye 3 R's, Hello 4 C's: The Skills Replacing Reading, Writing, and Arithmetic | [Your Kid's Only Edge Against AI: The 4 C's](https://www.youtube.com/watch?v=q-fLvh3kj8M) | RSS 00:14:47 (887s), YT 888s |
| 13 | S E7: Ep 7 - School Is SO Boring! Using Adaptive Apps… featuring Anna Davlantes | [School Is SO Boring! … Ep #7](https://www.youtube.com/watch?v=MfPFfAmqoX8) | Gifted Minds-era episode; still in RSS |
| 14 | S E1: Ep 1 - Consult, Don’t Manage: Achieving Academic Success, with Ned Johnson… | [Consult, Don't Manage: … Ep #1](https://www.youtube.com/watch?v=vFAIv_boPqw) | Gifted Minds Ep #1 = RSS |

## Unique buckets

These are the three buckets named in the feature. None is empty.

### 1. Shorts — **not empty**

- **Shorts tab:** 188 videos at [youtube.com/@future_of_education/shorts](https://www.youtube.com/@future_of_education/shorts).
- **Curated playlist “Shorts”** (22 items): [PL46RDqX0ZGh-pTUWEX9mPEuuLBuujHt-B](https://www.youtube.com/playlist?list=PL46RDqX0ZGh-pTUWEX9mPEuuLBuujHt-B).

Example Shorts (playlist): *Learn Like an Olympian*; *Does cramming ever work?*; *Fornite in School*; *Dr Phil thinks AI could be the future of Education*; *Inside a 2-hour learning school: Asha's daily routine*; *5 ways traditional schools fail students*.

Treat as **unique video assets** vs RSS (different format; many are clips of longer videos already in RSS). Do not treat as 188 new full episodes.

### 2. Gifted Minds — **not empty** (mix of RSS duplicates and unique branding)

Playlists on the FoE channel:

| Playlist | URL | Items | vs RSS |
|---|---|---:|---|
| Gifted Minds Episodes | [PL46RDqX0ZGh_ncPKxFTxOSPF2cqpd7mvG](https://www.youtube.com/playlist?list=PL46RDqX0ZGh_ncPKxFTxOSPF2cqpd7mvG) | 14 | **RSS duplicates** (Ep #1–#10 plus later FoE episodes). Skip for unique ingest. |
| About Gifted Minds | [PL46RDqX0ZGh9ez9WB9-FnbU1ax6ry9OMU](https://www.youtube.com/playlist?list=PL46RDqX0ZGh9ez9WB9-FnbU1ax6ry9OMU) | 6 | Mix (below) |

**About Gifted Minds (unique vs overlapping):**

| Title | Unique vs RSS? |
|---|---|
| Meet Gifted Minds | Unique branding (~2 min) |
| Gifted Minds: The story behind the idea | Unique branding |
| Gifted Minds: What's in a name? | Unique branding |
| Gifted Minds: Academics | Overlaps RSS *Intro to Academics* (~1:17) |
| Gifted Minds: Emotional Health | Overlaps RSS *Intro to Emotional Health* (~1:18) |
| Gifted Minds: Life Skills | Overlaps RSS *Intro to Life Skills* (~1:22; Δ1s) |

**Unique Gifted Minds ingest set:** the three branding clips that are not RSS intros. Skip Episodes playlist and the three intro twins.

### 3. Appearances — **not empty**

Playlist **MacKenzie Price Media Appearances** (6): [PL46RDqX0ZGh8bEmizco2UYHlKfWUb2-NZ](https://www.youtube.com/playlist?list=PL46RDqX0ZGh8bEmizco2UYHlKfWUb2-NZ).

| Title | Channel | On FoE uploads? | vs RSS |
|---|---|---|---|
| Here's some apps to help keep your kid learning and engaged over the summer | KGW News | No | Unique (off-channel) |
| How to Halt Summer Learning Loss for Your Children | GT School | No | Unique (off-channel) |
| Helping Kids Master Basic Math (CBS 58 Milwaukee) | GT School | No | Unique (off-channel) |
| Learning apps to help kids in improve math and science proficiency | FOX 32 Chicago | No | Unique (off-channel) |
| MacKenzie Price Keynote-The Game of Jenga in Education | GT School | No | Unique (off-channel) |
| Have You Built A Tower With Missing Pieces? | Future of Education | Yes | Unique long-form clip on FoE (not an RSS episode) |

Five of six are **other channels** (news / GT School). Ingest as guest appearances, not as FoE episode videos.

### Other unique long-form (not in the three named buckets)

Still on `@future_of_education`, not RSS episode audio (duration far from any RSS item, or clearly a clip/doc):

- *Virtual Watch Party Premiere \| TEACHERS 2.0* (~94 min)
- *Teachers 2.0 (A mini documentary…)* (~95s trailer)
- *What is 2 Hour Learning? A Brief Overview…*
- *Here's How I'm Fixing School* / *School Has Failed You... I’m Fixing It* (YouTube-native essays; duration can collide with RSS by chance — do not skip on duration alone when titles are unrelated)
- Campus / explainer clips: *Meet the School with No Teachers*, *Go to School for Only 2 Hours!*, *A new Sports Academy…*, *What Parents are saying about … Brownsville*
- Ali Abdaal visit clips (*Diverge from academics!*, *From Doctor to YouTuber*)

## Recommended ingest order (TASK-5)

Goal: agents can cite unique-YT docs; podcast duplicates must not become a second search blob.

1. **Skip (do not ingest as new corpus)**  
   - Any Videos-tab upload whose title matches an RSS episode (after stripping `S2E###` / `Ep #N`).  
   - Any Videos-tab upload ≥ 8 minutes whose duration is within 5 seconds of an RSS `itunes:duration` **and** the titles are the same episode (including rewritten titles like Stock Market / 9 Models / Pay to Read).  
   - Entire **Gifted Minds Episodes** playlist (already in RSS).  
   - RSS intro twins: *Gifted Minds: Academics / Life Skills / Emotional Health*.

2. **Ingest first — unique long-form with no RSS twin**  
   Teachers 2.0 (trailer; watch party only if a unique-doc cut is desired), 2 Hour Learning overview, YouTube-native essays, campus/explainer clips. Prefer videos with no RSS duration twin.

3. **Appearances**  
   The five off-channel items, then the on-channel Jenga clip. Different audio than the podcast RSS.

4. **Gifted Minds branding**  
   *Meet Gifted Minds*, *The story behind the idea*, *What's in a name?*

5. **Shorts last**  
   High volume (188). Many are clips of content already in RSS or in step 2. Ingest a curated subset (the 22-item playlist) before the rest of the Shorts tab, or skip clip-Shorts whose parent episode is already indexed.

**Do not** ingest `@thealphaschool` in the same pass as podcast-unique YT unless TASK-5 explicitly expands scope.

## Method notes

**No downloads / no Chroma.** Commands used `--flat-playlist` and `--skip-download` only. Helper: `scripts/inventory_youtube_rss.py`. Artifacts written under `/tmp/lf_inventory/` (not the repo). `chroma_db` was not modified (mtime unchanged by this task).

```bash
# RSS count + titles (feedparser, already a project dependency)
uv run python -c "import feedparser; f=feedparser.parse('https://rss.art19.com/future-of-education'); print(len(f.entries))"

# Channel identity + counts (metadata only)
yt-dlp --flat-playlist --skip-download -j \
  'https://www.youtube.com/playlist?list=UUKHZkY1J1NyKypLn80Pj19A'
yt-dlp --flat-playlist --skip-download --print '%(id)s %(title)s' \
  'https://www.youtube.com/@future_of_education/videos'
yt-dlp --flat-playlist --skip-download --print '%(id)s %(title)s' \
  'https://www.youtube.com/@future_of_education/shorts'
yt-dlp --flat-playlist --skip-download --print '%(id)s %(title)s' \
  'https://www.youtube.com/@future_of_education/playlists'

# Re-run matcher
uv run python scripts/inventory_youtube_rss.py --json-out /tmp/lf_inventory/inventory.json
```

**Matching**

- Normalize titles: lowercase, strip `S2E356:` / `S E10:` / `Ep #N` prefixes, punctuation.
- High-confidence duplicate = normalized title match (or Gifted Minds intro ↔ *Intro to …*).
- YouTube often rewrites titles; same-recording check = RSS `itunes:duration` vs YT duration (Δ≤5s) for videos ≥ 8 minutes. Duration-only can collide (two unrelated episodes with the same length) — TASK-5 should require a title cue or manual review when topics diverge (*Here's How I'm Fixing School* vs an NFL-homeschool RSS item at 21:19 is a collision, not a duplicate).

**Tools:** `yt-dlp 2026.08.19`, `feedparser` via `uv`, Firecrawl scrape of the channel/playlists pages for playlist names and channel ID confirmation.

There is still **no YouTube ingest code** in the repo; this file is the inventory only.
