# YT Gallery

A small website for downloading YouTube videos and Shorts to your device /
phone gallery.

- **Backend:** FastAPI + [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) +
  `ffmpeg` (for merging streams and audio extraction).
- **Frontend:** Plain HTML / CSS / JS — paste a URL, pick a quality, tap
  **Download**. The browser saves the file straight into your
  Downloads / Gallery folder.

> Only download videos you own or have permission to download. Using this to
> bypass copyright or YouTube's Terms of Service is not endorsed.

## Repo layout

```
backend/    FastAPI service that wraps yt-dlp
frontend/   Static site (index.html, styles.css, app.js)
```

## Run locally

Backend (requires `ffmpeg` installed on the system):

```bash
cd backend
pip install -e .
uvicorn main:app --reload --port 8000
```

Frontend:

```bash
cd frontend
python -m http.server 5173
# open http://localhost:5173
```

The frontend auto-targets `http://localhost:8000` when served from
`localhost`. In production, deploy the backend somewhere public and set
`window.API_BASE` in `index.html`, or open the site with `?api=<backend-url>`.

## Endpoints

- `GET /api/info?url=<yt-url>` — returns title, thumbnail, duration, channel,
  and available quality labels (e.g. `1080p`, `720p`, ...).
- `GET /api/download?url=<yt-url>&format=video|audio&quality=<label>` —
  streams the merged MP4 (or extracted MP3) back as a file attachment.
- `GET /healthz` — liveness probe.
