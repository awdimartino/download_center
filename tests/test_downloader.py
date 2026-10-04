"""The MP3 bitrate follows the source (CODE_REVIEW L19).

Every download was re-encoded at 320 kbps, though YouTube's usual audio is
Opus at 130-160: twice the size with nothing more in it. A source no better
than 192 is now encoded at 192; a better or unknown one keeps the setting.
"""

from __future__ import annotations

import pytest

from app import downloader


@pytest.mark.parametrize("source, configured, chosen", [
    (129.5, "320", "192"),     # YouTube's Opus
    (160, "320", "192"),
    (192, "320", "192"),
    (256, "320", "320"),       # YouTube Music's AAC for Premium, SoundCloud Go
    (None, "320", "320"),      # not reported: better too big than worse
    (129.5, "128", "128"),     # never above what was asked for
    (129.5, "2", "2"),         # a VBR level scales by itself
])
def test_the_bitrate_follows_the_source(source, configured, chosen):
    assert downloader.bitrate_for(source, configured) == chosen


def test_the_source_bitrate_is_read_from_what_was_downloaded():
    assert downloader._source_kbps({"abr": 129.6}) == 129.6
    assert downloader._source_kbps({"tbr": 140}) == 140
    assert downloader._source_kbps(
        {"requested_formats": [{"vcodec": "avc1"}, {"abr": 160}]}) == 160
    assert downloader._source_kbps({}) is None


def test_the_extraction_sets_its_bitrate_once_it_sees_the_source(monkeypatch):
    seen = []

    def run(self, information):
        seen.append(self._preferredquality)
        return [], information

    monkeypatch.setattr(downloader.FFmpegExtractAudioPP, "run", run)
    monkeypatch.setattr(downloader.settings, "audio_bitrate", "320")
    step = downloader._MatchedQuality(None, preferredcodec="mp3",
                                      preferredquality="320")

    step.run({"id": "a", "abr": 129.6, "filepath": "a.webm", "ext": "webm"})
    step.run({"id": "b", "abr": 256, "filepath": "b.m4a", "ext": "m4a"})

    assert seen == [192.0, 320.0]
