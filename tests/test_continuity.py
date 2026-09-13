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


def test_reacquisition_beyond_the_bound_records_an_unresolved_gap():
    tracker = TemporalMaskTracker(max_gap=2)
    for idx, dets in enumerate([[det(0, 10)], [], [], [], [], [], [det(1, 11)]]):
        tracker.push(idx, dets)
    tracker.flush()

    stats = tracker.stats()
    # Propagation covered frames 1-2; frames 3-5 went out unmasked.
    assert stats["unresolved_gaps"] == [
        {"label": "FACE", "first_frame": 3, "last_frame": 5}
    ]
    assert stats["tracks_created"] == 2


def test_reacquisition_within_the_bound_is_not_an_unresolved_gap():
    tracker = TemporalMaskTracker(max_gap=2)
    for idx, dets in enumerate([[det(0, 10)], [], [det(0, 10)]]):
        tracker.push(idx, dets)
    tracker.flush()
    assert tracker.stats()["unresolved_gaps"] == []


def test_gap_reporting_forgets_expired_tracks_beyond_the_window():
    tracker = TemporalMaskTracker(max_gap=1, gap_report_window=4)
    frames = [[det(0, 10)]] + [[] for _ in range(8)] + [[det(0, 10)]]
    for idx, dets in enumerate(frames):
        tracker.push(idx, dets)
    tracker.flush()
    # Nine frames later is beyond the window: a fresh subject, not a gap.
    assert tracker.stats()["unresolved_gaps"] == []


def test_gap_reporting_never_crosses_labels():
    tracker = TemporalMaskTracker(max_gap=1)
    frames = [[det(0, 10, label="FACE")], [], [], [],
              [det(0, 10, label="PLATE")]]
    for idx, dets in enumerate(frames):
        tracker.push(idx, dets)
    tracker.flush()
    assert tracker.stats()["unresolved_gaps"] == []


def test_stats_returns_an_isolated_copy():
    tracker = TemporalMaskTracker(max_gap=1)
    for idx, dets in enumerate([[det(0, 10)], [], [], [], [det(0, 10)]]):
        tracker.push(idx, dets)
    tracker.flush()
    first = tracker.stats()
    first["unresolved_gaps"].clear()
    first["unresolved_gaps"].append({"label": "TAMPERED"})
    assert tracker.stats()["unresolved_gaps"] == [
        {"label": "FACE", "first_frame": 2, "last_frame": 3}
    ]


def test_reacquisition_attributes_the_gap_to_the_nearest_expired_track():
    """The gap-widened match score would prefer the staler, farther entry;
    attribution must go to the spatially nearest one — those frame ranges
    are exactly what a human is told to review."""
    tracker = TemporalMaskTracker(max_gap=1)
    tracker.push(0, [det(30, 40), det(0, 10)])   # S1 (once) and S2
    for idx in range(1, 8):                      # S2 tracked through frame 7
        tracker.push(idx, [det(0, 10)])
    for idx in range(8, 12):                     # both lost
        tracker.push(idx, [])
    tracker.push(12, [det(10, 20)])              # S2 returns, 10 px away
    tracker.flush()

    gaps = tracker.stats()["unresolved_gaps"]
    # Attributed to S2 (last seen frame 7, propagation covered frame 8):
    # exposed 9-11 — not to S1's frame-0 entry, which would claim 2-11.
    assert gaps == [{"label": "FACE", "first_frame": 9, "last_frame": 11}]


def test_far_reacquisitions_beyond_jitter_scale_are_not_gaps():
    """Uncapped, the gap-widened gate matches a same-label detection across
    the whole frame; capped at jitter scale it must reject it."""
    tracker = TemporalMaskTracker(max_gap=2)
    tracker.push(0, [det(0, 100)])
    for idx in range(1, 25):
        tracker.push(idx, [])
    tracker.push(25, [det(1900, 2000)])          # opposite side of the frame
    tracker.flush()
    assert tracker.stats()["unresolved_gaps"] == []


def test_report_window_is_floored_so_reporting_cannot_be_disabled():
    """A window below max_gap + 2 would prune every expired entry before it
    could ever be matched — the constructor floors it."""
    tracker = TemporalMaskTracker(max_gap=2, gap_report_window=0)
    assert tracker.gap_report_window == 4
    tracker.push(0, [det(0, 10)])
    for idx in range(1, 4):
        tracker.push(idx, [])
    tracker.push(4, [det(0, 10)])                # re-detected at expiry frame
    tracker.flush()
    assert tracker.stats()["unresolved_gaps"] == [
        {"label": "FACE", "first_frame": 3, "last_frame": 3}
    ]
