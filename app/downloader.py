"""Fetches audio with yt-dlp and converts it to MP3."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import yt_dlp

from .config import settings

log = logging.getLogger("download_center.downloader")

ProgressHook = Callable[[float], None]


class DownloadError(Exception):
    pass


class _QuietLogger:
    """yt-dlp is chatty on stdout; route it into our logger instead."""

    def debug(self, message: str) -> None:
        if not message.startswith("[debug] "):
            log.debug(message)

    def info(self, message: str) -> None:
        log.debug(message)

    def warning(self, message: str) -> None:
        log.debug(message)

    def error(self, message: str) -> None:
        log.warning(message)


def _make_hook(on_progress: ProgressHook | None):
    def hook(status: dict[str, Any]) -> None:
        if not on_progress or status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        done = status.get("downloaded_bytes") or 0
        if total:
            on_progress(min(1.0, done / total))

    return hook


def download(url: str, destination: Path, on_progress: ProgressHook | None = None) -> Path:
    """Download `url` and leave an MP3 at `destination`.

    yt-dlp appends the extension itself after the audio is extracted, so the
    output template is given without one.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    options: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(destination.with_suffix("")) + ".%(ext)s",
        "logger": _QuietLogger(),
        "progress_hooks": [_make_hook(on_progress)],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # Keep the file's mtime as "now" rather than the upload date, so the
        # staging directory sorts sensibly and beets sees fresh files.
        "updatetime": False,
        "retries": 3,
        "fragment_retries": 3,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": settings.audio_bitrate,
            }
        ],
    }

    cookies = settings.cookies_file
    if cookies:
        options["cookiefile"] = str(cookies)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(str(exc).replace("\n", " ")[:300]) from exc
    except Exception as exc:
        raise DownloadError(f"{type(exc).__name__}: {exc}"[:300]) from exc

    if not destination.exists():
        # Extraction can land on a different extension if ffmpeg declined the
        # conversion; surface that rather than reporting a phantom success.
        # Matched by prefix rather than by glob: a title like "Song [Remix]"
        # is a character class to glob, so the diagnostic that exists to say
        # what went wrong reported an empty list.
        prefix = destination.stem + "."
        siblings = sorted(p.name for p in destination.parent.iterdir()
                          if p.name.startswith(prefix))
        raise DownloadError(
            f"Expected {destination.name} but found {siblings}"
        )

    return destination
