"""YouTube ingest constants (channel, playlists, V1 cap).

Channel in scope: @future_of_education. @thealphaschool is out of scope.
"""

from __future__ import annotations

FOE_HANDLE = "@future_of_education"
FOE_CHANNEL_ID = "UCKHZkY1J1NyKypLn80Pj19A"
FOE_CHANNEL_URL = "https://www.youtube.com/@future_of_education"
FOE_UPLOADS_PLAYLIST_ID = f"UU{FOE_CHANNEL_ID[2:]}"

PLAYLIST_APPEARANCES = "PL46RDqX0ZGh8bEmizco2UYHlKfWUb2-NZ"
PLAYLIST_ABOUT_GIFTED_MINDS = "PL46RDqX0ZGh9ez9WB9-FnbU1ax6ry9OMU"
PLAYLIST_GIFTED_MINDS_EPISODES = "PL46RDqX0ZGh_ncPKxFTxOSPF2cqpd7mvG"
PLAYLIST_SHORTS_CURATED = "PL46RDqX0ZGh-pTUWEX9mPEuuLBuujHt-B"

PLAYLIST_URLS = {
    "appearances": f"https://www.youtube.com/playlist?list={PLAYLIST_APPEARANCES}",
    "about_gifted_minds": f"https://www.youtube.com/playlist?list={PLAYLIST_ABOUT_GIFTED_MINDS}",
    "gifted_minds_episodes": f"https://www.youtube.com/playlist?list={PLAYLIST_GIFTED_MINDS_EPISODES}",
    "shorts_curated": f"https://www.youtube.com/playlist?list={PLAYLIST_SHORTS_CURATED}",
}

VIDEOS_TAB_URL = "https://www.youtube.com/@future_of_education/videos"

# Inventory unique-longform titles (shorter items first; skip 94-min watch party).
UNIQUE_LONGFORM_TITLE_HINTS = [
    "teachers 2.0 (a mini documentary",
    "what is 2 hour learning",
    "meet the school with no teachers",
    "here's how i'm fixing school",
    "go to school for only 2 hours",
    "school has failed you",
    "a new sports academy",
    "what parents are saying about",
]

RSS_INTRO_TWIN_TITLES = (
    "Gifted Minds: Academics",
    "Gifted Minds: Emotional Health",
    "Gifted Minds: Life Skills",
)

# V1 cap (TASK-5 first pass). Not the full unique inventory.
V1_APPEARANCES = 6
V1_BRANDING = 3
V1_UNIQUE_LONGFORM = 3
V1_SHORTS = 3

# Known RSS duplicate used for skip proof (inventory pair #2).
SKIP_PROOF_VIDEO_ID = "i9mL6DbSRaA"
SKIP_PROOF_TITLE_HINT = "Alpha Anywhere"

BUCKET_UNIQUE_LONGFORM = "unique_longform"
BUCKET_APPEARANCE = "appearance"
BUCKET_BRANDING = "gifted_minds_branding"
BUCKET_SHORT = "short"

SOURCE_TYPE_VIDEO = "youtube_video"
SOURCE_TYPE_SHORT = "youtube_short"
