"""
services/extractor.py

Isolated placeholder for turning a YouTube video ID into a playable audio
stream/source. This is intentionally NOT implemented yet.

Do not add a YouTube downloading/extraction library here. When you are
ready to wire this up to an audio source you are authorized to stream,
replace the body of ``extract_audio`` - the rest of the application only
depends on this function's signature and on it returning ``None`` (or, in
the future, some serializable value) without raising for a valid video ID.
"""

from __future__ import annotations

from typing import Any, Optional


def extract_audio(video_id: str) -> Optional[Any]:
    pass
