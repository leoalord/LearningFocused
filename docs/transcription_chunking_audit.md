# Transcription and chunking quality audit (2026-11-08)

Upstream of retrieval and graph extraction: how well sources are transcribed, and how the transcripts become chunks. Investigation only. Nothing was reprocessed or re-transcribed.

**Scope and data.** 337 podcast episodes (`transcripts/`, `segmented_transcripts/`), 60 YouTube videos (`youtube_videos/transcripts|segmented/`), 139 Substack articles (`substack_articles/text/`). Indexed chunks were measured in the local `chroma_db/` (5,448 rows, opened read-only). `PG_DSN` isn't set and the IAP tunnel wasn't open, so Postgres itself was **not** queried. `index_pg`/`pg_sync` keep Chroma's ids and text, so the Postgres `chunks` table should match these numbers, but that is unverified. Token counts use `cl100k_base`, the tokenizer `text-embedding-3-small` uses.

**Billed and external calls.** One rerun of the 18-question eval on Chroma (18 query embeddings). Captions for 5 public YouTube videos (`yt-dlp --skip-download`, kept in the session scratchpad, not in `youtube_videos/`).

## Summary ranking

| # | Problem | Scale | Hurts retrieval | Hurts graph |
|---|---|---|---|---|
| 1 | LLM topic segments put answers in the wrong chunk (size, boundaries, misleading headers) | Median 597 tokens, p99 2,287, max 6,768; 17.7% of boundaries split a question from its answer | **High**: all 4 eval misses | Medium |
| 2 | Diarization collapse makes segmentation collapse too | 17 episodes; 8 of 11 segments empty in S2E339 | High for those episodes | **High**: whole episode credited to one speaker |
| 3 | Re-released podcast episodes indexed twice | 44 near-duplicate pairs; 347 extra segments | Medium: duplicates crowd top-k | Medium: double-counted mentions |
| 4 | Speaker names wrong or missing | 509/2,776 segments say "A"/"B"; host spelled 4 ways; narrator intro credited to a guest in ~30 episodes | Low–Medium | **High**: entity splits and misattribution |
| 5 | YouTube caption text is noisy | "Speaker:" is 12% of tokens; 2,846 `&gt;` in 18 videos; 23/56 unpunctuated | Medium for 410 chunks | Medium |
| 6 | Substack article = 1 chunk, including page chrome and reader comments | Median 2,231 tokens, max 5,353; comments in 72/139 | Medium (passage questions) | Medium–High: commenters' views read as the author's |
| 7 | Dropped and empty segments | 64 empty segments; 1.56% of podcast words dropped | Low | Low |
| 8 | Stale duplicate index rows | 45 YouTube rows from the old id scheme | Low | Low |
| 9 | ASR word accuracy | ~4–8% WER against captions, mostly number formatting | Low | Low |

The headline: **word-level transcription is fine; the losses come from segmentation and speaker handling.**

---

## 1. Topic segments put answers in the wrong chunk (biggest retrieval cost)

The pipeline asks an LLM to choose segment start and end times (`src/pipeline/audio/segment_topics.py`, `src/pipeline/youtube/segment.py`). Each segment becomes one chunk headed by `Episode / Topic / Summary` (`src/pipeline/audio/index_chroma.py:126`). There's no size cap, no overlap and no check that the LLM's summary matches the text.

**Size distribution (indexed chunks, tokens):**

| type | n | p10 | p50 | p90 | p99 | max |
|---|---|---|---|---|---|---|
| transcript_segment | 2,776 | 256 | 597 | 1,188 | 2,287 | 6,768 |
| youtube_transcript_segment | 410 | 243 | 630 | 1,617 | 2,956 | 7,389 |
| article_text | 139 | 1,369 | 2,231 | 3,245 | 4,937 | 5,353 |
| key_takeaway | 1,442 | 47 | 57 | 70 | 84 | 107 |

23 podcast segments are over 2,000 words. 38 non-empty ones are under 60 words, mostly outros.

**Thought cut at boundaries.** In 432 of 2,437 podcast boundaries (17.7%), the previous chunk ends on the host's question and the answer opens the next chunk with no question attached. Example: `segmented_transcripts/S E101 Managing Technology in the Family_segmented.json` ends "Introduction and The Digital Challenge" on "*…what are the struggles that you find parents are dealing with…?*", and "Key Struggles: Addiction and Self-Image" begins "*B: Well, absolutely. Addiction…*".

**Link to retrieval misses.** Rerun today: 14 of 18 hits (Sept report: 15 of 18). All 4 misses are `retriever_failed`, and in **every one the right episode was retrieved but the segment holding the answer wasn't**:

| miss | where the answer is | why it lost |
|---|---|---|
| `2026-india-model-test` | S2E356 segment 0, a cold-open teaser | Header says "Introduction: AI and Preserving Human Connection", and the summary never mentions India or the test |
| `s104-mastery-vs-time` | S E104 segment 3, last sentence ("*It's a time based model. It is not a personalized mastery based model.*") | The LLM opened a new segment "Time-Based vs. Mastery-Based Education" one sentence later, so the sentence that concludes the thought sits under "Systemic Barriers to Personalization" |
| `s2e233-guides-dont-lecture` | S2E233 segment 1, "Introduction of Juwan Delaney" | The answer is in the intro chunk. "The Role of a Guide vs. Teacher" is the next segment. The speaker is also "Juwan" in the transcript but "Jawaan" in the title and question |
| `2026-alpha-anywhere` (new) | S2E335 segment 10, "Upcoming Enhancements…" (holds `alphaanywhere.co`) | 4 of 5 retrieved segments were from S2E335, but not this one. The enrollment URL sits under an unrelated topic label |

The large, LLM-summarized chunks also blur graph extraction. One 1,000–2,000-word chunk mixes many claims, and the generated header gets extracted as if it were source text.

## 2. Diarization collapse makes segmentation collapse too

In 17 episodes, AssemblyAI returned one "utterance" of 1,700–5,557 words. Mostly these are the newer solo and Q+A episodes, S2E285–S2E349. `S2E253 … Dean Dan Schwartz (Part 2)` is a single 5,557-word utterance covering 0–1,915 s.

Segment text is assigned per utterance by midpoint, so the whole utterance lands in **one** segment and every other topic segment comes out empty:

- `S2E339 The Pygmalion Effect…` (an eval topic): segment word counts `[109, 0, 0, 0, 0, 2758, 0, 0, 0, 0, 94]`
- `S2E331 Why We Don't Say the Pledge…`: `[57, 37, 0, 0, 0, 3701, 0, 0, 95]`
- `S2E253`: `[0×9, 5559, 0]`

This is where most of the 64 empty segments come from. The indexer drops empty segments, so those topics simply vanish from retrieval and the graph. The whole episode is also credited to one speaker. For the graph, that means the guest's and the narrator's statements are attributed to whoever that label maps to.

## 3. Re-released podcast episodes are indexed twice

The feed re-publishes early episodes under new numbers. 8-gram shingle Jaccard similarity above 0.5 finds **44 near-duplicate pairs**, for example `S E48`/`S E154` "The Power of Yet" (0.93), `S E18`/`S E153` Micro-School FAQ (0.87) and `S E19`/`S E97` Social Capital (0.86). Also, `S E206` and `S E207` are the same recording under two different titles (0.95).

The 43 older twins carry 347 non-empty segments, about 12.5% of podcast chunks. The effect is visible in the eval: for `s104-mastery-vs-time`, both `S E153` and `S E18` took slots in the top 5. In the graph, each fact gets two episodes of provenance, and mention counts double.

## 4. Speaker names are wrong or missing (biggest graph cost)

- **Segmentation ran before speaker ID** on 64 episodes. Their segment `speakers` and inline `content` labels are `A`/`B` (509 of 2,776 indexed segments), even though all 337 raw transcripts now carry `speaker_name`. Example: `segmented_transcripts/S E104 The Revolutionary 2-Hour Learning System_segmented.json` says `A`/`B`, while the transcript's map says `A: Mackenzie Price, B: Jay Lyons`.
- **Host name.** Transcript text has `Mackenzie` 1,063 times, `MacKenzie` 198, `McKenzie` 132 and `McKinsey` 13. The speaker-ID prompt hardcodes "Mackenzie Price" (`src/pipeline/audio/identify_speakers.py:58`), so every label uses the wrong spelling, and 2 maps say "Mackenzie Price (Host)". Each variant can become a separate graph entity.
- **Narrator line misattributed.** The "Welcome to the Future of Education podcast hosted by…" line belongs to the narrator in 163 episodes. In 123 it's merged into MacKenzie's turn (usually a real host clip diarized together with the narrator). In about 30 it's credited to a **guest**: Jessica Leahy, Dana Sheridan, Kelly Davenport and others, e.g. `transcripts/S E109 What Can My Kids Learn From Failure.json`. Graph extraction can then read "*[guest] … co founder of Two Hour Learning*".
- **Generic labels** in speaker maps: `Co-host`/`Co-Host` (6), `Audience Member …` (9), `Unidentified Speaker`, `J`, `Guest (Jay/Pewter)`, `Secondary Speaker`, and others. **Name variants:** guest `Juwan` vs the title's `Jawaan`; `Novia Delgado` vs `Nolvia Delgado` in other episode titles.

## 5. YouTube caption transcripts are noisy

56 of 60 YouTube transcripts come from captions and 4 from AssemblyAI.

- Caption cues have no speaker, but `_attach_content` writes `Speaker: ` before every cue. That label is **12% of the words** in YouTube segment text (median). Example, `youtube_videos/segmented/0j23aHnSXZ0.json` segment 1: "*Speaker: so Claire you're not only homeschooling Speaker: your seven-year-old um but you've really Speaker: become…*"
- `parse_vtt` strips tags but doesn't unescape HTML entities. That leaves 2,846 `&gt;` (YouTube's `>>` speaker-change marker) across 18 videos, plus 83 `&nbsp;` and 6 `&amp;`, along with 18 `[Music]` and 3 `[Applause]`.
- 23 of 56 caption transcripts have essentially no sentence punctuation (auto captions), which weakens both the segmentation LLM and graph extraction.
- Long videos are cut into up to 7 segments of ~2,400 words (`OVA5fwcQysk`, 16.5k words). Windows over 120k characters are segmented with no context carried between windows (`segment.py:_budget_windows`).

## 6. A Substack article is one chunk, including page chrome and reader comments

`create_article_text_document` embeds the whole article as one vector (median 2,231 tokens), so a question about one paragraph competes against the whole article's average. The text is the full page converted from HTML:

- 109 of 139 articles carry nav and footer chrome: "*This site requires JavaScript…*", `Subscribe`, copyright lines, dozens of bare `-` lines. URLs are 1.4% of tokens and bare `-`/`/` another 2.8%.
- **Reader comments are included** in 72 of 139 articles (3.9% of words). Example: `substack_articles/text/i-turned-out-fine-is-horrible-rationale-c875289640.md` ends with commenter "Pam" arguing against 2-hour-learning schools. Graph extraction feeds this text as is (`src/pipeline/substack/index_neo4j.py`), so commenters' claims can be attributed to the article and its author.

The 5 Substack eval questions all hit, so this shows up more in graph quality than in the current gold set.

## 7. Dropped and empty segments

- 64 empty podcast segments (13 with `end == start`, e.g. outros at the final timestamp). Most of the rest come from item 2.
- 21,661 of 1,387,538 transcript words (1.56%) fall in no segment. Utterances whose midpoint lands in a gap between LLM timestamps are dropped. 152 episodes lose more than 1% and 39 lose more than 5%. Worst: `S E15 AI's Impact on Academic Integrity…` (10.6%) and `S E94 Helping Students with Dyslexia` (8.7%). Many episodes also have a 60–95 s uncovered tail where the last segment ends early (`S E15`, `S E111`, `S E139`, …), mostly outros.
- No utterance is duplicated across segments, and segments never overlap.

## 8. Stale duplicate index rows

Chroma holds 45 rows that current artifacts no longer produce: 43 `youtube_transcript_segment` rows from the old whole-second id scheme (e.g. `youtube_transcript_segment_CbyS8_vRdj4_3` next to `…_3.01_Math Learning Loss After COVID-19`), plus 2 podcast rows. Each has exactly the same text as a current row, so the same passage can fill two top-k slots. Prune has never run against them. Check whether Postgres has the same 45 ids.

## 9. Transcription accuracy (good)

AssemblyAI podcast transcripts were aligned word by word against YouTube captions for the same recordings:

| episode | reference | WER (aligned) | notes |
|---|---|---|---|
| S2E356 | human captions (en-US) | 4.3% | Almost all number formatting ("twenty" → "20") |
| S2E352 | human captions (en-US) | 4.0% | ASR has "GT Anywhere" and "TimeBack" right where the captions garble them |
| S2E335 | auto captions | 7.9% | Captions' `&gt;&gt;` markers and "McKenzie" make up most of the difference. "Morone" is correct |
| S2E332 | auto captions | 4.6% | |
| S2E215 | auto captions | 4.7% | Guest's name: ASR "Juwan", captions "Jawan"/"Joan" |

No substantive passage is missing from ASR. The largest missing run is 4 words of filler in S2E356, and in S2E335 the missing runs are caption-marker noise. Domain terms are consistent across the corpus: "Alpha School" 160, "Two/2 Hour Learning" 373, "TimeBack" 65 (+36 "time back"), "Brain Lift" 42, "GT School" 85. No custom vocabulary (`word_boost`) is configured. It's cheap insurance but not urgent. The one real ASR naming issue is the host's surname spelling (item 4).

---

## Recommended fixes (in priority order)

1. **Make retrieval chunks deterministic and keep topic segments as metadata.** Build chunks from utterances with a token target (~300–500, hard cap ~800) and ~1 utterance of overlap. Never end a chunk on a question: carry it into the answer's chunk. Keep the LLM topic and summary as metadata, or as a parent record for parent-child retrieval, instead of putting them in the embedded header. Put `episode title · speakers (named) · topic` in the header and drop the generated summary. Re-run `evals/run.py` (both backends) as the acceptance test. Expected to fix 3 or 4 of the current misses.
2. **Detect diarization collapse and recover from it.** Flag any transcript with an utterance over ~600 words, or with only one speaker in an episode that has a narrator. Re-run AssemblyAI with `speakers_expected`, or split long utterances at sentence boundaries before segmenting (this alone fixes the empty segments). These 17 episodes need re-transcription or re-segmentation; ask before running billed jobs.
3. **Dedupe re-released episodes.** Keep one canonical episode per shingle cluster (Jaccard > 0.7) and record the other as an alias (published date and number) in `documents.metadata`, not as separate chunks or graph sources.
4. **Fix speaker names at the source.** Re-run segmentation (or just rewrite labels) for the 64 letter-labelled episodes. Use the canonical spelling "MacKenzie Price" in the speaker-ID prompt. Add an alias table (McKenzie/McKinsey/Mackenzie → MacKenzie Price, Juwan/Jawaan, Novia/Nolvia) applied to text and graph entity resolution. Force the intro line ("Welcome to the Future of Education podcast…") to Narrator.
5. **Clean YouTube caption text.** Unescape HTML entities in `parse_vtt`. Treat `>>` as a turn boundary and drop `[Music]`. Stop writing `Speaker:` when no speaker is known. Consider AssemblyAI (with diarization and punctuation) for long caption-only videos.
6. **Chunk Substack properly.** Extract only the article body (title through the end of the post, no comments or footer). Split by headings and paragraphs to ~400 tokens with the title and section heading as a header. Keep the summary overview vector as is.
7. **Close coverage gaps.** After segmentation, assign any unassigned utterance to the nearest segment, and extend the last segment to the transcript end.
8. **Prune stale rows.** Run `index_pg --prune-stale --include-youtube` once after checking Postgres has the same 45 rows.

The measurement scripts are in the shared session notes folder (`side-session-notes/local_6888f5fb-…/transcription_chunking_audit/`), so the numbers can be re-measured after fixes.

## Follow-up: chunking from transcripts (branch `claude/transcript-chunking`)

Fixes 1 (partly), 2 (partly), 4 (partly) and 7 are in `src/pipeline/audio/chunking.py`. Chunks
are rebuilt from the raw transcripts at index time, so no transcription or segmentation
LLM call was re-run.

| | before (topic segments) | after |
|---|---|---|
| chunks | 2,776 indexed (+64 empty, dropped) | 3,948 |
| tokens p10 / p50 / p90 / p99 / max | 256 / 597 / 1,188 / 2,287 / 6,768 | 225 / 463 / 688 / 784 / 800 |
| question at the end of a chunk, answer in the next | 432 boundaries | 547 questions moved to their answer's chunk |
| words in no chunk | 21,661 (1.56%) | 0 |
| A/B speaker labels | 509 segments | 0 (names from the transcript) |
| narrator intro credited to a host or guest | 31 turns | 0 |

Retrieval eval (Chroma, 18 questions, scratch copy of the index re-embedded with
`text-embedding-3-small`):

| max segments | before: hits, phrases | after: hits, phrases |
|---|---|---|
| 5 (default) | 14/18, 38/42 | 14/18, 38/42 |
| 7 | 15/18, 39/42 | 14/18, 38/42 |
| 10 | 16/18, 40/42 | 14/18, 38/42 |

`2026-alpha-anywhere` now hits; `2026-esa-gt-anywhere` now misses (its "GT Anywhere"
passage ranks lower). Retrieval is flat on this set; the gains are in what the chunks
contain. Variants that scored worse and were dropped: fixed ~400- and ~550-token windows
ignoring topics (13/18, 36/42 at k=5), the same with the LLM summary in the header
(13/18, 36/42), and topic-aligned chunks with the summary (12/18, 36/42). The set is too
small to tune against: most differences are one or two phrases.

Still open: the narrator fix covers only the "hosted by" intro sentence pattern; S2E253
and S2E285 (live and audience Q+A) need re-transcription with `speakers_expected`
(billed); guest-name aliases (Juwan/Jawaan, Novia/Nolvia); and fixes 3, 5, 6 and 8.
`2026-india-model-test` and `s104-mastery-vs-time` miss under every variant, because of a
cold-open teaser and re-released duplicates (fix 3).
