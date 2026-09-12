"""Pure-stdlib tests for bounded temporal redaction continuity."""

import pytest

from redact.continuity import TemporalMaskTracker


def det(x1, x2, label="FACE", score=0.9):
    return (float(x1), 0.0, float(x2), 10.0, float(score), label)


def resolved_map(items):
    return {idx: masks for idx, masks in items}


def test_short_gap_is_masked_and_reacquisition_interpolates():
    tracker = TemporalMaskTracker(max_gap=2, padding=0.0)
    out = []
    out += tracker.push(0, [det(0, 10)])
    out += tracker.push(1, [])
    out += tracker.push(2, [det(20, 30)])  # no IoU; motion gate still links it
    out += tracker.flush()

    by_frame = resolved_map(out)
    assert sorted(by_frame) == [0, 1, 2]
    assert len(by_frame[1]) == 1
    x1, _y1, x2, _y2, _score, label = by_frame[1][0]
    assert x1 == pytest.approx(10.0)
    assert x2 == pytest.approx(20.0)
    assert label == "FACE"
    stats = tracker.stats()
    assert stats["tracks_created"] == 1
    assert stats["interpolated_masks"] == 1
    assert stats["propagated_masks"] >= 1  # provisional mask existed before reacquisition


def test_unrecovered_short_gap_keeps_conservative_mask():
    tracker = TemporalMaskTracker(max_gap=2, padding=0.1)
    out = []
    out += tracker.push(0, [det(10, 20)])
    out += tracker.push(1, [])
    out += tracker.push(2, [])
    out += tracker.flush()

    by_frame = resolved_map(out)
    assert len(by_frame[1]) == len(by_frame[2]) == 1
    # Synthetic masks are deliberately expanded beyond the last real box.
    assert by_frame[1][0][0] < 10.0
    assert by_frame[1][0][2] > 20.0


def test_gap_larger_than_bound_expires_track_instead_of_inventing_identity():
    tracker = TemporalMaskTracker(max_gap=1, padding=0.0)
    out = []
    out += tracker.push(0, [det(0, 10)])
    out += tracker.push(1, [])
    out += tracker.push(2, [])
    out += tracker.push(3, [det(0, 10)])
    out += tracker.flush()

    by_frame = resolved_map(out)
    assert by_frame[1]  # one missing frame is bridged
    assert by_frame[2] == []  # the second miss is beyond max_gap
    assert tracker.stats()["tracks_created"] == 2
    assert tracker.stats()["expired_tracks"] >= 1


def test_labels_never_cross_associate():
    tracker = TemporalMaskTracker(max_gap=2, padding=0.0)
    tracker.push(0, [det(0, 10, "FACE")])
    tracker.push(1, [det(0, 10, "LICENSE_PLATE")])
    tracker.flush()
    assert tracker.stats()["tracks_created"] == 2


def test_frame_indexes_must_be_consecutive():
    tracker = TemporalMaskTracker(max_gap=2)
    tracker.push(0, [])
    with pytest.raises(ValueError, match="consecutive"):
        tracker.push(2, [])


def test_zero_gap_disables_synthetic_masks():
    tracker = TemporalMaskTracker(max_gap=0)
    first = tracker.push(0, [det(0, 10)])
    second = tracker.push(1, [])
    assert first[0][1]
    assert second[0][1] == []
    assert tracker.stats()["propagated_masks"] == 0
