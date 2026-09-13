"""Verification for redacted video artifacts.

A writer closing successfully is not proof that a usable privacy artifact was
produced. Re-open the file, decode it end to end, compare the decoded frame
count with the frame count processed by the backend, and write a small JSON
sidecar that records what continuity repair happened.

The sidecar is intentionally named from the already-redacted output, e.g.
``clip.redacted.mp4.verification.json``. That name contains ``.redacted.`` and
therefore satisfies the suite's ingestion-idempotence rule: a later recursive
batch will not ingest its own verification artifact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def verification_sidecar_path(output: Path) -> Path:
    output = Path(output)
    return output.with_name(output.name + ".verification.json")


def verify_video_output(
    output: Path,
    *,
    expected_frames: int,
    continuity: Optional[dict] = None,
    audio_preserved: Optional[bool] = None,
    source: Optional[Path] = None,
    backend: Optional[str] = None,
) -> Dict[str, Any]:
    """Decode ``output`` fully and return a JSON-safe verification report.

    This function never raises for an ordinary verification failure; the report
    carries ``passed=False`` and error strings so a batch backend can return a
    normal ``RedactionResult(success=False, ...)`` instead of aborting the run.
    """
    output = Path(output)
    errors = []
    frames_decoded = 0
    size_bytes = 0

    if not output.is_file():
        errors.append("output file does not exist")
    else:
        try:
            size_bytes = output.stat().st_size
        except OSError as exc:
            errors.append(f"could not stat output: {exc}")
        if size_bytes <= 0:
            errors.append("output file is empty")

    if not errors:
        try:
            import imageio.v2 as iio

            reader = iio.get_reader(str(output))
            try:
                for _frame in reader:
                    frames_decoded += 1
            finally:
                reader.close()
        except Exception as exc:
            errors.append(f"decode failed: {type(exc).__name__}: {exc}")

    if frames_decoded <= 0 and not any(e.startswith("decode failed") for e in errors):
        errors.append("decoded zero frames")
    if int(expected_frames) >= 0 and frames_decoded != int(expected_frames):
        errors.append(
            f"frame count mismatch: decoded {frames_decoded}, expected {int(expected_frames)}"
        )

    malformed_continuity = None
    if continuity is not None and not isinstance(continuity, dict):
        malformed_continuity = continuity
        continuity = None
    continuity = dict(continuity or {})
    # A gap the tracker refused to bridge is honest uncertainty, not a broken
    # artifact: the file still decodes and every detection was masked. It must
    # never be folded into a clean "passed", and it must never outrank a hard
    # decode failure either — a human reviews the named frames, the batch runs
    # on. A malformed gap record from a caller degrades to a warning too: this
    # function's contract is that it never raises.
    warnings = []
    if malformed_continuity is not None:
        warnings.append(f"malformed continuity record: {malformed_continuity!r}")
    raw_gaps = continuity.get("unresolved_gaps") or []
    if not isinstance(raw_gaps, (list, tuple)):
        warnings.append(f"malformed continuity unresolved_gaps: {raw_gaps!r}")
        raw_gaps = []
    for gap in raw_gaps:
        if isinstance(gap, dict):
            warnings.append(
                "possible masked->exposed->masked sequence: "
                f"{gap.get('label', '?')} unmasked frames "
                f"{gap.get('first_frame', '?')}-{gap.get('last_frame', '?')}"
            )
        else:
            warnings.append(f"malformed continuity gap record: {gap!r}")
    if errors:
        status = "failed"
    elif warnings:
        status = "passed_with_warnings"
    else:
        status = "passed"
    report: Dict[str, Any] = {
        "verification_version": 1,
        "source": str(source) if source is not None else None,
        "backend": backend,
        "output": str(output),
        "verification_status": status,
        "passed": not errors,
        "warnings": warnings,
        "size_bytes": size_bytes,
        "expected_frames": int(expected_frames),
        "frames_decoded": frames_decoded,
        "audio_preserved": audio_preserved,
        "continuity": continuity,
        "errors": errors,
    }
    return report


def write_verification_sidecar(output: Path, report: Dict[str, Any]) -> Path:
    """Atomically persist a verification report next to ``output``."""
    sidecar = verification_sidecar_path(Path(output))
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_name("." + sidecar.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(sidecar)
    return sidecar
