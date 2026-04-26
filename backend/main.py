"""FastAPI backend for the YouTube downloader site.

Exposes two endpoints:

    GET /api/info?url=<youtube-url>
        Returns metadata (title, thumbnail, duration, channel, available
        qualities) for a YouTube video or short.

    GET /api/download?url=<youtube-url>&format=<video|audio>&quality=<label>
        Streams the merged/processed media file back to the client as an
        attachment. The browser on mobile will save it to the device's
        downloads/gallery folder.

The service deliberately runs yt-dlp in a worker thread so the FastAPI event
loop stays responsive under concurrent requests.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


def _ffmpeg_location() -> str | None:
    """Return a path to an ffmpeg binary if one is available.

    Falls back to the static binary shipped with ``imageio-ffmpeg`` so the
    service works in minimal containers that don't have ``ffmpeg`` on PATH.
    """
    if shutil.which("ffmpeg"):
        return None  # yt-dlp will discover it on PATH
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - best effort
        return None


FFMPEG_LOCATION = _ffmpeg_location()

# YouTube increasingly requires a "Sign in to confirm you're not a bot" check
# on requests from datacenter IPs (e.g. Fly.io). Different yt-dlp player
# clients have different triggers for this gate: the iOS / Android / mweb /
# tv clients often succeed where the default "web" client is blocked. We ask
# yt-dlp to try them in order and fall back on failure.
YT_PLAYER_CLIENTS = ["ios", "mweb", "android", "tv", "web_safari", "web"]
YT_EXTRACTOR_ARGS = {"youtube": {"player_client": YT_PLAYER_CLIENTS}}

# Messages yt-dlp emits when the anti-bot gate is active. Used to decide
# whether to retry with a different player client.
_BOT_CHECK_MARKERS = (
    "confirm you're not a bot",
    "confirm you are not a bot",
    "Sign in to confirm",
    "requires login",
)


def _is_bot_check_error(exc: Exception) -> bool:
    message = str(exc)
    return any(marker.lower() in message.lower() for marker in _BOT_CHECK_MARKERS)


def _run_with_client_fallback(runner):
    """Call ``runner(opts)`` trying each player client until one succeeds.

    ``runner`` takes a partial ydl options dict (just the extractor_args
    override) and returns whatever it wants. If a call raises a
    ``DownloadError`` that looks like the bot-check gate, we retry with the
    next client. Any other error is re-raised immediately.
    """
    last_exc: Exception | None = None
    for client in YT_PLAYER_CLIENTS:
        try:
            return runner({"youtube": {"player_client": [client]}})
        except DownloadError as exc:
            last_exc = exc
            if _is_bot_check_error(exc):
                continue
            raise
    assert last_exc is not None
    raise last_exc


app = FastAPI(title="YT Gallery Downloader", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


class VideoInfo(BaseModel):
    id: str
    title: str
    channel: str | None
    duration: int | None
    thumbnail: str | None
    is_short: bool
    qualities: list[str]
    webpage_url: str


def _validate_url(url: str) -> None:
    try:
        parsed = urlparse(url)
    except ValueError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=400, detail="Invalid URL") from exc
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="URL must be http(s)")
    if parsed.hostname is None or parsed.hostname.lower() not in ALLOWED_HOSTS:
        raise HTTPException(
            status_code=400,
            detail="Only YouTube URLs are supported.",
        )


def _unique_heights(formats: Iterable[dict[str, Any]]) -> list[str]:
    heights: set[int] = set()
    for fmt in formats:
        height = fmt.get("height")
        vcodec = fmt.get("vcodec")
        if height and vcodec and vcodec != "none":
            heights.add(int(height))
    ordered = sorted(heights, reverse=True)
    return [f"{h}p" for h in ordered]


def _extract_info(url: str) -> dict[str, Any]:
    base_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    if FFMPEG_LOCATION:
        base_opts["ffmpeg_location"] = FFMPEG_LOCATION

    def _run(extractor_args: dict[str, Any]) -> dict[str, Any] | None:
        opts = {**base_opts, "extractor_args": extractor_args}
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = _run_with_client_fallback(_run)
    except DownloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if info is None:
        raise HTTPException(status_code=404, detail="Video not found")
    if "entries" in info:  # playlist fallback — take first item
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise HTTPException(status_code=404, detail="Empty playlist")
        info = entries[0]
    return info


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/info", response_model=VideoInfo)
async def get_info(url: str = Query(..., description="YouTube video URL")) -> VideoInfo:
    _validate_url(url)
    info = await asyncio.to_thread(_extract_info, url)
    is_short = "/shorts/" in (info.get("webpage_url") or url)
    return VideoInfo(
        id=info.get("id", ""),
        title=info.get("title", "Untitled"),
        channel=info.get("uploader") or info.get("channel"),
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        is_short=is_short,
        qualities=_unique_heights(info.get("formats") or []),
        webpage_url=info.get("webpage_url") or url,
    )


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", name).strip()
    return cleaned or "video"


def _download(url: str, fmt: str, quality: str | None, workdir: Path) -> Path:
    outtmpl = str(workdir / "%(title).200B-%(id)s.%(ext)s")
    if fmt == "audio":
        ydl_opts: dict[str, Any] = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    else:
        if quality and quality.endswith("p") and quality[:-1].isdigit():
            height = int(quality[:-1])
            fmt_selector = (
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"best[height<={height}][ext=mp4]/"
                f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
            )
        else:
            fmt_selector = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        ydl_opts = {
            "format": fmt_selector,
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
        }

    if FFMPEG_LOCATION:
        ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION

    def _run(extractor_args: dict[str, Any]) -> tuple[dict[str, Any], str]:
        opts = {**ydl_opts, "extractor_args": extractor_args}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return info, ydl.prepare_filename(info)

    try:
        info, filename = _run_with_client_fallback(_run)
    except DownloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    path = Path(filename)
    if fmt == "audio":
        path = path.with_suffix(".mp3")
    elif not path.exists():
        # yt-dlp may rewrite the extension after merging
        mp4 = path.with_suffix(".mp4")
        if mp4.exists():
            path = mp4
    if not path.exists():
        raise HTTPException(status_code=500, detail="Download failed to produce a file")
    return path


@app.get("/api/download")
async def download(
    background_tasks: BackgroundTasks,
    url: str = Query(...),
    format: str = Query("video", pattern="^(video|audio)$"),
    quality: str | None = Query(None),
) -> FileResponse:
    _validate_url(url)
    workdir = Path(tempfile.mkdtemp(prefix=f"ytdl-{uuid.uuid4().hex}-"))
    try:
        path = await asyncio.to_thread(_download, url, format, quality, workdir)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    download_name = _safe_filename(path.name)
    media_type = "audio/mpeg" if format == "audio" else "video/mp4"

    def _cleanup() -> None:
        shutil.rmtree(workdir, ignore_errors=True)

    background_tasks.add_task(_cleanup)

    return FileResponse(
        path,
        media_type=media_type,
        filename=download_name,
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.get("/")
def root() -> JSONResponse:
    return JSONResponse(
        {
            "name": "YT Gallery Downloader API",
            "endpoints": ["/api/info", "/api/download", "/healthz"],
        }
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
