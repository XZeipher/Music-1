# Music Search API

A FastAPI service that looks up a song on YouTube Music, finds the
best-matching YouTube video for it, and returns a single clean JSON payload.
It's deployable directly to Vercel as a serverless Python function.

**No API keys, no accounts, no environment variables** — everything goes
through [`ytmusicapi`](https://github.com/sigma67/ytmusicapi).

## 1. What the API does

```
GET /api/search?q=Blinding%20Lights
```

1. Takes your search query (`q`).
2. Searches YouTube Music's **songs** catalog using **ytmusicapi** — an
   unofficial wrapper around the same internal API that music.youtube.com
   uses. No API key, OAuth flow, or Google account is required.
3. Normalizes the top result into title / artist / album / artwork /
   duration / YouTube Music URL.
4. Searches YouTube **videos** (also via ytmusicapi) for
   `"<track title> <artist>"`.
5. Adds the track's own official audio upload to the candidate list (so there
   is always at least one candidate).
6. Scores every candidate with a deterministic matching algorithm and picks
   the best one.
7. Calls a placeholder `extract_audio(video_id)` function (currently
   unimplemented — see [section 10](#10-current-extractor-limitation)).
8. Returns everything as JSON.

No Spotify, no YouTube Data API, no database, no Redis.

## 2. Architecture

```
Client
  │  GET /api/search?q=...
  ▼
api/index.py  (FastAPI app, Vercel entrypoint)
  │
  ├─► services/ytmusic.py   → ytmusicapi (search, filter="songs")  → NormalizedTrack
  │
  ├─► services/youtube.py   → ytmusicapi (search, filter="videos") → list[YouTubeCandidate]
  │
  ├─► services/matcher.py   → deterministic scoring → best MatchResult
  │
  └─► services/extractor.py → extract_audio(video_id)  (currently `pass`)
```

Each service module is self-contained and only exposes a small, stable
function/interface to the rest of the app. If ytmusicapi's output changes, or
you swap the audio extractor for a real implementation, you only touch that
one file.

## 3. Project structure

```
music-api/
├── api/
│   └── index.py          # FastAPI app + Vercel entrypoint
├── services/
│   ├── ytmusic.py         # ytmusicapi client + song lookup → NormalizedTrack
│   ├── youtube.py         # ytmusicapi video search → YouTubeCandidate
│   ├── matcher.py         # track ↔ video matching/scoring
│   └── extractor.py       # extract_audio() placeholder (unimplemented)
├── requirements.txt
├── vercel.json
├── .gitignore
└── README.md
```

## 4. ytmusicapi dependency

This project uses [`ytmusicapi`](https://github.com/sigma67/ytmusicapi)
(`from ytmusicapi import YTMusic`), unauthenticated.

- `services/ytmusic.py` calls `YTMusic().search(query, filter="songs")` and
  walks the result defensively (every field access has a fallback), turning
  it into our own stable `NormalizedTrack` shape. The rest of the app never
  touches a raw ytmusicapi response.
- `services/youtube.py` calls `YTMusic().search(query, filter="videos")` for
  candidate videos.
- ytmusicapi is **synchronous**, so every call runs on a worker thread
  (`asyncio.to_thread`) with an 8-second timeout, and one `YTMusic` client is
  reused across requests within a warm serverless instance.
- ytmusicapi is **unofficial**. It can break or change shape without notice,
  and YouTube may rate-limit or block requests coming from cloud/datacenter
  IP addresses. If you start seeing 502 errors from `/api/search`, check the
  [ytmusicapi issues](https://github.com/sigma67/ytmusicapi/issues) first and
  try upgrading the package — this is a known characteristic of any tool
  built on private endpoints, not a bug specific to this project.
- Requires Python 3.10+ (ytmusicapi's minimum).

## 5. Environment variables

None. Nothing in this project reads a secret or an API key.

## 6. Local installation

```bash
git clone <this-repo>
cd music-api
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Requires Python 3.11+.

## 7. Running locally

```bash
uvicorn api.index:app --reload --port 8000
```

The API is now available at `http://localhost:8000`.

## 8. API examples

**Health check**

```bash
curl http://localhost:8000/api/health
```

```json
{ "status": "ok" }
```

**Search** (values below are illustrative)

```bash
curl "http://localhost:8000/api/search?q=Blinding%20Lights"
```

```json
{
  "success": true,
  "query": "Blinding Lights",
  "track": {
    "id": "<video id of the official audio upload>",
    "title": "Blinding Lights",
    "artist": "The Weeknd",
    "album": "After Hours",
    "artwork": "https://lh3.googleusercontent.com/...=w544-h544-l90-rj",
    "duration_ms": 200000,
    "ytmusic_url": "https://music.youtube.com/watch?v=<id>"
  },
  "youtube": {
    "video_id": "4NRXx6U8ABQ",
    "title": "Blinding Lights (Official Video)",
    "channel_title": "The Weeknd",
    "thumbnail": "https://i.ytimg.com/vi/4NRXx6U8ABQ/sddefault.jpg",
    "published_at": null,
    "youtube_url": "https://www.youtube.com/watch?v=4NRXx6U8ABQ",
    "match_score": 92.5
  },
  "stream": null
}
```

Notes on the fields:

- `track.id` is a YouTube video ID (the track's official audio upload).
- `youtube.channel_title` is the artist/uploader name(s) YouTube Music shows
  for the video.
- `youtube.published_at` is always `null` — ytmusicapi's search results don't
  include a publish date. The field is kept so existing clients don't break.

**Missing query**

```bash
curl "http://localhost:8000/api/search?q="
```

```json
{ "success": false, "error": "Search query is required" }
```
→ HTTP 400

**No matching song**

```json
{ "success": false, "error": "Track not found" }
```
→ HTTP 404

**Upstream failure** (YouTube Music unreachable, timing out, or erroring)
→ HTTP 502, with `"Failed to search YouTube Music"` (song lookup) or
`"Failed to search YouTube"` (video search).

**Unexpected internal error** → HTTP 500, generic message only — no stack
traces or internals are ever included in a response.

If the video search returns nothing, the track's own official audio upload is
used, so `youtube` is populated whenever the song lookup succeeds.

## 9. Vercel deployment

1. Push this project to a Git repository.
2. Import it into [Vercel](https://vercel.com/new).
3. Deploy. (No environment variables to configure.)

Once deployed:

```
https://YOUR-DOMAIN.vercel.app/api/health
https://YOUR-DOMAIN.vercel.app/api/search?q=Blinding%20Lights
```

`vercel.json` declares `api/index.py` as an ASGI entrypoint (Vercel's
Python runtime detects the module-level `app` object automatically) —
there is no long-running server process, which keeps this compatible with
Vercel's serverless execution model.

### CORS

CORS currently allows all origins (`allow_origins=["*"]`) so you can call
this API from any frontend while developing. Before going to production,
open `api/index.py` and replace:

```python
ALLOWED_ORIGINS = ["*"]
```

with your actual frontend origin(s):

```python
ALLOWED_ORIGINS = ["https://your-frontend-domain.com"]
```

## 10. Current extractor limitation

`services/extractor.py` intentionally contains only:

```python
def extract_audio(video_id: str) -> Optional[Any]:
    pass
```

This is **not implemented on purpose**. No YouTube audio-extraction or
downloading library is installed or used anywhere in this project. As a
result, `"stream"` in every `/api/search` response is currently `null`.

The rest of the API is fully functional without it — track metadata, video
matching, and match scoring all work end-to-end. When you have an audio
source you are authorized to stream from, implement the body of
`extract_audio` in `services/extractor.py`; nothing else in the codebase
needs to change, since `api/index.py` already calls this function and
passes its return value straight through as `"stream"` in the response.

## Security notes

- This project holds no credentials: no API keys, tokens, or secrets are
  read, stored, logged, or returned.
- All unexpected errors are caught and converted into a generic
  `{"success": false, "error": "Internal server error"}` response with an
  HTTP 500 status — internal exception details and stack traces are only
  ever written to the server-side log (`logger.exception(...)`), never
  returned to the client.
