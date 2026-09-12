"""Adapter for understand.ai's Anonymizer (faces & license plates).

Anonymizer uses local deep-learning models to detect and blur faces and license
plates. Upstream ships as a *git checkout*, not a PyPI package — the PyPI
project named ``anonymizer`` is an unrelated k-anonymisation library — so this
adapter never imports it. It drives the upstream CLI instead:

    git clone https://github.com/understand-ai/anonymizer
    pip install -r anonymizer/requirements.txt
    export ANONYMIZER_HOME=/path/to/anonymizer      # the checkout root

Upstream's entry point is ``anonymizer/bin/anonymize.py`` taking
``--input``/``--image-output``/``--weights`` (weights download on first run).
Any executable with the same interface can be used instead via
``ANONYMIZER_BIN``. Weights live in ``ANONYMIZER_WEIGHTS`` (default:
``~/.cache/redact-suite/anonymizer-weights``).

Images are processed directly. Videos are handled by extracting frames with
ffmpeg, anonymizing every frame, and re-encoding with the source's audio. The
ffmpeg binary is taken from ``PATH`` or, failing that, from ``imageio-ffmpeg``
(which ships a static build), so a system ffmpeg is not required.

Because upstream is unmaintained and pins ``tensorflow-gpu==1.11.0`` (Python 3.6
and older), this backend is effectively unreachable on a current interpreter.
It is kept because it is the only option here that blurs **license plates** as
well as faces; for faces alone prefer the ``deface`` backend, which installs
with one pip command.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from ..document import Document, output_path
from ..media import ffmpeg_bin, video_fps
from ..types import MediaType, RedactionOptions, RedactionResult
from .base import Backend

_IMAGE_EXTENSIONS = "jpg,jpeg,png"


def _script() -> Optional[Path]:
    home = os.environ.get("ANONYMIZER_HOME")
    if not home:
        return None
    script = Path(home) / "anonymizer" / "bin" / "anonymize.py"
    return script if script.is_file() else None


def _base_command() -> Optional[List[str]]:
    """The command prefix that invokes the anonymizer, or None if absent."""
    script = _script()
    if script is not None:
        return [sys.executable, str(script)]
    exe = os.environ.get("ANONYMIZER_BIN")
    resolved = shutil.which(exe) if exe else None
    return [resolved] if resolved else None


def _weights_dir() -> str:
    return os.environ.get(
        "ANONYMIZER_WEIGHTS",
        str(Path.home() / ".cache" / "redact-suite" / "anonymizer-weights"),
    )


def _run_anonymizer(in_dir: Path, out_dir: Path) -> None:
    """Anonymize every supported image in ``in_dir`` into ``out_dir``."""
    cmd = _base_command() + [
        "--input", str(in_dir),
        "--image-output", str(out_dir),
        "--weights", _weights_dir(),
        "--image-extensions", _IMAGE_EXTENSIONS,
    ]
    env = os.environ.copy()
    home = os.environ.get("ANONYMIZER_HOME")
    if home:  # upstream expects its checkout root on PYTHONPATH
        env["PYTHONPATH"] = home + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)


def _ffmpeg_missing() -> List[str]:
    return [] if ffmpeg_bin() else ["ffmpeg (install it, or pip install imageio-ffmpeg)"]


def _frame_rate(video: Path) -> str:
    """Source frame rate, via ffprobe when present, else imageio, else 30."""
    if shutil.which("ffprobe"):
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(video),
            ],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if out:
            return out
    return str(video_fps(video))


def _anonymize_image(source: Path, out: Path) -> None:
    # Upstream writes outputs under the input's file name, so stage the single
    # file in a scratch tree to guarantee the original is never overwritten.
    with tempfile.TemporaryDirectory() as tmp:
        in_dir, out_dir = Path(tmp) / "in", Path(tmp) / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        shutil.copy2(source, in_dir / source.name)
        _run_anonymizer(in_dir, out_dir)
        produced = out_dir / source.name
        if not produced.is_file():
            raise OSError(f"anonymizer produced no output for {source.name}")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(out))


def _anonymize_video(source: Path, out: Path) -> None:
    fps = _frame_rate(source)
    with tempfile.TemporaryDirectory() as tmp:
        frames, blurred = Path(tmp) / "frames", Path(tmp) / "blurred"
        frames.mkdir()
        blurred.mkdir()
        ffmpeg = ffmpeg_bin()
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(source), str(frames / "%06d.png")],
            check=True, capture_output=True, text=True,
        )
        _run_anonymizer(frames, blurred)
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg, "-y", "-v", "error",
                "-framerate", fps, "-i", str(blurred / "%06d.png"),
                "-i", str(source),
                "-map", "0:v:0", "-map", "1:a?",      # blurred video + original audio
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy",
                "-shortest", str(out),
            ],
            check=True, capture_output=True, text=True,
        )


class AnonymizerBackend(Backend):
    name = "anonymizer"
    description = (
        "understand.ai Anonymizer — faces AND license plates (legacy: needs TF 1.x, "
        "Python<=3.6; prefer 'deface' for faces)."
    )
    supported_media_types = (MediaType.IMAGE, MediaType.VIDEO)
    priority = 60  # below deface: this upstream no longer installs anywhere modern

    def missing_dependencies(self) -> List[str]:
        if _base_command() is None:
            return [
                "understand-ai anonymizer "
                "(set ANONYMIZER_HOME to a checkout, or ANONYMIZER_BIN to a CLI)"
            ]
        return []

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        result = RedactionResult(
            source=document.path, backend=self.name, media_type=document.media_type,
        )
        if self.missing_dependencies():
            result.success = False
            result.message = "anonymizer not found (set ANONYMIZER_HOME or ANONYMIZER_BIN)"
            return result
        if document.media_type is MediaType.VIDEO and _ffmpeg_missing():
            result.success = False
            result.message = f"video needs {', '.join(_ffmpeg_missing())} on PATH"
            return result
        if options.dry_run:
            result.message = "dry-run: would blur faces/plates (no detection counts available pre-run)"
            return result

        out = output_path(document, options)
        try:
            if document.media_type is MediaType.VIDEO:
                _anonymize_video(document.path, out)
            else:
                _anonymize_image(document.path, out)
        except subprocess.CalledProcessError as exc:
            result.success = False
            result.message = f"anonymizer failed: {(exc.stderr or '').strip() or exc}"
            return result
        except OSError as exc:
            result.success = False
            result.message = f"could not write output: {exc}"
            return result

        result.output_path = out
        result.message = "faces & license plates blurred"
        return result
