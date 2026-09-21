"""
api/index.py

FastAPI application entrypoint. This module is the Vercel Python entrypoint:
Vercel's Python runtime detects the module-level ``app`` variable below and
runs it as an ASGI application, so no separate server-start code or
Mangum/WSGI adapter is needed.

Request flow for GET /api/search?q=<query>:

    request -> ytmusicapi song search -> track metadata
             -> ytmusicapi video search -> matching algorithm
             -> extract_audio(video_id) -> JSON response

No API keys or credentials are needed anywhere in this flow.
"""

from __future__ import annotations

import logging
import os
import sys

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Allow `import services.xxx` to work both when this file is run directly
# by Vercel's Python runtime and when run locally with `uvicorn api.index:app`.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.extractor import extract_audio  # noqa: E402
from services.matcher import find_best_match  # noqa: E402
from services.youtube import (  # noqa: E402
    YouTubeSearchError,
    candidate_from_track,
    search_videos,
)
from services.ytmusic import TrackSearchError, search_track  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("music_api")

app = FastAPI(
    title="Music Search API",
    description=(
        "Searches YouTube Music (via ytmusicapi, no credentials required) and "
        "finds the best matching YouTube video for a given music query."
    ),
    version="2.0.0",
)

# CORS is wide open initially so any frontend can call this API during
# development. Before shipping to production, replace allow_origins=["*"]
# with your actual frontend origin(s), e.g. ["https://your-frontend.com"].
ALLOWED_ORIGINS = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/search")
async def search(q: str = Query(default="", description="Music search query")):
    query = (q or "").strip()

    if not query:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Search query is required"},
        )

    # Step 1: Look the track up on YouTube Music (no credentials required).
    try:
        track = await search_track(query)
    except TrackSearchError as exc:
        if exc.not_found:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "Track not found"},
            )
        logger.warning("Track search failed for query %r: %s", query, exc)
        return JSONResponse(
            status_code=502,
            content={"success": False, "error": "Failed to search YouTube Music"},
        )
    except Exception:  # noqa: BLE001 - never leak internals to the client
        logger.exception("Unexpected error during track search for query %r", query)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )

    track_dict = track.to_dict()

    # Step 2: Search YouTube using the track title + artist.
    youtube_query = f"{track.title} {track.artist}".strip()
    try:
        candidates = await search_videos(youtube_query)
    except YouTubeSearchError as exc:
        logger.warning("YouTube search failed for query %r: %s", youtube_query, exc)
        return JSONResponse(
            status_code=502,
            content={"success": False, "error": "Failed to search YouTube"},
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Unexpected error during YouTube search for query %r", youtube_query
        )
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )

    # The track's own official audio upload is always a valid candidate, so
    # a response with track metadata but no video is no longer possible
    # unless the matcher itself finds nothing.
    own_upload = candidate_from_track(track)
    if not any(c.video_id == own_upload.video_id for c in candidates):
        candidates.append(own_upload)

    # Step 3: Run the matching algorithm to pick the best candidate.
    try:
        match = find_best_match(track.title, track.artist, candidates)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error while matching candidates for %r", query)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )

    if match is None:
        return {
            "success": True,
            "query": query,
            "track": track_dict,
            "youtube": None,
            "stream": None,
        }

    youtube_dict = match.to_dict()

    # Step 4: Pass the selected video ID to the (currently unimplemented)
    # audio extractor. The rest of the API keeps working when this
    # returns None.
    try:
        stream = extract_audio(match.candidate.video_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "extract_audio raised unexpectedly for video_id %r",
            match.candidate.video_id,
        )
        stream = None

    return {
        "success": True,
        "query": query,
        "track": track_dict,
        "youtube": youtube_dict,
        "stream": stream,
    }
