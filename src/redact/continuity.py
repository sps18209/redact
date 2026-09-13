"""Bounded temporal continuity for frame-by-frame visual redaction.

Object detectors occasionally miss a face or plate for one or two frames. A
privacy transform must not turn that ordinary detector jitter into a one-frame
leak. This module associates same-label detections across frames, keeps only a
small delayed buffer, and supplies conservative synthetic masks through short
misses.

The design is deliberately bounded:

* association uses IoU OR a motion-tolerant centre-distance gate;
* only ``max_gap`` missing frames are bridged;
* an unmatched track expires rather than becoming a long-lived prediction;
* provisional masks are padded and biased toward hiding too much, not too little;
* if a track is reacquired before emission, the provisional masks are replaced
  with linear interpolation between the two real detections;
* a gap the bound refuses to bridge is *reported* rather than hidden: when a
  new detection starts a track near a recently expired same-label one, the
  frames that went out unmasked in between are recorded as an unresolved gap —
  the masked -> exposed -> masked signature a verification report must
  surface. (Only gaps that end in a re-detection are visible this way; a
  subject never re-detected, or one whose re-detection is associated to a
  different live track, leaves no record.)

No pixels and no model-specific objects live here. The module is dependency-free
and works only with neutral detection tuples:
``(x1, y1, x2, y2, confidence, label)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Detection = Tuple[float, float, float, float, float, str]
Box = Tuple[float, float, float, float]


@dataclass
class _Track:
    track_id: int
    label: str
    last_box: Box
    last_score: float
    last_frame: int
    prev_box: Optional[Box] = None
    prev_frame: Optional[int] = None


class TemporalMaskTracker:
    """Resolve masks with bounded short-gap continuity.

    ``push(frame_index, detections)`` returns zero or more resolved frames as
    ``[(frame_index, detections_to_mask), ...]``. Callers keep the corresponding
    image frames in a tiny buffer until they are returned. ``flush()`` resolves
    the tail at end-of-stream.
    """

    def __init__(
        self,
        max_gap: int = 2,
        *,
        min_iou: float = 0.05,
        centre_gate: float = 1.25,
        padding: float = 0.12,
        gap_report_window: int = 30,
    ) -> None:
        self.max_gap = max(0, int(max_gap))
        self.min_iou = max(0.0, float(min_iou))
        self.centre_gate = max(0.25, float(centre_gate))
        self.padding = max(0.0, float(padding))
        self.gap_report_window = max(0, int(gap_report_window))
        self._tracks: Dict[int, _Track] = {}
        self._pending: Dict[int, Dict[int, Detection]] = {}
        #: Recently expired tracks, kept only so a nearby re-detection can be
        #: recognised as the end of an exposure window: (label, box, last_frame).
        self._recently_expired: List[Tuple[str, Box, int]] = []
        self._next_track_id = 1
        self._last_frame = -1
        self._stats = {
            "raw_detections": 0,
            "tracks_created": 0,
            "interpolated_masks": 0,
            "propagated_masks": 0,
            "expired_tracks": 0,
            "unresolved_gaps": [],
            "max_gap": self.max_gap,
        }

    def push(
        self, frame_index: int, detections: Iterable[Detection]
    ) -> List[Tuple[int, List[Detection]]]:
        """Add one chronological frame and return frames safe to emit."""
        frame_index = int(frame_index)
        if frame_index != self._last_frame + 1:
            raise ValueError(
                "TemporalMaskTracker requires consecutive frame indexes "
                f"({self._last_frame + 1} expected, got {frame_index})"
            )
        self._last_frame = frame_index
        detections = [self._normalise(d) for d in detections]
        self._stats["raw_detections"] += len(detections)
        self._pending.setdefault(frame_index, {})

        # Tracks with a gap larger than the configured bridge cannot be
        # reacquired; expire them before association so an old object cannot
        # steal a new detection later in the scene.
        for tid, track in list(self._tracks.items()):
            if frame_index - track.last_frame - 1 > self.max_gap:
                self._recently_expired.append(
                    (track.label, track.last_box, track.last_frame)
                )
                del self._tracks[tid]
                self._stats["expired_tracks"] += 1
        self._recently_expired = [
            entry for entry in self._recently_expired
            if frame_index - entry[2] <= self.gap_report_window
        ]

        matches, unmatched_dets = self._associate(frame_index, detections)
        matched_tracks = set()

        for tid, det_index in matches:
            track = self._tracks[tid]
            det = detections[det_index]
            matched_tracks.add(tid)
            gap = frame_index - track.last_frame - 1
            old_box = track.last_box
            new_box = det[:4]

            # Reacquisition lets us replace the conservative provisional masks
            # with the path between two actual detector observations.
            if gap > 0:
                for step in range(1, gap + 1):
                    target_frame = track.last_frame + step
                    if target_frame not in self._pending:
                        continue  # already emitted; should not occur within max_gap
                    alpha = step / float(gap + 1)
                    box = _lerp_box(old_box, new_box, alpha)
                    self._pending[target_frame][tid] = _synthetic_detection(
                        box, min(track.last_score, det[4]), det[5], self.padding
                    )
                    self._stats["interpolated_masks"] += 1

            self._pending[frame_index][tid] = det
            track.prev_box = track.last_box
            track.prev_frame = track.last_frame
            track.last_box = new_box
            track.last_score = det[4]
            track.last_frame = frame_index

        # Keep a bounded mask alive while a track is briefly missing. This is
        # intentionally false-positive biased. If it returns, interpolation
        # above replaces these predictions before the affected frames emit.
        for tid, track in list(self._tracks.items()):
            if tid in matched_tracks:
                continue
            missing = frame_index - track.last_frame
            if 1 <= missing <= self.max_gap:
                box = _predict_box(track, missing)
                self._pending[frame_index][tid] = _synthetic_detection(
                    box, track.last_score, track.label, self.padding
                )
                self._stats["propagated_masks"] += 1

        for det_index in unmatched_dets:
            det = detections[det_index]
            self._note_exposure(det, frame_index)
            tid = self._next_track_id
            self._next_track_id += 1
            self._tracks[tid] = _Track(
                track_id=tid,
                label=det[5],
                last_box=det[:4],
                last_score=det[4],
                last_frame=frame_index,
            )
            self._pending[frame_index][tid] = det
            self._stats["tracks_created"] += 1

        emit_through = frame_index - self.max_gap
        return self._emit_through(emit_through)

    def flush(self) -> List[Tuple[int, List[Detection]]]:
        """Resolve the buffered tail after the final input frame."""
        return self._emit_through(self._last_frame)

    def stats(self) -> dict:
        """Return JSON-safe continuity accounting for an audit sidecar."""
        report = dict(self._stats)
        report["unresolved_gaps"] = [dict(gap) for gap in self._stats["unresolved_gaps"]]
        return report

    def _note_exposure(self, det: Detection, frame_index: int) -> None:
        """Record the exposed frame span behind a re-detection of an expired track.

        Propagation covered the first ``max_gap`` frames after the last real
        detection; everything from there to the frame before this re-detection
        went out unmasked. A subject may simply have left and returned, so this
        is a warning for the audit record, never a reason to invent a
        trajectory."""
        best_index: Optional[int] = None
        best_score = float("-inf")
        for index, (label, box, last_frame) in enumerate(self._recently_expired):
            if label != det[5]:
                continue
            score = _match_score(
                box,
                det[:4],
                max(0, frame_index - last_frame - 1),
                min_iou=self.min_iou,
                centre_gate=self.centre_gate,
            )
            if score is not None and score > best_score:
                best_index, best_score = index, score
        if best_index is None:
            return
        label, _box, last_frame = self._recently_expired.pop(best_index)
        first_exposed = last_frame + self.max_gap + 1
        last_exposed = frame_index - 1
        if last_exposed >= first_exposed:
            self._stats["unresolved_gaps"].append(
                {"label": label, "first_frame": first_exposed, "last_frame": last_exposed}
            )

    def _associate(
        self, frame_index: int, detections: Sequence[Detection]
    ) -> Tuple[List[Tuple[int, int]], List[int]]:
        candidates = []
        for tid, track in self._tracks.items():
            gap = max(0, frame_index - track.last_frame - 1)
            for det_index, det in enumerate(detections):
                if det[5] != track.label:
                    continue
                score = _match_score(
                    track.last_box,
                    det[:4],
                    gap,
                    min_iou=self.min_iou,
                    centre_gate=self.centre_gate,
                )
                if score is not None:
                    candidates.append((score, tid, det_index))

        candidates.sort(reverse=True)
        used_tracks = set()
        used_detections = set()
        matches = []
        for _score, tid, det_index in candidates:
            if tid in used_tracks or det_index in used_detections:
                continue
            used_tracks.add(tid)
            used_detections.add(det_index)
            matches.append((tid, det_index))

        unmatched = [i for i in range(len(detections)) if i not in used_detections]
        return matches, unmatched

    def _emit_through(self, frame_index: int) -> List[Tuple[int, List[Detection]]]:
        ready = []
        for idx in sorted(k for k in self._pending if k <= frame_index):
            masks = list(self._pending.pop(idx).values())
            ready.append((idx, masks))
        return ready

    @staticmethod
    def _normalise(det: Detection) -> Detection:
        if len(det) != 6:
            raise ValueError("detection must be (x1, y1, x2, y2, score, label)")
        x1, y1, x2, y2, score, label = det
        if float(x2) <= float(x1) or float(y2) <= float(y1):
            raise ValueError(f"invalid detection box: {det[:4]}")
        return (
            float(x1), float(y1), float(x2), float(y2),
            float(score), str(label),
        )


def _iou(a: Box, b: Box) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


def _match_score(
    old: Box,
    new: Box,
    gap: int,
    *,
    min_iou: float,
    centre_gate: float,
) -> Optional[float]:
    overlap = _iou(old, new)
    ocx, ocy = (old[0] + old[2]) / 2.0, (old[1] + old[3]) / 2.0
    ncx, ncy = (new[0] + new[2]) / 2.0, (new[1] + new[3]) / 2.0
    distance = hypot(ncx - ocx, ncy - ocy)
    diag = max(
        1.0,
        hypot(old[2] - old[0], old[3] - old[1]),
        hypot(new[2] - new[0], new[3] - new[1]),
    )
    gate = diag * centre_gate * (1.0 + 0.65 * max(0, gap))
    if overlap < min_iou and distance > gate:
        return None
    distance_score = max(0.0, 1.0 - distance / max(gate, 1.0))
    return max(overlap * 2.0, distance_score)


def _lerp_box(a: Box, b: Box, alpha: float) -> Box:
    return tuple(float(x + (y - x) * alpha) for x, y in zip(a, b))  # type: ignore[return-value]


def _predict_box(track: _Track, steps: int) -> Box:
    """Short-horizon centre prediction, capped to avoid runaway masks."""
    box = track.last_box
    if track.prev_box is None or track.prev_frame is None:
        return box
    elapsed = max(1, track.last_frame - track.prev_frame)
    cx = (box[0] + box[2]) / 2.0
    cy = (box[1] + box[3]) / 2.0
    pcx = (track.prev_box[0] + track.prev_box[2]) / 2.0
    pcy = (track.prev_box[1] + track.prev_box[3]) / 2.0
    width = box[2] - box[0]
    height = box[3] - box[1]
    vx = (cx - pcx) / elapsed
    vy = (cy - pcy) / elapsed
    # A privacy mask may safely be too large, but a wildly extrapolated mask can
    # hide unrelated content and steal association from a real subject. Bound
    # per-frame motion to a fraction of the last observed box size.
    vx = max(-0.35 * width, min(0.35 * width, vx))
    vy = max(-0.35 * height, min(0.35 * height, vy))
    dx, dy = vx * steps, vy * steps
    return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


def _synthetic_detection(box: Box, score: float, label: str, padding: float) -> Detection:
    x1, y1, x2, y2 = box
    px = max(0.0, x2 - x1) * padding
    py = max(0.0, y2 - y1) * padding
    return (x1 - px, y1 - py, x2 + px, y2 + py, float(score), label)
