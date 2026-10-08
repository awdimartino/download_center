"""Fetches audio with yt-dlp and converts it to MP3."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any
from collections.abc import Callable

import yt_dlp
from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP

from .config import settings

log = logging.getLogger("navidrome_companion.downloader")

ProgressHook = Callable[[float], None]


class DownloadError(Exception):
    pass


class Cancelled(Exception):
    """The download was asked to stop and did. Never retried."""


# Warnings already logged, so a notice yt-dlp gives for every download is
# written once rather than once per track. Bounded: a restart clears it.
_warned: set[str] = set()
_WARNED_MAX = 500


class _QuietLogger:
    """yt-dlp is chatty on stdout; route it into our logger instead."""

    def debug(self, message: str) -> None:
        if not message.startswith("[debug] "):
            log.debug(message)

    def info(self, message: str) -> None:
        log.debug(message)

    def warning(self, message: str) -> None:
        # Kept, once each. They went to debug, which is off, so yt-dlp saying
        # that YouTube extraction was degraded - the warning that comes
        # before every download starts failing - never reached the log.
        if message in _warned:
            return
        if len(_warned) < _WARNED_MAX:
            _warned.add(message)
        log.warning("yt-dlp: %s", message)

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


# The MP3 bitrate for a source that is no better than it. YouTube's usual
# audio is Opus at 130-160 kbps; re-encoding that at 320 made files twice the
# size with nothing more in them, while 192 MP3 keeps everything a source
# that size had. A better source - 256 kbps AAC, a lossless upload - still
# gets the configured bitrate.
MATCHED_BITRATE = 192


def bitrate_for(source_kbps: float | None, configured: str) -> str:
    """The bitrate to encode at, given what the download actually was.

    A VBR level (0-9) is left alone: it already scales with the audio. So is
    a source whose bitrate yt-dlp did not report - better too big than
    quietly worse.
    """
    try:
        wanted = int(configured)
    except ValueError:
        return configured
    if wanted <= 9 or not source_kbps or source_kbps > MATCHED_BITRATE:
        return configured
    return str(min(wanted, MATCHED_BITRATE))


def _source_kbps(info: dict[str, Any]) -> float | None:
    """The audio bitrate of what was downloaded, as yt-dlp reported it."""
    for one in (info, *(info.get("requested_formats") or [])):
        for key in ("abr", "tbr"):
            value = one.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    return None


class _MatchedQuality(FFmpegExtractAudioPP):
    """yt-dlp's MP3 extraction, at a bitrate chosen once the source is known.

    The quality is otherwise fixed when the downloader is built, before
    yt-dlp has picked a format - so it could not depend on that format.
    """

    @classmethod
    def pp_key(cls) -> str:
        # yt-dlp's own name, so its logs and per-step arguments still apply.
        return "ExtractAudio"

    def run(self, information):
        chosen = bitrate_for(_source_kbps(information), settings.audio_bitrate)
        self._preferredquality = float(chosen)
        log.debug("encoding %s at %s (source %s kbps)", information.get("id"),
                  chosen, _source_kbps(information))
        return super().run(information)


def download(url: str, destination: Path, on_progress: ProgressHook | None = None,
             stop: threading.Event | None = None) -> Path:
    """Download `url` and leave an MP3 at `destination`.

    yt-dlp appends the extension itself after the audio is extracted, so the
    output template is given without one.

    `stop` ends it early: checked on every chunk and at each post-processing
    step, the only places yt-dlp lets a caller in. Cancelling the job's task
    used to end only the wait for this thread, which went on downloading
    into a scratch folder already deleted, past the concurrency limit.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    def check(_status: dict[str, Any]) -> None:
        if stop is not None and stop.is_set():
            raise yt_dlp.utils.DownloadCancelled("stopped")

    options: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(destination.with_suffix("")) + ".%(ext)s",
        "logger": _QuietLogger(),
        "progress_hooks": [check, _make_hook(on_progress)],
        "postprocessor_hooks": [check],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # Keep the file's mtime as "now" rather than the upload date. A
        # rename keeps it, and the inbox poller leaves a file alone until it
        # has been quiet for a while - an upload date years old would hand a
        # delivered file to the poller at once (see `inbox.deliver`).
        "updatetime": False,
        "retries": 3,
        "fragment_retries": 3,
    }

    cookies = settings.cookies_file
    if cookies:
        options["cookiefile"] = str(cookies)

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.add_post_processor(
                _MatchedQuality(ydl, preferredcodec="mp3",
                                preferredquality=settings.audio_bitrate),
                when="post_process")
            ydl.download([url])
    except yt_dlp.utils.DownloadCancelled as exc:
        raise Cancelled() from exc
    except yt_dlp.utils.DownloadError as exc:
        if stop is not None and stop.is_set():
            raise Cancelled() from exc
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
