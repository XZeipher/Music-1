"""
services/youtube.py

Finds candidate YouTube videos for a track by title + artist.

This used to call the official YouTube Data API v3 (which needed an API key
and burned 100 quota units per search). It now uses ``ytmusicapi`` instead,
through the shared helper in ``services/ytmusic.py`` - so there is no API
key, no quota, and no extra HTTP client to configure.

``ytmusicapi``'s ``search(query, filter="videos")`` results look like::

    {
      "category": "Videos",
      "resultType": "video",
      "videoId": "<11 char id>",
      "title": "<video title>",
      "artists": [{"name": "<uploader / artist>", "id": "<channel id>"}],
      "views": "1.4M",
      "videoType": "MUSIC_VIDEO_TYPE_OMV",
      "duration": "4:38",
      "duration_seconds": 278,
      "thumbnails": [{"url": "...", "width": 400, "height": 225}, ...]
    }

Note the trade-off versus the Data API: results do not include a publish
date (``published_at`` is always ``None``), and "channel" is the artist /
uploader name(s) that YouTube Music shows for the video.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from services.ytmusic import (
    NormalizedTrack,
    YTMusicRequestError,
    artist_names,
    best_thumbnail,
    raw_search,
)

logger = logging.getLogger("music_api.youtube")


class YouTubeSearchError(Exception):
    """Raised whenever a YouTube search cannot be completed.

    An empty result list (no error) simply means YouTube had no matches.
    """


@dataclass
class YouTubeCandidate:
    """A single candidate video returned by the YouTube search."""

    video_id: str
    title: str
    channel_title: str
    thumbnail: Optional[str]
    published_at: Optional[str]
    youtube_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "channel_title": self.channel_title,
            "thumbnail": self.thumbnail,
            "published_at": self.published_at,
            "youtube_url": self.youtube_url,
        }


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def candidate_from_track(track: NormalizedTrack) -> YouTubeCandidate:
    """Build a candidate from the track's own official audio upload.

    YouTube Music already tells us which video is the studio version of the
    song, so it is always a valid fallback (and is scored alongside the
    other candidates by the matcher).
    """
    return YouTubeCandidate(
        video_id=track.id,
        title=track.title,
        channel_title=track.artist,
        thumbnail=track.artwork,
        published_at=None,
        youtube_url=_watch_url(track.id),
    )


def _parse_video_results(results: list[Any]) -> list[YouTubeCandidate]:
    candidates: list[YouTubeCandidate] = []
    seen: set[str] = set()

    for item in results:
        if not isinstance(item, dict):
            continue

        video_id = item.get("videoId")
        if not isinstance(video_id, str) or not video_id or video_id in seen:
            continue
        seen.add(video_id)

        title = item.get("title")
        if not isinstance(title, str):
            title = ""

        thumbnail = best_thumbnail(item.get("thumbnails"))
        if thumbnail is None:
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        candidates.append(
            YouTubeCandidate(
                video_id=video_id,
                title=title,
                channel_title=", ".join(artist_names(item.get("artists"))),
                thumbnail=thumbnail,
                published_at=None,
                youtube_url=_watch_url(video_id),
            )
        )

    return candidates


async def search_videos(query: str, *, max_results: int = 8) -> list[YouTubeCandidate]:
    """
    Search YouTube (via ytmusicapi) for ``query`` (expected to be
    "<track title> <artist>") and return a list of candidate videos.

    Raises:
        YouTubeSearchError: on a network failure, timeout, or malformed
            upstream response. An empty result list (no error) simply
            means YouTube had no matches.
    """
    query = (query or "").strip()
    if not query:
        raise YouTubeSearchError("Search query is required")

    try:
        results = await raw_search(query, search_filter="videos", limit=max_results)
    except YTMusicRequestError as exc:
        raise YouTubeSearchError("Failed to search YouTube") from exc

    try:
        return _parse_video_results(results)[:max_results]
    except Exception as exc:  # noqa: BLE001 - never leak internals to the client
        logger.exception("Failed to parse YouTube search response")
        raise YouTubeSearchError("Failed to parse YouTube response") from exc
