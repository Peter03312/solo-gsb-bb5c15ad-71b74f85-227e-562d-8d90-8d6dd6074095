"""Unit tests for fleet coordination (common departure windows)."""

from datetime import datetime, timedelta, timezone

from app.services.fleet import (
    REASON_MAINTENANCE,
    REASON_SHIP,
    REASON_UNKNOWN,
    FleetShip,
    compute_fleet_windows,
)

ONE_SECOND = timedelta(seconds=1)


def dt(hours: float) -> datetime:
    return datetime.fromtimestamp(hours * 3600, tz=timezone.utc)


TRIANGLE_HEIGHTS = [0, 2, 4, 6, 8, 10, 8, 6, 4, 2, 0, 2, 4]


def _triangle_ships(*specs):
    # specs: (name, threshold, transit_hours)
    return [
        FleetShip(
            name=name,
            draft_m=threshold - 1.0,
            clearance_m=1.0,
            transit_duration_seconds=transit_h * 3600,
        )
        for name, threshold, transit_h in specs
    ]


def _fleet(ships, maintenance=None, heights=None, times=None, q0=0, q1=12, gap=4000):
    heights = heights if heights is not None else TRIANGLE_HEIGHTS
    times = times if times is not None else [dt(h) for h in range(len(heights))]
    return compute_fleet_windows(
        ships=ships,
        sample_times=times,
        sample_heights=heights,
        query_start=dt(q0),
        query_end=dt(q1),
        max_gap_seconds=gap,
        maintenance_blocks=maintenance,
    )


def test_per_ship_windows_computed_with_own_threshold():
    # A needs 5 m (safe 02:30-07:30), B needs 9 m (safe ~04:24-05:36).
    res = _fleet(_triangle_ships(("A", 5, 0.5), ("B", 9, 0.5)))
    assert [s.threshold_m for s in res.ships] == [5.0, 9.0]
    assert len(res.ships[0].windows) == 1
    b_window = res.ships[1].windows[0]
    assert b_window.start > dt(4)
    assert b_window.end < dt(6)
    # Both share the same tide samples; no unknown anywhere.
    assert all(s.unknown == [] for s in res.ships)


def test_common_windows_are_intersection_of_departure_windows():
    # Same threshold (safe window 02:30-07:30), different transits:
    # A (1 h) departs 02:30-06:30, B (2 h) departs 02:30-05:30.
    res = _fleet(_triangle_ships(("A", 5, 1), ("B", 5, 2)))
    common = res.common_departure_windows
    assert len(common) == 1
    assert common[0].start == dt(2.5)
    assert common[0].end == dt(5.5)
    assert common[0].duration_seconds == 3 * 3600
    # The stretch barred only by B's longer transit is attributed to B.
    b_only = [e for e in res.exclusions if e.reason == REASON_SHIP]
    assert [(e.ships, e.start, e.end) for e in b_only] == [
        (["B"], dt(5.5) + ONE_SECOND, dt(6.5))
    ]


def test_maintenance_closure_is_fleet_constraint_with_empty_ship_list():
    # Closure [04:00, 05:00): A transits 1 h, B transits 2 h.
    # Per-vessel departure windows:
    #   A: 02:30-03:00, 05:00-06:30
    #   B: 05:00-05:30
    # Common: 05:00-05:30.
    res = _fleet(
        _triangle_ships(("A", 5, 1), ("B", 5, 2)),
        maintenance=[(dt(4), dt(5))],
    )
    common = res.common_departure_windows
    assert [(d.start, d.end) for d in common] == [(dt(5), dt(5.5))]

    closure_excl = [e for e in res.exclusions if e.reason == REASON_MAINTENANCE]
    assert len(closure_excl) == 1
    ex = closure_excl[0]
    # Shifted by the LONGEST transit (2 h): barred departures (02:00, 05:00),
    # intersect the fleet-candidate range from 02:30.
    assert ex.start == dt(2.5)
    assert ex.end == dt(5) - ONE_SECOND
    # Fleet-wide exclusion: no individual vessel is blamed.
    assert ex.ships == []

    ship_excl = [e for e in res.exclusions if e.reason == REASON_SHIP]
    assert [e.ships for e in ship_excl] == [["B"]]
    assert ship_excl[0].start == dt(5.5) + ONE_SECOND


def test_unknown_sample_segment_is_fleet_constraint():
    # Two safe plateaus with a 3-hour forbidden gap.  A transits 1 h, B 2 h.
    times = [dt(h) for h in (0, 1, 2, 5, 6, 7)]
    heights = [8, 9, 8, 8, 9, 8]
    res = _fleet(
        _triangle_ships(("A", 5, 1), ("B", 5, 2)),
        heights=heights,
        times=times,
        q0=0,
        q1=7,
        gap=3660,
    )
    # No common departure spanning the gap.
    for d in res.common_departure_windows:
        assert d.end <= dt(2) or d.start >= dt(5)
    # The gap (shifted by B's 2 h transit) is a fleet exclusion with no names.
    unknown_excl = [e for e in res.exclusions if e.reason == REASON_UNKNOWN]
    assert unknown_excl and all(e.ships == [] for e in unknown_excl)
    reasons = {u.reason for s in res.ships for u in s.unknown}
    assert "sample_gap_exceeds_max_gap" in reasons


def test_no_common_window_when_draughts_disjoint():
    # A threshold 5 (safe 02:30-07:30); B threshold 11 (never safe).
    res = _fleet(_triangle_ships(("A", 5, 1), ("B", 11, 1)))
    assert res.common_departure_windows == []
    # Every candidate exclusion on A's range is blamed on B.
    assert res.exclusions
    assert all(e.ships == ["B"] for e in res.exclusions)
    # B itself has no windows/departures.
    assert res.ships[1].windows == []
    assert res.ships[1].departure_windows == []


def test_single_vessel_fleet_common_equals_its_departure_windows():
    res = _fleet(
        _triangle_ships(("solo", 5, 1)),
        maintenance=[(dt(4), dt(5))],
    )
    expected = [(d.start, d.end) for d in res.ships[0].departure_windows]
    assert [(d.start, d.end) for d in res.common_departure_windows] == expected
    # The closure keeps the fleet attribution even with a single vessel.
    assert [e.reason for e in res.exclusions] == [REASON_MAINTENANCE]
    assert res.exclusions[0].ships == []


def test_exclusion_duration_is_end_minus_start_seconds():
    res = _fleet(_triangle_ships(("A", 5, 1), ("B", 5, 2)))
    for e in res.exclusions:
        assert e.duration_seconds == int((e.end - e.start).total_seconds())
    for d in res.common_departure_windows:
        assert d.duration_seconds == int((d.end - d.start).total_seconds())


def test_multiple_vessels_blocking_lists_all_names_in_request_order():
    # A and B have disjoint safe windows: A threshold 5 (02:30-07:30),
    # C-like ship threshold 11 never safe. Add two failing vessels.
    res = _fleet(
        _triangle_ships(("A", 5, 1), ("B", 11, 1), ("C", 12, 1))
    )
    assert res.common_departure_windows == []
    for e in res.exclusions:
        assert e.reason == REASON_SHIP
        assert e.ships == ["B", "C"]


def test_zero_duration_common_window_supported():
    # Threshold 9 gives B a ~71-minute safe window; a 4000 s transit leaves
    # only a small departure range (possibly a single second).
    res = _fleet(_triangle_ships(("A", 5, 1), ("B", 9, 4000 / 3600)))
    assert res.common_departure_windows
    dep = res.common_departure_windows[0]
    assert dep.duration_seconds >= 0
