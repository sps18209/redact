"""Shared media helpers (ffmpeg discovery, audio muxing).

Kept out of any single backend so the video paths in ``anonymizer`` and ``yolo``
resolve ffmpeg the same way.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def ffmpeg_bin() -> Optional[str]:
    """A usable ffmpeg: the system one, else the static build imageio ships."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


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
