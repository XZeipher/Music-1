# Music Search API

A FastAPI service that searches for a song on Spotify (without any Spotify
credentials), finds the best-matching YouTube video for it, and returns a
single clean JSON payload. It's deployable directly to Vercel as a
serverless Python function.

## 1. What the API does

```
GET /api/search?q=Blinding%20Lights
```

1. Takes your search query (`q`).
2. Searches Spotify's catalog using **SpotAPI** — a reverse-engineered
   wrapper around Spotify's own internal ("partner") API. No Client ID,
   Client Secret, OAuth flow, or Spotify account is required.
3. Normalizes the top Spotify result into title / artist / album / artwork
   / duration / Spotify URL.
4. Searches YouTube (official YouTube Data API v3) for
   `"<track title> <artist>"`.
5. Scores every YouTube result with a deterministic matching algorithm and
   picks the best one.
6. Calls a placeholder `extract_audio(video_id)` function (currently
   unimplemented — see [section 11](#11-current-extractor-limitation)).
7. Returns everything as JSON.

No official Spotify SDK, no database, no Redis — the only external
credential this project needs is a YouTube Data API key.

## 2. Architecture

```
Client
  │  GET /api/search?q=...
  ▼
api/index.py  (FastAPI app, Vercel entrypoint)
  │
  ├─► services/spotify.py   → SpotAPI (Song.query_songs) → NormalizedTrack
  │
  ├─► services/youtube.py   → YouTube Data API v3 → list[YouTubeCandidate]
  │
  ├─► services/matcher.py   → deterministic scoring → best MatchResult
  │
  └─► services/extractor.py → extract_audio(video_id)  (currently `pass`)
```

Each service module is self-contained and only exposes a small, stable
function/interface to the rest of the app. If SpotAPI's internal schema
changes, or you swap the audio extractor for a real implementation, you
only touch that one file.

## 3. Project structure

```
music-api/
├── api/
│   └── index.py          # FastAPI app + Vercel entrypoint
├── services/
│   ├── spotify.py         # SpotAPI wrapper → NormalizedTrack
│   ├── youtube.py         # YouTube Data API v3 wrapper
│   ├── matcher.py         # Spotify ↔ YouTube matching/scoring
│   └── extractor.py       # extract_audio() placeholder (unimplemented)
├── requirements.txt
├── vercel.json
├── .env.example
├── .gitignore
└── README.md
```

## 4. SpotAPI dependency

This project uses [`spotapi`](https://github.com/Aran404/SpotAPI)
(the `Aran404/SpotAPI` project, imported as `from spotapi import Song`),
**not** Spotify's official Web API and **not** `spotipy`.

- `Song().query_songs(query, limit=...)` returns Spotify's raw internal
  search response (`data.searchV2.tracksV2.items[...].item.data`).
- `services/spotify.py` walks that structure defensively (every field
  access has a fallback) and converts it into our own stable
  `NormalizedTrack` shape, so the rest of the app never touches SpotAPI's
  raw response directly.
- SpotAPI is **undocumented and reverse-engineered**. It can break or
  change shape without notice, and Spotify can also change how its
  internal API responds. If you start seeing 502 errors from
  `/api/search`, check the [SpotAPI GitHub issues](https://github.com/Aran404/SpotAPI/issues)
  first — this is a known characteristic of any tool built on Spotify's
  private endpoints, not a bug specific to this project.
- No Spotify account, Client ID, Client Secret, or OAuth token is used or
  required anywhere in this codebase.

## 5. YouTube Data API setup

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (or use an existing one).
3. Enable the **YouTube Data API v3** for that project.
4. Create an **API key** (Credentials → Create Credentials → API key).
5. (Recommended) Restrict the key to the YouTube Data API v3 and, if
   possible, to the server IP(s)/domain(s) that will call it.
6. Copy the key into your `.env` file as `YOUTUBE_API_KEY`.

The YouTube Data API has a daily quota; a `search.list` call costs 100
quota units against the default 10,000/day quota, so keep that in mind if
you expect heavy traffic.

## 6. Environment variable setup

Only one environment variable is required:

```
YOUTUBE_API_KEY=your-youtube-api-key-here
```

Copy the example file and fill it in:

```bash
cp .env.example .env
```

No `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, or
`SPOTIFY_ACCESS_TOKEN` variables exist in this project — SpotAPI doesn't
need them.

## 7. Local installation

```bash
git clone <this-repo>
cd music-api
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# then edit .env and set YOUTUBE_API_KEY
```

Requires Python 3.11+.

## 8. Running locally

```bash
uvicorn api.index:app --reload --port 8000
```

The API is now available at `http://localhost:8000`.

## 9. API examples

**Health check**

```bash
curl http://localhost:8000/api/health
```

```json
{ "status": "ok" }
```

**Search**

```bash
curl "http://localhost:8000/api/search?q=Blinding%20Lights"
```

```json
{
  "success": true,
  "query": "Blinding Lights",
  "track": {
    "id": "0VjIjW4GlUZ8o2W7uezw3p",
    "title": "Blinding Lights",
    "artist": "The Weeknd",
    "album": "After Hours",
    "artwork": "https://i.scdn.co/image/ab67616d0000b273...",
    "duration_ms": 200040,
    "spotify_url": "https://open.spotify.com/track/0VjIjW4GlUZ8o2W7uezw3p"
  },
  "youtube": {
    "video_id": "4NRXx6U8ABQ",
    "title": "The Weeknd - Blinding Lights (Official Video)",
    "channel_title": "The Weeknd",
    "thumbnail": "https://i.ytimg.com/vi/4NRXx6U8ABQ/hqdefault.jpg",
    "published_at": "2019-11-29T16:00:07Z",
    "youtube_url": "https://www.youtube.com/watch?v=4NRXx6U8ABQ",
    "match_score": 92.5
  },
  "stream": null
}
```

**Missing query**

```bash
curl "http://localhost:8000/api/search?q="
```

```json
{ "success": false, "error": "Search query is required" }
```
→ HTTP 400

**No Spotify result**

```json
{ "success": false, "error": "Track not found" }
```
→ HTTP 404

**No YouTube result** (Spotify metadata is still returned)

```json
{
  "success": true,
  "query": "...",
  "track": { "...": "..." },
  "youtube": null,
  "stream": null
}
```
→ HTTP 200

**Upstream failure** (SpotAPI or YouTube unreachable/erroring) → HTTP 502

**Unexpected internal error** → HTTP 500, generic message only — no stack
traces, credentials, or internals are ever included in a response.

## 10. Vercel deployment

1. Push this project to a Git repository.
2. Import it into [Vercel](https://vercel.com/new).
3. In the Vercel project's **Settings → Environment Variables**, add:
   - `YOUTUBE_API_KEY` = your YouTube Data API v3 key
4. Deploy.

Once deployed:

```
https://YOUR-DOMAIN.vercel.app/api/health
https://YOUR-DOMAIN.vercel.app/api/search?q=Blinding%20Lights
```

`vercel.json` declares `api/index.py` as an ASGI entrypoint (Vercel's
Python runtime detects the module-level `app` object automatically) and
routes all `/api/*` requests to it — there is no long-running server
process, which keeps this compatible with Vercel's serverless execution
model.

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

## 11. Current extractor limitation

`services/extractor.py` intentionally contains only:

```python
def extract_audio(video_id: str) -> Optional[Any]:
    pass
```

This is **not implemented on purpose**. No YouTube audio-extraction or
downloading library is installed or used anywhere in this project. As a
result, `"stream"` in every `/api/search` response is currently `null`.

The rest of the API is fully functional without it — Spotify metadata,
YouTube matching, and match scoring all work end-to-end. When you have an
audio source you are authorized to stream from, implement the body of
`extract_audio` in `services/extractor.py`; nothing else in the codebase
needs to change, since `api/index.py` already calls this function and
passes its return value straight through as `"stream"` in the response.

## Security notes

- `YOUTUBE_API_KEY` is read only from the environment
  (`os.environ.get("YOUTUBE_API_KEY")`) inside `services/youtube.py`. It is
  never included in JSON responses, logs, or exception messages.
- All unexpected errors are caught and converted into a generic
  `{"success": false, "error": "Internal server error"}` response with an
  HTTP 500 status — internal exception details and stack traces are only
  ever written to the server-side log (`logger.exception(...)`), never
  returned to the client.
