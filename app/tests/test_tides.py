import math
from datetime import datetime, timedelta, timezone

from app.services.tides import compute_navigation_windows


def dt(hours: float) -> datetime:
    return datetime.fromtimestamp(hours * 3600, tz=timezone.utc)


TRIANGLE_HEIGHTS = [0, 2, 4, 6, 8, 10, 8, 6, 4, 2, 0, 2, 4]


def _triangle(transit=None, maintenance=None):
    # Heights 0..12 h: rise to 10 m, fall to 0, then slight rise below
    # threshold. Safety threshold = draft 4 + clearance 1 = 5 m; the single
    # safe window is exactly [02:30, 07:30].
    return compute_navigation_windows(
        sample_times=[dt(h) for h in range(13)],
        sample_heights=TRIANGLE_HEIGHTS,
        query_start=dt(0),
        query_end=dt(12),
        draft_m=4.0,
        clearance_m=1.0,
        max_gap_seconds=4000,
        transit_duration_seconds=transit,
        maintenance_blocks=maintenance,
    )


def test_linear_triangle_windows():
    # Heights 0..12 h: rise to 10 m, fall to 0, then slight rise below
    # threshold. Safety threshold = draft 4 + clearance 1 = 5 m.
    times = [dt(h) for h in range(13)]
    heights = [0, 2, 4, 6, 8, 10, 8, 6, 4, 2, 0, 2, 4]

    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=dt(0),
        query_end=dt(12),
        draft_m=4.0,
        clearance_m=1.0,  # threshold = 5
        max_gap_seconds=4000,
    )
    assert res.threshold_m == 5.0
    assert res.unknown == []
    # Knots 3..7 (03:00..07:00, heights 6..10..6) form one continuous run;
    # the rising crossing is at 02:30 (9000 s), the falling at 07:30 (27000 s).
    assert len(res.windows) == 1
    w = res.windows[0]
    assert w.start == datetime.fromtimestamp(math.ceil(9000), tz=timezone.utc)
    assert w.end == datetime.fromtimestamp(math.floor(27000), tz=timezone.utc)
    # Every rounded boundary/inside knot satisfies the threshold.
    assert w.start_height_m >= 5.0 - 1e-9
    assert w.end_height_m >= 5.0 - 1e-9
    assert w.min_height_m >= 5.0 - 1e-9
    assert w.duration_seconds == 18000


def test_gap_splits_windows_and_is_unknown():
    # Two safe plateaus separated by a 3-hour gap (> max_gap).
    times = [dt(h) for h in (0, 1, 2, 5, 6, 7)]
    heights = [8, 9, 8, 8, 9, 8]
    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=dt(0),
        query_end=dt(7),
        draft_m=5.0,
        clearance_m=0.0,
        max_gap_seconds=3600 + 60,  # 3 h gap exceeds this
    )
    assert len(res.windows) == 2
    # Windows never span the forbidden gap.
    assert res.windows[0].end <= dt(2)
    assert res.windows[1].start >= dt(5)
    reasons = [u.reason for u in res.unknown]
    assert "sample_gap_exceeds_max_gap" in reasons
    gap = next(u for u in res.unknown if u.reason == "sample_gap_exceeds_max_gap")
    assert gap.start == dt(2) and gap.end == dt(5)


def test_query_outside_coverage_is_unknown():
    times = [dt(2), dt(3)]
    heights = [9.0, 9.0]
    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=dt(0),
        query_end=dt(10),
        draft_m=5.0,
        clearance_m=0.0,
        max_gap_seconds=7200,
    )
    assert len(res.windows) == 1
    assert res.windows[0].start == dt(2) and res.windows[0].end == dt(3)
    assert {u.reason for u in res.unknown} == {"no_sample_coverage"}
    assert res.unknown[0].start == dt(0)
    assert res.unknown[-1].end == dt(10)


def test_no_safe_window():
    times = [dt(0), dt(1), dt(2)]
    heights = [1.0, 1.2, 1.0]
    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=dt(0),
        query_end=dt(2),
        draft_m=5.0,
        clearance_m=0.0,
        max_gap_seconds=7200,
    )
    assert res.windows == []
    assert res.unknown == []


def test_crossing_rounded_to_seconds():
    # Crossing at 100.5 s after the query start: start rounds up.
    t0 = datetime(2026, 9, 21, tzinfo=timezone.utc)
    times = [
        t0,
        datetime.fromtimestamp(t0.timestamp() + 201, tz=timezone.utc),
    ]
    heights = [0.0, 20.0]  # threshold 10 crosses at 100.5 s
    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=t0,
        query_end=times[1],
        draft_m=10.0,
        clearance_m=0.0,
        max_gap_seconds=10000,
    )
    assert len(res.windows) == 1
    assert res.windows[0].start == datetime.fromtimestamp(
        t0.timestamp() + 101, tz=timezone.utc
    )
    assert res.windows[0].end == times[1]


def test_departure_windows_absent_without_transit():
    res = _triangle()
    assert res.departure_windows is None


def test_departure_windows_basic():
    res = _triangle(transit=3600)
    assert res.departure_windows is not None
    assert len(res.windows) == 1
    assert len(res.departure_windows) == 1
    w = res.windows[0]
    dep = res.departure_windows[0]
    # The whole occupancy [t, t+3600) must fit in the safe window:
    # departures run from the window start to window end minus the transit.
    assert dep.start == w.start == dt(2.5)
    assert dep.end == w.end - timedelta(hours=1) == dt(6.5)
    assert dep.duration_seconds == 18000 - 3600


def test_departure_windows_window_shorter_than_transit():
    res = _triangle(transit=6 * 3600)  # safe window lasts only 5 h
    assert res.departure_windows == []


def test_departure_windows_maintenance_shift_and_boundaries():
    res = _triangle(transit=3600, maintenance=[(dt(4), dt(5))])
    # Closure [04:00, 05:00) bars departures in (03:00, 05:00): a departure
    # at exactly 03:00 occupies [03:00, 04:00) and only touches the closure,
    # and one at exactly 05:00 starts as the closure ends — both allowed.
    deps = res.departure_windows
    assert [(d.start, d.end) for d in deps] == [
        (dt(2.5), dt(3)),
        (dt(5), dt(6.5)),
    ]


def test_departure_windows_merge_touching_and_overlapping_blocks():
    # Touching blocks merge into [03:00, 05:00) before the shift.
    res = _triangle(transit=3600, maintenance=[(dt(3), dt(4)), (dt(4), dt(5))])
    assert [(d.start, d.end) for d in res.departure_windows] == [
        (dt(5), dt(6.5)),
    ]
    # Overlapping blocks merge the same way.
    res = _triangle(transit=3600, maintenance=[(dt(3), dt(4.5)), (dt(4), dt(5))])
    assert [(d.start, d.end) for d in res.departure_windows] == [
        (dt(5), dt(6.5)),
    ]


def test_departure_windows_blocks_clipped_to_query_range():
    # Blocks entirely outside the query range are clipped away.
    res = _triangle(transit=3600, maintenance=[(dt(20), dt(21))])
    assert len(res.departure_windows) == 1
    assert res.departure_windows[0].start == dt(2.5)
    assert res.departure_windows[0].end == dt(6.5)
    # A block straddling the query end is clipped, then shifted.
    res = _triangle(transit=3600, maintenance=[(dt(7), dt(20))])
    assert [(d.start, d.end) for d in res.departure_windows] == [
        (dt(2.5), dt(6)),
    ]


def test_departure_windows_never_cross_unknown():
    # Two safe plateaus separated by a forbidden gap: each plateau yields its
    # own departure window, none spans the unknown stretch.
    res = compute_navigation_windows(
        sample_times=[dt(h) for h in (0, 1, 2, 5, 6, 7)],
        sample_heights=[8, 9, 8, 8, 9, 8],
        query_start=dt(0),
        query_end=dt(7),
        draft_m=5.0,
        clearance_m=0.0,
        max_gap_seconds=3600 + 60,
        transit_duration_seconds=3600,
    )
    assert len(res.windows) == 2
    assert [(d.start, d.end) for d in res.departure_windows] == [
        (dt(0), dt(1)),
        (dt(5), dt(6)),
    ]
    for dep in res.departure_windows:
        assert dep.end + timedelta(hours=1) <= dt(2) or dep.start >= dt(5)
