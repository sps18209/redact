"""ffmpeg discovery must return a *usable* binary, not merely a present one.

Reported from a real machine: Homebrew upgraded x265 from 4.2 to 4.3 while the
installed ffmpeg was still linked against the 4.2 soname. `which ffmpeg` kept
returning a path, every invocation died with a linker error, and the suite's
bundled static fallback — which exists for exactly this — was never reached,
because the code stopped at "found".
"""

import pytest

from redact import media


@pytest.fixture(autouse=True)
def _clear_cache():
    media._FFMPEG = False
    yield
    media._FFMPEG = False


def test_a_present_but_broken_system_ffmpeg_falls_through(monkeypatch):
    """The bug: a broken binary was returned and the fallback skipped."""
    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media, "_runs", lambda c: c != "/usr/bin/ffmpeg")

    resolved = media.ffmpeg_bin()
    assert resolved != "/usr/bin/ffmpeg", "a binary that cannot run is not a usable ffmpeg"


def test_a_working_system_ffmpeg_is_preferred(monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media, "_runs", lambda c: True)
    assert media.ffmpeg_bin() == "/usr/bin/ffmpeg"


def test_none_when_nothing_usable(monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    monkeypatch.setattr(media, "_runs", lambda c: False)
    assert media.ffmpeg_bin() is None


def test_runs_rejects_a_binary_that_exits_nonzero():
    assert media._runs("/bin/false") is False
    assert media._runs("/nonexistent/ffmpeg") is False


def test_the_probe_is_cached(monkeypatch):
    """Video paths ask repeatedly; probing spawns a subprocess each time."""
    calls = []
    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media, "_runs", lambda c: calls.append(c) or True)
    media.ffmpeg_bin()
    media.ffmpeg_bin()
    media.ffmpeg_bin()
    assert len(calls) == 1


def test_refresh_reprobes(monkeypatch):
    calls = []
    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media, "_runs", lambda c: calls.append(c) or True)
    media.ffmpeg_bin()
    media.ffmpeg_bin(refresh=True)
    assert len(calls) == 2
