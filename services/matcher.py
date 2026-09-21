"""
services/matcher.py

Deterministic, generic scoring algorithm for picking the YouTube video that
best matches a given track. Nothing here is hard-coded for any
particular song or artist - every signal is computed from text on hand.

Scoring is on a 0-100 scale (a couple of small bonuses can push slightly
above 100, which is intentional headroom and harmless since we only use
scores for relative ranking).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from services.youtube import YouTubeCandidate

# Words that suggest the video is not a standard studio/official music video.
# These *penalize* a candidate rather than eliminate it outright, since a
# person might genuinely be looking for a live version, a cover, etc.
NEGATIVE_SIGNAL_WORDS: dict[str, float] = {
    "live": 12.0,
    "cover": 18.0,
    "karaoke": 30.0,
    "instrumental": 22.0,
    "remix": 15.0,
    "slowed": 20.0,
    "reverb": 20.0,
    "sped up": 20.0,
    "nightcore": 25.0,
    "8d": 22.0,
    "acoustic": 14.0,
    "reaction": 35.0,
    "tutorial": 35.0,
    "bass boosted": 22.0,
    "fan made": 20.0,
    "fanmade": 20.0,
}

# Wording that suggests a normal, official upload of the track.
POSITIVE_SIGNAL_WORDS: tuple[str, ...] = (
    "official video",
    "official music video",
    "official audio",
    "official lyric video",
    "lyric video",
    "audio",
    "visualizer",
)

OFFICIAL_CHANNEL_HINTS: tuple[str, ...] = (
    "official",
    "vevo",
    "records",
    "music",
)

_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class MatchResult:
    candidate: YouTubeCandidate
    score: float

    def to_dict(self) -> dict[str, object]:
        result = self.candidate.to_dict()
        result["match_score"] = round(self.score, 2)
        return result


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace."""
    if not text:
        return ""
    text = text.lower()
    text = _PUNCTUATION_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def tokenize(text: str) -> list[str]:
    normalized = normalize_text(text)
    if not normalized:
        return []
    return normalized.split(" ")


def _token_overlap_ratio(a_tokens: list[str], b_tokens: list[str]) -> float:
    """Fraction of a_tokens that also appear in b_tokens (0.0 - 1.0)."""
    if not a_tokens:
        return 0.0
    b_set = set(b_tokens)
    matched = sum(1 for token in a_tokens if token in b_set)
    return matched / len(a_tokens)


def _title_similarity_score(track_title: str, video_title: str) -> float:
    """
    Score (0-45) based on how closely the video title matches the track
    title: exact/near-exact match scores highest, falling back to token
    overlap for partial matches.
    """
    norm_track = normalize_text(track_title)
    norm_video = normalize_text(video_title)

    if not norm_track or not norm_video:
        return 0.0

    if norm_track == norm_video:
        return 45.0

    if norm_track in norm_video:
        # The full track title appears verbatim inside the video title -
        # very likely a correct match even with extra wording around it.
        return 40.0

    track_tokens = tokenize(track_title)
    video_tokens = tokenize(video_title)
    overlap = _token_overlap_ratio(track_tokens, video_tokens)
    return overlap * 35.0


def _artist_signal_score(artist: str, video_title: str, channel_title: str) -> float:
    """Score (0-35) for the artist name appearing in the title and/or channel."""
    norm_artist = normalize_text(artist)
    if not norm_artist:
        return 0.0

    norm_title = normalize_text(video_title)
    norm_channel = normalize_text(channel_title)

    score = 0.0
    if norm_artist in norm_title:
        score += 18.0
    if norm_artist in norm_channel:
        score += 17.0

    if score == 0.0:
        # Fall back to per-token overlap in case the artist is a multi-word
        # name that appears out of order or partially (e.g. band members).
        artist_tokens = tokenize(artist)
        title_tokens = tokenize(video_title)
        channel_tokens = tokenize(channel_title)
        title_overlap = _token_overlap_ratio(artist_tokens, title_tokens)
        channel_overlap = _token_overlap_ratio(artist_tokens, channel_tokens)
        score = max(title_overlap * 18.0, channel_overlap * 17.0)

    return score


def _wording_bonus(video_title: str) -> float:
    """Small bonus (0-10) for normal official-sounding music video wording."""
    norm_title = normalize_text(video_title)
    for phrase in POSITIVE_SIGNAL_WORDS:
        if normalize_text(phrase) in norm_title:
            return 10.0
    return 0.0


def _channel_bonus(channel_title: str) -> float:
    """Small bonus (0-10) if the channel name looks like an official/artist channel."""
    norm_channel = normalize_text(channel_title)
    for hint in OFFICIAL_CHANNEL_HINTS:
        if hint in norm_channel:
            return 10.0
    return 0.0


def _negative_signal_penalty(video_title: str) -> float:
    """Total penalty subtracted for words suggesting a non-standard version."""
    norm_title = normalize_text(video_title)
    penalty = 0.0
    for word, weight in NEGATIVE_SIGNAL_WORDS.items():
        if normalize_text(word) in norm_title:
            penalty += weight
    return penalty


def score_candidate(
    track_title: str, track_artist: str, candidate: YouTubeCandidate
) -> float:
    """
    Compute a 0-100(ish) match score for one YouTube candidate against the
    track's title and artist. Higher is a better match.
    """
    score = 0.0
    score += _title_similarity_score(track_title, candidate.title)
    score += _artist_signal_score(track_artist, candidate.title, candidate.channel_title)
    score += _wording_bonus(candidate.title)
    score += _channel_bonus(candidate.channel_title)
    score -= _negative_signal_penalty(candidate.title)

    # Clamp to a sane floor; no artificial ceiling so relative ranking
    # among strong matches is preserved.
    return max(score, 0.0)


def find_best_match(
    track_title: str,
    track_artist: str,
    candidates: list[YouTubeCandidate],
) -> Optional[MatchResult]:
    """
    Score every candidate and return the highest scoring one, or ``None``
    if ``candidates`` is empty.
    """
    if not candidates:
        return None

    best_candidate = None
    best_score = -1.0

    for candidate in candidates:
        score = score_candidate(track_title, track_artist, candidate)
        if score > best_score:
            best_score = score
            best_candidate = candidate

    if best_candidate is None:
        return None

    return MatchResult(candidate=best_candidate, score=best_score)
