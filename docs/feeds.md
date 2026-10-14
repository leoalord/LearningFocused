# Feeds the daily job checks

`src/feeds/sources.json` is the list. Each entry has a `key`, a `kind`, a `cadence` (`daily`, `weekly` or `manual`), `enabled`, and a `config` for that kind. With Postgres configured it is mirrored into the `sources` table, which also keeps each source's last check, last success and last error.

| Key | Kind | What | Cadence |
| --- | --- | --- | --- |
| `art19` | `podcast_rss` | Future of Education podcast RSS (`rss.art19.com/future-of-education`). New episode MP3s are transcribed with AssemblyAI. | daily |
| `substack` | `substack` | `futureofeducation.substack.com/feed` | daily |
| `youtube` | `youtube_channel` | Whole `@future_of_education` Videos tab plus the "MacKenzie Price Media Appearances" and "About Gifted Minds" playlists. Videos that are the same recording as a podcast episode are skipped. Shorts are not included. | daily |
| `instagram` | `instagram` | Off until `accounts` and `INSTAGRAM_ACCESS_TOKEN` are set | daily |
| `x`, `linkedin`, `tiktok` | same | Off until accounts and credentials are set | weekly |

Weekly sources run when 6.5 days have passed since their last check (or on Mondays when there is no Postgres state).

## Turning on a social source

1. Add the account handles to `config.accounts` and set `enabled: true`.
2. Put the platform token in Secret Manager and pass it to the job as the env var named in `config.credential_env`.
3. Write the fetcher: `fetch(source, token) -> list[SocialPost]` in `src/feeds/social.py`, registered in `FETCHERS`. Until a fetcher exists the source fails loudly every run, so a half-configured source never looks healthy.

Instagram's Graph API only reads accounts you own or manage (a Business/Creator account linked to a Facebook Page). Public accounts you don't manage need a different route.

## Adding another podcast or Substack

The audio and Substack pipelines read their feed URL from `src/config.py`, so today a second `podcast_rss` or `substack` entry fails with a clear error. Parameterising those pipelines is the next step if more feeds are wanted.
