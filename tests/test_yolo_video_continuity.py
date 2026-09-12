"""Integration tests for the YOLO video privacy path without model weights/cv2."""

import json

import imageio.v2 as iio
import numpy as np

from redact import RedactionOptions
from redact.backends.yolo import YoloBackend
from redact.document import Document
from redact.types import MediaType


def make_video(path, frames=5):
    writer = iio.get_writer(str(path), fps=5, macro_block_size=1)
    try:
        for i in range(frames):
            writer.append_data(np.full((32, 32, 3), 30 + i, dtype=np.uint8))
    finally:
        writer.close()


def wire_stubs(monkeypatch, detections):
    from redact.backends import yolo as mod

    monkeypatch.setattr(YoloBackend, "missing_dependencies", lambda self: [])
    monkeypatch.setattr(mod, "_load_model", lambda name, wanted: ("model", {0: "FACE"}))
    calls = {"i": 0}

    def detect(*_args, **_kwargs):
        i = calls["i"]
        calls["i"] += 1
        return detections[i]

    monkeypatch.setattr(mod, "_detect", detect)
    # Continuity behavior is tested separately; this integration test exercises
    # buffering, writing, reopening, result semantics and sidecar creation. No
    # OpenCV dependency is needed in the dev environment.
    monkeypatch.setattr(mod, "_apply", lambda frame, masks, strategy: None)
    return mod


def test_video_short_miss_is_accounted_and_export_is_verified(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    make_video(source, 5)
    box = (5.0, 5.0, 15.0, 20.0, 0.9, "FACE")
    mod = wire_stubs(monkeypatch, [[box], [box], [], [box], [box]])

    result = YoloBackend().redact(
        Document(path=source, media_type=MediaType.VIDEO),
        RedactionOptions(output_dir=tmp_path / "out"),
    )

    assert result.success, result.message
    assert result.output_path and result.output_path.exists()
    sidecar = result.output_path.with_name(result.output_path.name + ".verification.json")
    assert sidecar.exists()
    report = json.loads(sidecar.read_text())
    assert report["passed"] is True
    assert report["frames_decoded"] == 5
    assert report["continuity"]["interpolated_masks"] >= 1
    assert "verified" in result.message
    assert mod.DEFAULT_TEMPORAL_GAP == 2


def test_verification_failure_returns_failed_result_and_removes_video(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    make_video(source, 2)
    box = (5.0, 5.0, 15.0, 20.0, 0.9, "FACE")
    mod = wire_stubs(monkeypatch, [[box], [box]])

    monkeypatch.setattr(
        mod,
        "verify_video_output",
        lambda *a, **k: {
            "passed": False,
            "verification_status": "failed",
            "errors": ["synthetic verification failure"],
        },
    )

    result = YoloBackend().redact(
        Document(path=source, media_type=MediaType.VIDEO),
        RedactionOptions(output_dir=tmp_path / "out"),
    )

    assert result.success is False
    assert result.output_path is None
    assert "verification failed" in result.message
    expected = tmp_path / "out" / "clip.redacted.mp4"
    assert not expected.exists()
    # The audit record survives even though the unusable privacy artifact does not.
    sidecar = expected.with_name(expected.name + ".verification.json")
    assert sidecar.exists()


def test_failed_rerun_cannot_leave_previous_output_or_sidecar(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    make_video(source, 1)
    mod = wire_stubs(monkeypatch, [[]])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    expected = out_dir / "clip.redacted.mp4"
    sidecar = expected.with_name(expected.name + ".verification.json")
    expected.write_bytes(b"stale video")
    sidecar.write_text('{"passed": true, "stale": true}')

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(mod, "_redact_video", boom)
    result = YoloBackend().redact(
        Document(path=source, media_type=MediaType.VIDEO),
        RedactionOptions(output_dir=out_dir),
    )

    assert result.success is False
    assert "synthetic render failure" in result.message
    assert not expected.exists()
    assert not sidecar.exists()
