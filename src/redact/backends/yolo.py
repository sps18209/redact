"""Adapter for Ultralytics YOLO — object and open-vocabulary visual redaction.

This is the backend that covers **license plates**, which no other backend here
can reach on a modern install. It runs in two modes:

**Open-vocabulary (default).** A YOLO-World model takes *text prompts* as its
classes, so you can name what to redact in plain language::

    redact run ./footage -b yolo --yolo-classes "license plate,human face,ID card"

Nothing is fine-tuned and no class list constrains you — this is the "semantic"
half of the suite's vision support: describe it, and it is detected and masked.

**Closed-vocabulary.** Any standard checkpoint (``yolo26x.pt``, ``yolo11x.pt``,
``yolov8x.pt``) detects its own trained classes, and requested names are matched
against them::

    redact run ./photos -b yolo --yolo-model yolo26x.pt --yolo-classes person

A COCO-trained checkpoint — which is what every stock YOLO release is, YOLO26
included — has 80 classes and **none of them is a license plate**. Asking a
stock model for plates therefore finds nothing, and this adapter says so rather
than reporting a clean run: unmatched class names are reported as an error
listing what the model actually knows. Use a YOLO-World model (the default) or a
plate-fine-tuned checkpoint instead.

Video masking carries detections through short detector misses (two frames by
default) and re-opens every completed export to verify that it decodes end to
end with the expected frame count. A machine-readable verification sidecar is
written next to the redacted video.

Weights download on first use and are cached by Ultralytics.
Install with ``pip install "redact-suite[yolo]"``.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path
from typing import List, Sequence

from ..continuity import TemporalMaskTracker
from ..document import Document, output_path
from ..media import mux_audio, video_fps
from ..types import (
    Entity,
    MediaType,
    RedactionMode,
    RedactionOptions,
    RedactionResult,
)
from ..verification import (
    verification_sidecar_path,
    verify_video_output,
    write_verification_sidecar,
)
from .base import Backend

#: Default checkpoint: open-vocabulary, so text prompts work out of the box.
DEFAULT_MODEL = "yolov8s-worldv2.pt"

#: Default prompts — the two things a privacy pass almost always wants.
DEFAULT_CLASSES = ("license plate", "human face")

#: Maximum detector-miss run bridged by temporal masking. This is deliberately
#: short: the goal is to close one-frame/two-frame jitter, not invent tracks.
DEFAULT_TEMPORAL_GAP = 2

_BLUR_KERNEL = 31
_MOSAIC_BLOCKS = 12


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def entity_label(class_name: str) -> str:
    """``license plate`` -> ``LICENSE_PLATE`` (a distinct label, never FACE)."""
    return "_".join(class_name.strip().upper().split())


class YoloBackend(Backend):
    name = "yolo"
    description = (
        "Ultralytics YOLO — open-vocabulary visual redaction from text prompts "
        "(license plates, faces, anything you can name)."
    )
    supported_media_types = (MediaType.IMAGE, MediaType.VIDEO)
    # Below deface (70) on purpose: deface is a purpose-built face detector and
    # is better at faces, so `auto` should not silently swap it for a
    # generalist. Select this backend explicitly when you need plates.
    priority = 65

    def missing_dependencies(self) -> List[str]:
        missing = []
        if not _module_present("ultralytics"):
            missing.append("ultralytics")
        if not _module_present("cv2"):
            missing.append("opencv-python")
        return missing

    # -- configuration -------------------------------------------------------
    def _model_name(self, options: RedactionOptions) -> str:
        return (
            options.extra.get("yolo_model")
            or os.environ.get("REDACT_YOLO_MODEL")
            or DEFAULT_MODEL
        )

    def _classes(self, options: RedactionOptions) -> List[str]:
        raw = options.extra.get("yolo_classes")
        if not raw:
            return list(DEFAULT_CLASSES)
        if isinstance(raw, str):
            raw = raw.split(",")
        return [c.strip() for c in raw if c.strip()]

    def _temporal_gap(self, options: RedactionOptions) -> int:
        raw = options.extra.get("temporal_gap", DEFAULT_TEMPORAL_GAP)
        try:
            gap = int(raw)
        except (TypeError, ValueError):
            gap = DEFAULT_TEMPORAL_GAP
        # Keep the escape hatch bounded. Zero explicitly disables propagation.
        return max(0, min(10, gap))

    def redact(self, document: Document, options: RedactionOptions) -> RedactionResult:
        result = RedactionResult(
            source=document.path, backend=self.name, media_type=document.media_type,
        )
        missing = self.missing_dependencies()
        if missing:
            result.success = False
            result.message = f"missing dependencies: {', '.join(missing)}"
            return result

        wanted = self._classes(options)
        if options.entities is not None:
            keep = {e.upper() for e in options.entities}
            wanted = [c for c in wanted if entity_label(c) in keep]
            if not wanted:
                result.message = "skipped: no requested entity type matches this backend"
                return result
        if options.dry_run:
            result.message = f"dry-run: would detect and mask {', '.join(wanted)}"
            return result

        try:
            model, labels = _load_model(self._model_name(options), wanted)
        except ValueError as exc:  # class names the model cannot produce
            result.success = False
            result.message = str(exc)
            return result
        except Exception as exc:
            result.success = False
            result.message = f"could not load YOLO model: {exc}"
            return result

        out = output_path(document, options)
        strategy = _STRATEGY.get(options.mode, "blur")
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            if document.media_type is MediaType.VIDEO:
                # A failed rerun must not leave a previous run's output or audit
                # record behind. Clear both before doing any work so filesystem
                # state cannot contradict a failed RedactionResult.
                out.unlink(missing_ok=True)
                verification_sidecar_path(out).unlink(missing_ok=True)

                found, frames, audio, continuity = _redact_video(
                    document.path,
                    out,
                    model,
                    labels,
                    options.threshold,
                    strategy,
                    max_gap=self._temporal_gap(options),
                )
                verification = verify_video_output(
                    out,
                    expected_frames=frames,
                    continuity=continuity,
                    audio_preserved=audio,
                )
                try:
                    sidecar = write_verification_sidecar(out, verification)
                except Exception as exc:
                    out.unlink(missing_ok=True)
                    result.success = False
                    result.message = f"verification sidecar failed: {exc}"
                    return result
                if not verification["passed"]:
                    out.unlink(missing_ok=True)
                    result.success = False
                    result.message = (
                        "video verification failed: "
                        + "; ".join(verification["errors"])
                        + f"; report: {sidecar}"
                    )
                    return result

                note = "" if audio else "; audio not preserved"
                repairs = continuity["interpolated_masks"] + continuity["propagated_masks"]
                result.message = (
                    f"{found} detection(s) across {frames} frame(s) masked ({strategy})"
                    f" for: {', '.join(wanted)}; verified; "
                    f"{repairs} temporal continuity mask(s); report: {sidecar}{note}"
                )
            else:
                result.entities = _redact_image(
                    document.path, out, model, labels, options.threshold, strategy
                )
                counts = _summarise(result.entities)
                result.message = (
                    f"{counts} masked ({strategy})" if counts
                    else f"nothing detected for: {', '.join(wanted)}"
                )
        except Exception as exc:
            result.success = False
            result.message = f"yolo failed: {exc}"
            return result

        result.output_path = out
        return result


#: Suite mode -> masking strategy.
_STRATEGY = {
    RedactionMode.BLUR: "blur",
    RedactionMode.REPLACE: "blur",
    RedactionMode.MASK: "mosaic",
    RedactionMode.REDACT: "solid",
    RedactionMode.HASH: "solid",
}

# Model construction reads a checkpoint; cache per (name, classes).
_MODEL_CACHE = {}


def weights_dir() -> Path:
    """Where checkpoints are cached (``REDACT_YOLO_WEIGHTS_DIR`` to override)."""
    return Path(
        os.environ.get(
            "REDACT_YOLO_WEIGHTS_DIR", Path.home() / ".cache" / "redact-suite" / "yolo"
        )
    )


def resolve_weights(name: str) -> str:
    """Turn a bare checkpoint name into a cached path.

    Ultralytics downloads a bare name into the *current working directory*, which
    would scatter 100 MB checkpoints wherever the user happened to run ``redact``.
    Passing a full path makes it download there instead, so bare names are
    redirected into a cache directory. Explicit paths are honoured untouched.
    """
    if os.sep in name or (os.altsep and os.altsep in name) or Path(name).is_file():
        return name
    cache = weights_dir()
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return name  # unwritable cache: fall back to Ultralytics' own behaviour
    return str(cache / name)


def _load_model(name: str, wanted: Sequence[str]):
    """Return ``(model, {class_index: entity_label})`` for the requested classes."""
    key = (name, tuple(wanted))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]

    checkpoint = resolve_weights(name)
    is_open_vocab = "world" in name.lower() or "yoloe" in name.lower()
    if is_open_vocab:
        from ultralytics import YOLOWorld

        model = YOLOWorld(checkpoint)
        model.set_classes(list(wanted))  # the prompts become the class list
        labels = {i: entity_label(c) for i, c in enumerate(wanted)}
    else:
        from ultralytics import YOLO

        model = YOLO(checkpoint)
        available = {str(v).lower(): i for i, v in model.names.items()}
        labels, unknown = {}, []
        for c in wanted:
            idx = available.get(c.strip().lower())
            if idx is None:
                unknown.append(c)
            else:
                labels[idx] = entity_label(c)
        if unknown:
            # Do not silently "succeed" having detected nothing.
            raise ValueError(
                f"model '{name}' cannot detect {unknown}; it knows "
                f"{sorted(available)[:12]}{'...' if len(available) > 12 else ''}. "
                "Use an open-vocabulary model (…-worldv2.pt) or a fine-tuned checkpoint."
            )
    _MODEL_CACHE[key] = (model, labels)
    return model, labels


def _detect(model, frame, labels, threshold: float):
    """Run the model on one frame -> ``[(x1, y1, x2, y2, score, label)]``."""
    results = model.predict(frame, conf=threshold, verbose=False)
    out = []
    for res in results:
        boxes = getattr(res, "boxes", None)
        if boxes is None:
            continue
        for box in boxes:
            cls = int(box.cls[0])
            if cls not in labels:
                continue
            x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
            out.append((x1, y1, x2, y2, float(box.conf[0]), labels[cls]))
    return out


def _mask_region(frame, x1: int, y1: int, x2: int, y2: int, strategy: str) -> None:
    """Mask one box in place, clamped to the frame."""
    import cv2

    h, w = frame.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return
    roi = frame[y1:y2, x1:x2]
    if strategy == "solid":
        frame[y1:y2, x1:x2] = 0
    elif strategy == "mosaic":
        bh = max(1, (y2 - y1) // _MOSAIC_BLOCKS)
        bw = max(1, (x2 - x1) // _MOSAIC_BLOCKS)
        small = cv2.resize(roi, (max(1, (x2 - x1) // bw), max(1, (y2 - y1) // bh)),
                           interpolation=cv2.INTER_LINEAR)
        frame[y1:y2, x1:x2] = cv2.resize(small, (x2 - x1, y2 - y1),
                                         interpolation=cv2.INTER_NEAREST)
    else:
        k = _BLUR_KERNEL | 1  # GaussianBlur needs an odd kernel
        frame[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (k, k), 0)


def _apply(frame, detections, strategy: str) -> None:
    for x1, y1, x2, y2, _score, _label in detections:
        _mask_region(frame, int(x1), int(y1), int(x2), int(y2), strategy)


def _redact_image(source: Path, out: Path, model, labels, threshold, strategy) -> List[Entity]:
    import imageio
    import imageio.v2 as iio

    frame = iio.imread(source)
    detections = _detect(model, frame, labels, threshold)
    _apply(frame, detections, strategy)
    imageio.imsave(str(out), frame)
    return [
        Entity(
            entity_type=label, score=score,
            bbox=(int(x1), int(y1), int(x2 - x1), int(y2 - y1)),
        )
        for x1, y1, x2, y2, score, label in detections
    ]


def _redact_video(
    source: Path,
    out: Path,
    model,
    labels,
    threshold,
    strategy,
    *,
    max_gap: int = DEFAULT_TEMPORAL_GAP,
):
    """Mask every frame with bounded continuity.

    Returns ``(raw_detections, frames, audio_preserved, continuity_stats)``.
    Only ``max_gap + 1`` source frames are retained at a time.
    """
    import imageio.v2 as iio

    fps = video_fps(source)
    found = frames = 0
    continuity = TemporalMaskTracker(max_gap=max_gap)
    frame_buffer = {}

    with tempfile.TemporaryDirectory() as tmp:
        silent = Path(tmp) / ("v" + (out.suffix or ".mp4"))
        reader = iio.get_reader(str(source))
        writer = iio.get_writer(str(silent), fps=fps, macro_block_size=1)
        try:
            for frame_index, frame in enumerate(reader):
                raw = _detect(model, frame, labels, threshold)
                found += len(raw)
                frames += 1
                frame_buffer[frame_index] = frame.copy()  # reader buffers may be read-only
                for ready_index, masks in continuity.push(frame_index, raw):
                    ready = frame_buffer.pop(ready_index)
                    _apply(ready, masks, strategy)
                    writer.append_data(ready)

            for ready_index, masks in continuity.flush():
                ready = frame_buffer.pop(ready_index)
                _apply(ready, masks, strategy)
                writer.append_data(ready)
            if frame_buffer:
                raise RuntimeError(
                    f"temporal continuity left {len(frame_buffer)} frame(s) unresolved"
                )
        finally:
            writer.close()
            reader.close()

        # Re-attach the original audio when we can; otherwise keep the silent render.
        audio = mux_audio(silent, source, out)
        if not audio:
            import shutil

            shutil.copy2(silent, out)
    return found, frames, audio, continuity.stats()


def _summarise(entities: Sequence[Entity]) -> str:
    counts = {}
    for e in entities:
        counts[e.entity_type] = counts.get(e.entity_type, 0) + 1
    return ", ".join(f"{n}x {label}" for label, n in sorted(counts.items()))
