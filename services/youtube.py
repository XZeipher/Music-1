"""
services/youtube.py

Thin async wrapper around the official YouTube Data API v3 "search" endpoint.
Used to find candidate videos for a Spotify track by title + artist.

The API key is read exclusively from the ``YOUTUBE_API_KEY`` environment
variable and is never logged, returned in a response body, or otherwise
exposed outside of this module.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger("music_api.youtube")

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
REQUEST_TIMEOUT_SECONDS = 8.0


class YouTubeSearchError(Exception):
    """Raised whenever a YouTube search cannot be completed.

    ``is_config_error`` flags a missing API key so the API layer can
    surface a clear 500 instead of a misleading upstream/502 error.
    """

    def __init__(self, message: str, *, is_config_error: bool = False) -> None:
        super().__init__(message)
        self.is_config_error = is_config_error


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


def _best_thumbnail(thumbnails: Any) -> Optional[str]:
    if not isinstance(thumbnails, dict):
        return None

    # Prefer the highest quality thumbnail that is actually present.
    for key in ("maxres", "high", "medium", "default"):
        entry = thumbnails.get(key)
        if isinstance(entry, dict):
            url = entry.get("url")
            if isinstance(url, str) and url:
                return url

    return None


def _parse_search_items(payload: dict[str, Any]) -> list[YouTubeCandidate]:
    items = payload.get("items")
    if not isinstance(items, list):
        return []

    candidates: list[YouTubeCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue

        id_field = item.get("id")
        snippet = item.get("snippet")
        if not isinstance(id_field, dict) or not isinstance(snippet, dict):
            continue

        video_id = id_field.get("videoId")
        title = snippet.get("title")
        channel_title = snippet.get("channelTitle")

        if not isinstance(video_id, str) or not video_id:
            continue
        if not isinstance(title, str):
            title = ""
        if not isinstance(channel_title, str):
            channel_title = ""

        published_at = snippet.get("publishedAt")
        if not isinstance(published_at, str):
            published_at = None

        candidates.append(
            YouTubeCandidate(
                video_id=video_id,
                title=title,
                channel_title=channel_title,
                thumbnail=_best_thumbnail(snippet.get("thumbnails")),
                published_at=published_at,
                youtube_url=f"https://www.youtube.com/watch?v={video_id}",
            )
        )

    return candidates


async def search_videos(query: str, *, max_results: int = 8) -> list[YouTubeCandidate]:
    """
    Search YouTube for ``query`` (expected to be "<track title> <artist>")
    and return a list of candidate videos.

    Raises:
        YouTubeSearchError: on a missing API key, network failure, quota
            error, or malformed upstream response. An empty result list
            (no error) simply means YouTube had no matches.
    """
    query = (query or "").strip()
    if not query:
        raise YouTubeSearchError("Search query is required")

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        logger.error("YOUTUBE_API_KEY is not configured")
        raise YouTubeSearchError(
            "YouTube search is not configured on the server", is_config_error=True
        )

    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "videoCategoryId": "10",  # "Music" category
        "maxResults": str(max_results),
        "key": api_key,
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(YOUTUBE_SEARCH_URL, params=params)
    except httpx.TimeoutException as exc:
        logger.warning("YouTube search timed out for query %r", query)
        raise YouTubeSearchError("YouTube search timed out") from exc
    except httpx.HTTPError as exc:
        logger.warning("YouTube search network error for query %r: %s", query, exc)
        raise YouTubeSearchError("Failed to reach YouTube") from exc

    if response.status_code == 403:
        # Almost always an exhausted quota or a bad/restricted key.
        logger.error("YouTube API returned 403 (quota or key issue)")
        raise YouTubeSearchError("YouTube search quota or permissions error")

    if response.status_code != 200:
        logger.error(
            "YouTube API returned unexpected status %s for query %r",
            response.status_code,
            query,
        )
        raise YouTubeSearchError(f"YouTube API error (status {response.status_code})")

    try:
        payload = response.json()
    except ValueError as exc:
        logger.exception("YouTube API returned invalid JSON")
        raise YouTubeSearchError("Invalid response from YouTube") from exc

    if not isinstance(payload, dict):
        raise YouTubeSearchError("Unexpected response structure from YouTube")

    try:
        return _parse_search_items(payload)
    except Exception as exc:  # noqa: BLE001 - never leak internals to the client
        logger.exception("Failed to parse YouTube search response")
        raise YouTubeSearchError("Failed to parse YouTube response") from exc
