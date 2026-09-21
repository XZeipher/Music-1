"""
services/ytmusic.py

Thin wrapper around the third-party ``ytmusicapi`` package
(https://github.com/sigma67/ytmusicapi), which talks to the same internal API
that music.youtube.com uses in the browser.

Why this exists
----------------
``ytmusicapi`` needs NO API key, OAuth token, or Google account for searching.
This module is the single place where the rest of the app touches it:

* ``search_track(query)``  -> the best "song" match as a ``NormalizedTrack``
  (this replaces the old Spotify/SpotAPI lookup).
* ``raw_search(...)``      -> a shared, timeout-protected search helper that
  ``services/youtube.py`` also uses to find candidate videos.

``ytmusicapi`` is a *synchronous*, *unofficial* library. Two consequences:

1. Every call is pushed onto a worker thread (``asyncio.to_thread``) so it
   never blocks FastAPI's event loop, and is wrapped in a timeout.
2. The shape of the JSON it returns can change without notice, so everything
   below reads results defensively (``.get()`` + type checks) and normalizes
   failures into ``TrackSearchError`` / ``YTMusicRequestError``. If the
   library's output ever changes, only this file needs updating.

Result shape for ``search(query, filter="songs")`` (abridged)::

    {
      "category": "Songs",
      "resultType": "song",
      "videoId": "<11 char id>",
      "title": "<track title>",
      "artists": [{"name": "<artist>", "id": "<channel id>"}, ...],
      "album": {"name": "<album>", "id": "<browse id>"},   # may be null
      "duration": "3:20",                                   # may be null
      "duration_seconds": 200,                              # may be missing
      "isExplicit": false,
      "thumbnails": [{"url": "...", "width": 226, "height": 226}, ...]
    }
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("music_api.ytmusic")

REQUEST_TIMEOUT_SECONDS = 8.0

# Album-art URLs on googleusercontent.com carry their size in the URL
# (``...=w226-h226-l90-rj``). Search results only include small versions, so
# we ask for a larger one.
_ARTWORK_TARGET_SIZE = 544
_GOOGLE_IMAGE_SIZE_RE = re.compile(r"=w(\d+)-h(\d+)")


class YTMusicRequestError(Exception):
    """Raised when a request to YouTube Music cannot be completed
    (timeout, network error, upstream error, or unexpected response)."""


class TrackSearchError(Exception):
    """Raised whenever a track search cannot be completed.

    ``not_found`` distinguishes "we talked to YouTube Music fine but there
    were no results" from every other failure mode (network, parsing,
    upstream package errors), so the API layer can return 404 vs 502.
    """

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


@dataclass
class NormalizedTrack:
    """Our own internal representation of a track.

    This is the ONLY thing the rest of the application knows about -
    nothing downstream ever touches a raw ytmusicapi response.

    ``id`` is the YouTube video ID of the track's official audio upload.
    """

    id: str
    title: str
    artist: str
    album: Optional[str]
    artwork: Optional[str]
    duration_ms: Optional[int]
    ytmusic_url: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "artwork": self.artwork,
            "duration_ms": self.duration_ms,
            "ytmusic_url": self.ytmusic_url,
        }


# --------------------------------------------------------------------------
# Shared client + search helper
# --------------------------------------------------------------------------

_client: Any = None
_client_lock = threading.Lock()


def get_client() -> Any:
    """Return a lazily created, process-wide ``YTMusic`` client.

    The import happens here (not at module import time) so a broken or
    missing ``ytmusicapi`` installation only breaks the search endpoint, not
    the whole app - ``/api/health`` keeps working.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from ytmusicapi import YTMusic

                _client = YTMusic()
    return _client


def reset_client() -> None:
    """Drop the cached client so the next call builds a fresh one."""
    global _client
    with _client_lock:
        _client = None


async def raw_search(query: str, *, search_filter: str, limit: int) -> list[Any]:
    """
    Run ``YTMusic.search(query, filter=search_filter, limit=limit)`` on a
    worker thread with a timeout and return the raw list of result dicts.

    Raises:
        YTMusicRequestError: on timeout, any exception raised by
            ytmusicapi, or a response that is not a list.
    """

    def _call() -> Any:
        return get_client().search(query, filter=search_filter, limit=limit)

    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(_call), timeout=REQUEST_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        logger.warning(
            "YouTube Music %r search timed out for query %r", search_filter, query
        )
        raise YTMusicRequestError("YouTube Music request timed out") from exc
    except Exception as exc:  # noqa: BLE001 - never leak internals to the client
        logger.exception(
            "YouTube Music %r search failed for query %r", search_filter, query
        )
        # A client that has gone stale (e.g. expired session state) would
        # otherwise keep failing for the life of this warm process.
        reset_client()
        raise YTMusicRequestError("YouTube Music request failed") from exc

    if not isinstance(results, list):
        logger.error("ytmusicapi returned a non-list search result")
        raise YTMusicRequestError("Unexpected response structure from YouTube Music")

    return results


# --------------------------------------------------------------------------
# Result parsing helpers (shared with services/youtube.py)
# --------------------------------------------------------------------------


def best_thumbnail(thumbnails: Any) -> Optional[str]:
    """Pick the largest thumbnail URL from a ``thumbnails`` list."""
    if not isinstance(thumbnails, list):
        return None

    best_url: Optional[str] = None
    best_area = -1
    for entry in thumbnails:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            continue
        try:
            area = int(entry.get("width") or 0) * int(entry.get("height") or 0)
        except (TypeError, ValueError):
            area = 0
        if area > best_area:
            best_url, best_area = url, area

    return best_url


def _upscale_artwork(url: str) -> str:
    """Request a larger version of a googleusercontent.com album-art URL."""
    if "googleusercontent.com" not in url and "ggpht.com" not in url:
        return url

    def _replace(match: re.Match[str]) -> str:
        width, height = int(match.group(1)), int(match.group(2))
        if max(width, height) >= _ARTWORK_TARGET_SIZE:
            return match.group(0)  # already big enough
        return f"=w{_ARTWORK_TARGET_SIZE}-h{_ARTWORK_TARGET_SIZE}"

    return _GOOGLE_IMAGE_SIZE_RE.sub(_replace, url, count=1)


def artist_names(artists_field: Any) -> list[str]:
    """Extract every artist display name from an ``artists`` list."""
    if not isinstance(artists_field, list):
        return []

    names: list[str] = []
    for artist in artists_field:
        if isinstance(artist, dict):
            name = artist.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def _parse_duration_text(text: Any) -> Optional[int]:
    """Convert ``"3:20"`` / ``"1:02:03"`` into seconds."""
    if not isinstance(text, str) or not text.strip():
        return None

    seconds = 0
    for part in text.strip().split(":"):
        if not part.isdigit():
            return None
        seconds = seconds * 60 + int(part)
    return seconds


def _duration_ms(item: dict[str, Any]) -> Optional[int]:
    seconds = item.get("duration_seconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        return int(seconds * 1000)

    parsed = _parse_duration_text(item.get("duration"))
    if parsed is not None and parsed > 0:
        return parsed * 1000

    return None


def _album_name(album_field: Any) -> Optional[str]:
    if isinstance(album_field, dict):
        name = album_field.get("name")
        if isinstance(name, str) and name:
            return name
    elif isinstance(album_field, str) and album_field:
        return album_field
    return None


def _normalize_track(item: Any) -> Optional[NormalizedTrack]:
    """Convert one raw ``search(filter="songs")`` result into a NormalizedTrack."""
    if not isinstance(item, dict):
        return None

    video_id = item.get("videoId")
    title = item.get("title")

    if not isinstance(video_id, str) or not video_id:
        # Without a video id and a title there's nothing usable to return.
        return None
    if not isinstance(title, str) or not title:
        return None

    names = artist_names(item.get("artists"))
    artist = names[0] if names else ""

    artwork = best_thumbnail(item.get("thumbnails"))
    if artwork:
        artwork = _upscale_artwork(artwork)

    return NormalizedTrack(
        id=video_id,
        title=title,
        artist=artist,
        album=_album_name(item.get("album")),
        artwork=artwork,
        duration_ms=_duration_ms(item),
        ytmusic_url=f"https://music.youtube.com/watch?v={video_id}",
    )


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


async def search_track(query: str, *, limit: int = 5) -> NormalizedTrack:
    """
    Search YouTube Music (via ytmusicapi, no credentials required) for
    ``query`` and return the top "song" result as a NormalizedTrack.

    Raises:
        TrackSearchError: with ``not_found=True`` if YouTube Music has no
            matching songs, or ``not_found=False`` for any network,
            parsing, or upstream package failure.
    """
    query = (query or "").strip()
    if not query:
        raise TrackSearchError("Search query is required")

    try:
        results = await raw_search(query, search_filter="songs", limit=limit)
    except YTMusicRequestError as exc:
        raise TrackSearchError("Failed to search YouTube Music") from exc

    try:
        for item in results:
            normalized = _normalize_track(item)
            if normalized is not None:
                return normalized
    except Exception as exc:  # noqa: BLE001 - never leak internals to the client
        logger.exception("Failed to parse YouTube Music response for query %r", query)
        raise TrackSearchError("Failed to parse YouTube Music response") from exc

    raise TrackSearchError(f"No track found for {query!r}", not_found=True)
