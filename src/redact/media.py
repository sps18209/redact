"""Shared media helpers (ffmpeg discovery, audio muxing).

Kept out of any single backend so the video paths in ``anonymizer`` and ``yolo``
resolve ffmpeg the same way.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


#: Memoised answer from :func:`ffmpeg_bin`; probing spawns a subprocess and the
#: video paths ask repeatedly. ``False`` means "probed, nothing usable".
_FFMPEG: "Optional[str] | bool" = False


def _runs(candidate: str) -> bool:
    """True only if the binary actually executes.

    Presence on PATH is not usability. A system ffmpeg whose shared libraries
    have moved out from under it — a package manager upgrading x265 while ffmpeg
    is still linked against the old soname is the common way — is still found by
    ``which`` and still fails on every invocation.
    """
    try:
        completed = subprocess.run(
            [candidate, "-version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def ffmpeg_bin(refresh: bool = False) -> Optional[str]:
    """A usable ffmpeg: the system one if it runs, else imageio's static build.

    The system binary is *probed*, not merely located. Returning a broken one
    would skip the bundled fallback that exists for exactly this case, and turn
    a recoverable situation into a cryptic linker error from the middle of a
    video redaction.
    """
    global _FFMPEG
    if _FFMPEG is not False and not refresh:
        return _FFMPEG

    found = shutil.which("ffmpeg")
    if found and _runs(found):
        _FFMPEG = found
        return _FFMPEG
    try:
        import imageio_ffmpeg

        static = imageio_ffmpeg.get_ffmpeg_exe()
        _FFMPEG = static if static and _runs(static) else None
    except Exception:
        _FFMPEG = None
    return _FFMPEG


def video_fps(path: Path, default: float = 30.0) -> float:
    """Frame rate of a video, falling back to ``default``."""
    try:
        import imageio.v2 as iio

        with iio.get_reader(str(path)) as reader:
            fps = reader.get_meta_data().get("fps")
        return float(fps) if fps else default
    except Exception:
        return default


def mux_audio(video_only: Path, original: Path, out: Path) -> bool:
    """Copy ``original``'s audio onto ``video_only``, writing ``out``.

    Returns False (leaving ``out`` unwritten) when ffmpeg is unavailable or the
    source has no audio track, so callers can fall back to the silent render
    rather than losing the result.
    """
    ffmpeg = ffmpeg_bin()
    if ffmpeg is None:
        return False
    try:
        subprocess.run(
            [
                ffmpeg, "-y", "-v", "error",
                "-i", str(video_only), "-i", str(original),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "copy", "-shortest", str(out),
            ],
            check=True, capture_output=True, text=True,
        )
        return out.is_file() and out.stat().st_size > 0
    except subprocess.CalledProcessError:
        return False
