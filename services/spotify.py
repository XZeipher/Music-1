"""
services/spotify.py

Thin wrapper around the third-party ``spotapi`` package
(https://github.com/Aran404/SpotAPI), which scrapes Spotify's internal
"partner" GraphQL API the same way open.spotify.com does in the browser.

Why this exists
----------------
``spotapi`` requires NO Spotify Client ID / Client Secret / OAuth token.
Under the hood it spins up an anonymous "guest" session against Spotify's
web client, so the only thing we need to do is call ``Song().query_songs()``.

Because this is an *undocumented, reverse-engineered* API, the exact shape
of the JSON it returns can change without notice. Everything in this file
is written defensively: every dict access uses ``.get()`` with sensible
fallbacks, and any failure (network, parsing, missing fields, package-level
exceptions) is caught and normalized into a ``SpotifySearchError`` so the
rest of the application never has to know or care that SpotAPI is involved.

Response shape (confirmed against the SpotAPI README + Spotify's internal
"searchDesktop" persisted query, which is what several other tools built on
the same endpoint also observe):

    {
      "data": {
        "searchV2": {
          "tracksV2": {
            "items": [
              {
                "item": {
                  "data": {
                    "__typename": "Track",
                    "id": "<spotify track id>",
                    "name": "<track title>",
                    "uri": "spotify:track:<id>",
                    "duration": {"totalMilliseconds": 200040},
                    "albumOfTrack": {
                        "name": "<album name>",
                        "uri": "spotify:album:<id>",
                        "coverArt": {
                            "sources": [
                                {"url": "...", "width": 640, "height": 640},
                                ...
                            ]
                        }
                    },
                    "artists": {
                        "items": [
                            {"profile": {"name": "<artist name>"},
                             "uri": "spotify:artist:<id>"},
                            ...
                        ]
                    }
                  }
                }
              },
              ...
            ]
          }
        }
      }
    }

If SpotAPI's internal schema ever changes, only this file needs updating -
the rest of the app depends solely on the ``NormalizedTrack`` structure
returned by ``search_track``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("music_api.spotify")


class SpotifySearchError(Exception):
    """Raised whenever a Spotify search cannot be completed.

    ``not_found`` distinguishes "we talked to Spotify fine but there were
    no results" from every other failure mode (network, parsing, upstream
    package errors), so the API layer can return 404 vs 502 correctly.
    """

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found


@dataclass
class NormalizedTrack:
    """Our own internal representation of a Spotify track.

    This is the ONLY thing the rest of the application knows about -
    nothing downstream ever touches a raw SpotAPI response.
    """

    id: str
    title: str
    artist: str
    album: Optional[str]
    artwork: Optional[str]
    duration_ms: Optional[int]
    spotify_url: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "artwork": self.artwork,
            "duration_ms": self.duration_ms,
            "spotify_url": self.spotify_url,
        }


def _best_artwork(cover_art: Any) -> Optional[str]:
    """Pick the largest available cover art URL from a coverArt.sources list."""
    if not isinstance(cover_art, dict):
        return None

    sources = cover_art.get("sources")
    if not isinstance(sources, list) or not sources:
        return None

    def _area(source: Any) -> int:
        if not isinstance(source, dict):
            return -1
        width = source.get("width") or 0
        height = source.get("height") or 0
        try:
            return int(width) * int(height)
        except (TypeError, ValueError):
            return -1

    best = max(sources, key=_area, default=None)
    if isinstance(best, dict):
        url = best.get("url")
        if isinstance(url, str) and url:
            return url

    # Fall back to the first source that has a usable URL.
    for source in sources:
        if isinstance(source, dict):
            url = source.get("url")
            if isinstance(url, str) and url:
                return url

    return None


def _primary_artist_name(artists_field: Any) -> str:
    """Extract the first artist's display name from the artists.items list."""
    if not isinstance(artists_field, dict):
        return ""

    items = artists_field.get("items")
    if not isinstance(items, list) or not items:
        return ""

    first = items[0]
    if not isinstance(first, dict):
        return ""

    profile = first.get("profile")
    if isinstance(profile, dict):
        name = profile.get("name")
        if isinstance(name, str):
            return name

    return ""


def _track_id_from_uri(track_data: dict[str, Any]) -> str:
    track_id = track_data.get("id")
    if isinstance(track_id, str) and track_id:
        return track_id

    uri = track_data.get("uri")
    if isinstance(uri, str) and uri.startswith("spotify:track:"):
        return uri.rsplit(":", maxsplit=1)[-1]

    return ""


def _normalize_track(track_data: dict[str, Any]) -> Optional[NormalizedTrack]:
    """Convert one raw ``item.data`` track object into a NormalizedTrack."""
    if not isinstance(track_data, dict):
        return None

    track_id = _track_id_from_uri(track_data)
    title = track_data.get("name")

    if not track_id or not isinstance(title, str) or not title:
        # Without an id and a title there's nothing usable to return.
        return None

    artist = _primary_artist_name(track_data.get("artists"))

    album_data = track_data.get("albumOfTrack")
    album = None
    artwork = None
    if isinstance(album_data, dict):
        album_name = album_data.get("name")
        if isinstance(album_name, str):
            album = album_name
        artwork = _best_artwork(album_data.get("coverArt"))

    duration_ms = None
    duration_field = track_data.get("duration")
    if isinstance(duration_field, dict):
        raw_duration = duration_field.get("totalMilliseconds")
        if isinstance(raw_duration, (int, float)):
            duration_ms = int(raw_duration)

    spotify_url = f"https://open.spotify.com/track/{track_id}" if track_id else None

    return NormalizedTrack(
        id=track_id,
        title=title,
        artist=artist,
        album=album,
        artwork=artwork,
        duration_ms=duration_ms,
        spotify_url=spotify_url,
    )


def _extract_track_items(raw_response: Any) -> list[Any]:
    """Defensively walk data.searchV2.tracksV2.items out of the raw response."""
    if not isinstance(raw_response, dict):
        raise SpotifySearchError("Unexpected response structure from SpotAPI")

    data = raw_response.get("data")
    if not isinstance(data, dict):
        raise SpotifySearchError("Malformed SpotAPI response: missing 'data'")

    search_v2 = data.get("searchV2")
    if not isinstance(search_v2, dict):
        raise SpotifySearchError("Malformed SpotAPI response: missing 'searchV2'")

    tracks_v2 = search_v2.get("tracksV2")
    if not isinstance(tracks_v2, dict):
        # Some queries can legitimately return no track section at all.
        return []

    items = tracks_v2.get("items")
    if not isinstance(items, list):
        return []

    return items


def search_track(query: str, *, limit: int = 5) -> NormalizedTrack:
    """
    Search Spotify (via SpotAPI, no credentials required) for ``query`` and
    return the best-guess top result as a NormalizedTrack.

    Raises:
        SpotifySearchError: with ``not_found=True`` if Spotify has no
            matching tracks, or ``not_found=False`` for any network,
            parsing, or upstream package failure.
    """
    query = (query or "").strip()
    if not query:
        raise SpotifySearchError("Search query is required")

    try:
        # Imported lazily so that a broken/missing SpotAPI installation
        # only breaks the search endpoint, not the whole app (e.g. /api/health
        # keeps working even if this import fails).
        from spotapi import Song
        from spotapi.exceptions.errors import SongError
    except ImportError as exc:  # pragma: no cover - packaging issue
        logger.exception("spotapi package is not installed correctly")
        raise SpotifySearchError("Spotify search backend is unavailable") from exc

    try:
        song = Song()
        raw_response = song.query_songs(query, limit=limit)
    except SongError as exc:
        logger.warning("SpotAPI raised an error while searching %r: %s", query, exc)
        raise SpotifySearchError("Failed to search Spotify") from exc
    except Exception as exc:  # noqa: BLE001 - never leak internals to the client
        logger.exception("Unexpected error calling SpotAPI for query %r", query)
        raise SpotifySearchError("Failed to search Spotify") from exc

    try:
        items = _extract_track_items(raw_response)
    except SpotifySearchError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to parse SpotAPI response for query %r", query)
        raise SpotifySearchError("Failed to parse Spotify response") from exc

    for item in items:
        if not isinstance(item, dict):
            continue
        inner = item.get("item")
        if not isinstance(inner, dict):
            continue
        track_data = inner.get("data")
        normalized = _normalize_track(track_data) if isinstance(track_data, dict) else None
        if normalized is not None:
            return normalized

    raise SpotifySearchError(f"No Spotify track found for {query!r}", not_found=True)
