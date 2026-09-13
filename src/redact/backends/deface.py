"""Adapter for deface — maintained, offline face blurring for images and video.

This is the practical replacement for understand.ai's Anonymizer, which pins
``tensorflow-gpu==1.11.0`` (Python 3.6 and older) and has been unmaintained
since 2019, so it cannot be installed on any current interpreter.

deface is a single ``pip install`` away, ships its CenterFace ONNX model inside
the wheel (so detection is fully offline with no first-run download), and pulls
``imageio-ffmpeg``, which bundles a static ffmpeg binary — so video works
without a system ffmpeg too.

    pip install "redact-suite[deface]"

**Scope:** deface detects *faces only*. It does not blur license plates. For
plates you still need the Anonymizer backend (see ``anonymizer.py``) or another
detector; the suite does not silently pretend plates are covered.

The library is driven through its Python API rather than its console script:
``python -m deface`` does not work (the package has no ``__main__``), so relying
on the ``deface`` executable being on ``PATH`` would be fragile inside venvs.
Going through the API also yields per-face bounding boxes, which the CLI does
not report.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import List

from ..document import Document, output_path
from ..types import (
    Entity,
    MediaType,
    RedactionMode,
    RedactionOptions,
    RedactionResult,
)
from .base import Backend

#: Entity label reported for a detected face.
FACE_ENTITY = "FACE"

#: How much to grow each detection box before masking (deface's own default).
_MASK_SCALE = 1.3
_MOSAIC_SIZE = 20

#: How each masking strategy reads in a result message.
_VERB = {"blur": "blurred", "mosaic": "pixelated", "solid": "blacked out"}


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class DefaceBackend(Backend):
    name = "deface"
    description = "deface — offline CNN face blurring for images & video (bundled model; faces only, not plates)."
    supported_media_types = (MediaType.IMAGE, MediaType.VIDEO)
    install_hint = 'pip install "redact-suite[deface]"'
    priority = 70  # above anonymizer: this one actually installs

    #: Suite mode -> deface's ``replacewith`` strategy.
    _REPLACEWITH = {
        RedactionMode.BLUR: "blur",
        RedactionMode.REPLACE: "blur",   # the sensible visual default
        RedactionMode.MASK: "mosaic",
        RedactionMode.REDACT: "solid",
        RedactionMode.HASH: "solid",
    }

    def missing_dependencies(self) -> List[str]:
        missing = []
        if not _module_present("deface"):
            missing.append("deface")
        if not _module_present("imageio"):
            missing.append("imageio")
        return missing

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        result = RedactionResult(
            source=document.path, backend=self.name, media_type=document.media_type,
        )
        missing = self.missing_dependencies()
        if missing:
            result.success = False
            result.message = f"missing dependencies: {', '.join(missing)}"
            return result
        if options.entities is not None and FACE_ENTITY not in options.entities:
            result.message = f"skipped: {FACE_ENTITY} not in the requested entity types"
            return result
        if options.dry_run:
            result.message = "dry-run: would blur faces (detection needs a full pass)"
            return result

        replacewith = self._REPLACEWITH.get(options.mode, "blur")
        out = output_path(document, options)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            verb = _VERB.get(replacewith, replacewith)
            if document.media_type is MediaType.VIDEO:
                # Per-face entities would mean one row per face per frame, so the
                # video path reports totals in the message instead.
                found, frames = _blur_video(document.path, out, options.threshold, replacewith)
                result.message = f"{found} face detection(s) across {frames} frame(s) {verb}"
            else:
                result.entities = _blur_image(document.path, out, options.threshold, replacewith)
                result.message = f"{len(result.entities)} face(s) {verb}"
        except Exception as exc:  # model/IO/codec failures must not abort a batch
            result.success = False
            result.message = f"deface failed: {exc}"
            return result

        result.output_path = out
        return result


# -- model ------------------------------------------------------------------
# Loading CenterFace reads and initialises the ONNX graph; cache one per process
# so a batch does not pay for it per file.
_MODEL_CACHE = {}


def _centerface():
    from deface.centerface import CenterFace  # lazy: pulls opencv/onnx

    if "instance" not in _MODEL_CACHE:
        _MODEL_CACHE["instance"] = CenterFace(in_shape=None, backend="auto")
    return _MODEL_CACHE["instance"]


class _CountingCenterFace:
    """Wraps CenterFace to tally detections while ``video_detect`` runs.

    deface's video path reports nothing about what it found, so the only way to
    surface real numbers is to observe the detector as it is called per frame.
    """

    def __init__(self, inner):
        self._inner = inner
        self.detections = 0
        self.frames = 0

    def __call__(self, frame, threshold):
        dets, lms = self._inner(frame, threshold=threshold)
        self.detections += len(dets)
        self.frames += 1
        return dets, lms


# -- images -----------------------------------------------------------------

def _blur_image(source: Path, out: Path, threshold: float, replacewith: str) -> List[Entity]:
    import imageio
    import imageio.v2 as iio
    from deface.deface import anonymize_frame

    frame = iio.imread(source)
    dets, _ = _centerface()(frame, threshold=threshold)
    # anonymize_frame edits the array in place.
    anonymize_frame(
        dets, frame,
        mask_scale=_MASK_SCALE, replacewith=replacewith, ellipse=True,
        draw_scores=False, replaceimg=None, mosaicsize=_MOSAIC_SIZE,
    )
    imageio.imsave(str(out), frame)
    return [_entity(det) for det in dets]


def _entity(det) -> Entity:
    """One CenterFace row ``[x1, y1, x2, y2, score]`` -> an Entity with a bbox."""
    x1, y1, x2, y2, score = (float(v) for v in det[:5])
    return Entity(
        entity_type=FACE_ENTITY,
        score=score,
        bbox=(int(x1), int(y1), int(x2 - x1), int(y2 - y1)),
    )


# -- video ------------------------------------------------------------------

def _blur_video(source: Path, out: Path, threshold: float, replacewith: str):
    from deface.deface import video_detect

    counter = _CountingCenterFace(_centerface())
    video_detect(
        ipath=str(source), opath=str(out), centerface=counter, threshold=threshold,
        enable_preview=False, cam=False, nested=False, replacewith=replacewith,
        mask_scale=_MASK_SCALE, ellipse=True, draw_scores=False,
        ffmpeg_config={"codec": "libx264"}, replaceimg=None,
        keep_audio=True,  # the original audio track is preserved
        mosaicsize=_MOSAIC_SIZE,
    )
    return counter.detections, counter.frames
