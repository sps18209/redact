"""Video reopen/verification and audit-sidecar tests."""

import json

import imageio.v2 as iio
import numpy as np

from redact.document import is_redaction_output
from redact.verification import (
    verification_sidecar_path,
    verify_video_output,
    write_verification_sidecar,
)


def make_video(path, frames=3):
    writer = iio.get_writer(str(path), fps=5, macro_block_size=1)
    try:
        for i in range(frames):
            frame = np.full((32, 32, 3), i * 30, dtype=np.uint8)
            writer.append_data(frame)
    finally:
        writer.close()


def test_verify_reopens_and_decodes_full_output(tmp_path):
    out = tmp_path / "clip.redacted.mp4"
    make_video(out, frames=3)
    report = verify_video_output(
        out,
        expected_frames=3,
        continuity={"interpolated_masks": 1},
        audio_preserved=False,
    )
    assert report["passed"] is True
    assert report["verification_status"] == "passed"
    assert report["frames_decoded"] == 3
    assert report["continuity"]["interpolated_masks"] == 1


def test_frame_count_mismatch_fails_without_raising(tmp_path):
    out = tmp_path / "clip.redacted.mp4"
    make_video(out, frames=2)
    report = verify_video_output(out, expected_frames=3)
    assert report["passed"] is False
    assert any("frame count mismatch" in e for e in report["errors"])


def test_missing_output_is_a_reported_failure(tmp_path):
    report = verify_video_output(tmp_path / "missing.redacted.mp4", expected_frames=1)
    assert report["passed"] is False
    assert report["errors"]


def test_sidecar_is_atomic_json_and_is_never_reingested(tmp_path):
    out = tmp_path / "clip.redacted.mp4"
    make_video(out, frames=1)
    report = verify_video_output(out, expected_frames=1)
    sidecar = write_verification_sidecar(out, report)

    assert sidecar == verification_sidecar_path(out)
    assert sidecar.name == "clip.redacted.mp4.verification.json"
    assert is_redaction_output(sidecar)
    loaded = json.loads(sidecar.read_text())
    assert loaded["passed"] is True
    assert not list(tmp_path.glob(".*verification.json.tmp"))


def test_unresolved_gaps_are_warnings_not_failures(tmp_path):
    out = tmp_path / "clip.redacted.mp4"
    make_video(out, frames=3)
    report = verify_video_output(
        out,
        expected_frames=3,
        continuity={"unresolved_gaps": [
            {"label": "FACE", "first_frame": 40, "last_frame": 44},
        ]},
    )
    assert report["passed"] is True
    assert report["verification_status"] == "passed_with_warnings"
    assert len(report["warnings"]) == 1
    assert "masked->exposed->masked" in report["warnings"][0]
    assert "40-44" in report["warnings"][0]


def test_hard_failures_outrank_gap_warnings(tmp_path):
    report = verify_video_output(
        tmp_path / "missing.redacted.mp4",
        expected_frames=1,
        continuity={"unresolved_gaps": [
            {"label": "FACE", "first_frame": 1, "last_frame": 1},
        ]},
    )
    assert report["passed"] is False
    assert report["verification_status"] == "failed"


def test_report_records_source_and_backend(tmp_path):
    out = tmp_path / "clip.redacted.mp4"
    make_video(out, frames=1)
    report = verify_video_output(
        out, expected_frames=1, source=tmp_path / "clip.mp4", backend="yolo",
    )
    assert report["source"] == str(tmp_path / "clip.mp4")
    assert report["backend"] == "yolo"
