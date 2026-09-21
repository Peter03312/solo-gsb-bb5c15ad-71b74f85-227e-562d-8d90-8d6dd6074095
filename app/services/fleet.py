"""Fleet coordination: common departure windows for a group of vessels.

Each vessel gets its own safe/departure window computation
(:func:`app.services.tides.compute_navigation_windows`) against the SAME
shared tide samples, query range, max sampling gap and maintenance closures.
A *common* departure instant ``t`` must then satisfy every vessel:

* for each vessel, the occupancy ``[t, t + transit_duration)`` lies entirely
  inside one of that vessel's safe windows (its own draft/clearance
  threshold), and does not intersect any ``unknown`` stretch;
* the occupancy of no vessel intersects a maintenance closure or an unknown
  stretch — these are whole-fleet constraints.  Because the closure is
  shifted back by the transit duration, the vessel with the longest transit
  dominates: the fleet bars departures on ``[s - max_d, e)`` for every merged
  closure ``[s, e)`` (unknown stretches are handled the same way, via each
  vessel's own departure-window computation and the fleet union below).

Besides the common windows, every departure instant that lies in the
*fleet-candidate range* (departable for at least one vessel) but is not common
is reported as an exclusion, tagged either with the names of the vessels that
cannot depart then, or — when the instant is barred by a fleet-wide closure
/ unknown sampling stretch — with an empty vessel list and the fleet reason.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.services.tides import (
    DepartureWindow,
    NavigationResult,
    _merge_intervals,
    compute_navigation_windows,
    to_datetime,
    to_epoch,
)


@dataclass(frozen=True)
class FleetShip:
    """One vessel's parameters within a fleet task."""

    name: str
    draft_m: float
    clearance_m: float
    transit_duration_seconds: float


@dataclass(frozen=True)
class FleetShipResult:
    """Per-vessel window computation outcome."""

    name: str
    threshold_m: float
    windows: list
    unknown: list
    departure_windows: list


@dataclass(frozen=True)
class FleetExclusion:
    """A fleet-candidate stretch excluded from the common departure windows.

    ``ships`` lists the vessels that cannot depart on this stretch in request
    order; an empty list means the stretch is barred by a fleet-wide
    constraint (maintenance closure or unknown sampling segment) rather than
    by individual vessel depth constraints.
    """

    start: datetime
    end: datetime
    reason: str
    ships: list[str]
    duration_seconds: int


@dataclass(frozen=True)
class FleetResult:
    ships: list[FleetShipResult]
    common_departure_windows: list[DepartureWindow]
    exclusions: list[FleetExclusion]


# Reasons carried on exclusions.
REASON_UNKNOWN = "unknown_sample_segment"
REASON_MAINTENANCE = "maintenance_closure"
REASON_SHIP = "ship_constraint"


def _to_segments(windows: Sequence[DepartureWindow]) -> list[tuple[int, int]]:
    """Half-open integer-second segments [a, b+1) from departure windows."""
    return [(to_epoch(w.start), to_epoch(w.end) + 1) for w in windows]


def _merge(segments: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of integer half-open segments (touching segments merge)."""
    if not segments:
        return []
    out: list[list[int]] = []
    for a, b in sorted(segments):
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _intersect(
    left: Sequence[tuple[int, int]], right: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Intersection of two sorted, merged segment lists."""
    out: list[tuple[int, int]] = []
    i = j = 0
    while i < len(left) and j < len(right):
        a, b = left[i]
        c, d = right[j]
        lo, hi = max(a, c), min(b, d)
        if lo < hi:
            out.append((lo, hi))
        if b < d:
            i += 1
        else:
            j += 1
    return out


def _contains(segments: Sequence[tuple[int, int]], t: int) -> bool:
    """Whether integer instant ``t`` lies inside merged half-open segments."""
    starts = [a for a, _ in segments]
    k = bisect.bisect_right(starts, t) - 1
    return k >= 0 and t < segments[k][1]


def _shifted_fleet_blocks(
    blocks: Sequence[tuple[datetime, datetime]],
    max_transit: float,
    query_start: datetime,
    query_end: datetime,
) -> list[tuple[int, int]]:
    """Shift forbidden occupancy blocks back by the fleet's longest transit.

    A merged block ``[s, e)`` bars fleet departures on ``(s - max_d, e)``.
    In integer departure seconds that is ``[floor(s-max_d)+1, ceil(e))``:
    cut points (departures whose occupancy merely touches the block) stay
    allowed, matching per-vessel departure-window semantics.
    """
    qs = to_epoch(query_start)
    qe = to_epoch(query_end)
    shifted = [(to_epoch(s) - max_transit, to_epoch(e)) for s, e in blocks]
    merged = _merge_intervals(shifted, qs, qe)
    segments: list[tuple[int, int]] = []
    for a, b in merged:
        lo = int(math.floor(a)) + 1
        hi = int(math.ceil(b))
        if lo < hi:
            segments.append((lo, hi))
    return _merge(segments)


def compute_fleet_windows(
    ships: Sequence[FleetShip],
    sample_times: Sequence[datetime],
    sample_heights: Sequence[float],
    query_start: datetime,
    query_end: datetime,
    max_gap_seconds: float,
    maintenance_blocks: Sequence[tuple[datetime, datetime]] | None = None,
) -> FleetResult:
    """Compute per-vessel windows and the fleet's common departure windows.

    See the module docstring for the fleet constraints.  Every vessel must
    supply a transit duration (validated at the schema layer): coordination is
    only meaningful for vessels completing a voyage.
    """
    qs = to_epoch(query_start)
    qe = to_epoch(query_end)

    ship_results: list[FleetShipResult] = []
    ship_segments: list[list[tuple[int, int]]] = []
    raw_segments: list[list[tuple[int, int]]] = []
    unknown_intervals: list = []

    for ship in ships:
        common_kwargs = dict(
            sample_times=sample_times,
            sample_heights=sample_heights,
            query_start=query_start,
            query_end=query_end,
            draft_m=ship.draft_m,
            clearance_m=ship.clearance_m,
            max_gap_seconds=max_gap_seconds,
            transit_duration_seconds=ship.transit_duration_seconds,
        )
        # Closure-aware departure windows: used for the common intersection.
        result: NavigationResult = compute_navigation_windows(
            **common_kwargs, maintenance_blocks=maintenance_blocks
        )
        # Raw (closure-free) departure windows: used to decide whether a
        # blocked stretch is a fleet-wide closure or a vessel depth limit.
        raw_result = compute_navigation_windows(
            **common_kwargs, maintenance_blocks=None
        )
        ship_results.append(
            FleetShipResult(
                name=ship.name,
                threshold_m=result.threshold_m,
                windows=result.windows,
                unknown=result.unknown,
                departure_windows=list(result.departure_windows or []),
            )
        )
        ship_segments.append(_merge(_to_segments(result.departure_windows or [])))
        raw_segments.append(_merge(_to_segments(raw_result.departure_windows or [])))
        # Unknown stretches are identical for every vessel (shared samples);
        # remember them from the first vessel.
        if not unknown_intervals:
            unknown_intervals = result.unknown

    # Common windows: intersection of every vessel's departure windows.
    # Departure windows already live on conservative integer seconds inside
    # the query range, so the full integer-second domain is a neutral element.
    q0 = int(math.floor(qs))
    q1 = int(math.ceil(qe))
    common_segments: list[tuple[int, int]] = [(q0, q1 + 1)]
    for segs in ship_segments:
        common_segments = _intersect(common_segments, segs)
        if not common_segments:
            break

    # Fleet-wide blocked departure stretches (longest-transit shift), split by
    # reason for exclusion attribution.
    max_transit = max(s.transit_duration_seconds for s in ships)
    fleet_unknown = _shifted_fleet_blocks(
        [(u.start, u.end) for u in unknown_intervals],
        max_transit,
        query_start,
        query_end,
    )
    fleet_maintenance = _shifted_fleet_blocks(
        list(maintenance_blocks or []),
        max_transit,
        query_start,
        query_end,
    )

    # Atomic partition of the fleet-candidate range: every second on which at
    # least one vessel could depart (raw, closure-free departability) is in
    # scope; inside one piece the exclusion classification is constant.
    raw_union: list[tuple[int, int]] = []
    for segs in raw_segments:
        raw_union = _merge(raw_union + segs)

    cuts: set[int] = set()
    for a, b in raw_union:
        cuts.add(a)
        cuts.add(b)
    # Fleet-block boundaries must split pieces even when they fall inside a
    # single vessel's window: classification (fleet-wide vs. ship) is taken at
    # the piece start and must be constant over the piece.
    for segs in (fleet_unknown, fleet_maintenance):
        for a, b in segs:
            cuts.add(a)
            cuts.add(b)
    for segs in ship_segments:
        for a, b in segs:
            cuts.add(a)
            cuts.add(b)
    grid = sorted(cuts)

    common_windows = [
        DepartureWindow(
            start=to_datetime(a),
            end=to_datetime(b - 1),
            duration_seconds=int(b - 1 - a),
        )
        for a, b in common_segments
    ]

    exclusions: list[FleetExclusion] = []
    for k in range(len(grid) - 1):
        a, b = grid[k], grid[k + 1]
        if a >= b:
            continue
        # Only classify pieces inside the fleet-candidate range.
        if not _contains(raw_union, a):
            continue
        # Whole-fleet constraints first (empty ship list).
        if _contains(fleet_maintenance, a):
            reason = REASON_MAINTENANCE
            names: list[str] = []
        elif _contains(fleet_unknown, a):
            reason = REASON_UNKNOWN
            names = []
        else:
            blocking = [
                ship.name
                for ship, segs in zip(ships, ship_segments, strict=True)
                if not _contains(segs, a)
            ]
            if not blocking:
                continue  # every vessel departs: belongs to a common window
            reason = REASON_SHIP
            names = blocking
        exclusions.append(
            FleetExclusion(
                start=to_datetime(a),
                end=to_datetime(b - 1),
                reason=reason,
                ships=names,
                duration_seconds=int(b - 1 - a),
            )
        )

    # Merge adjacent pieces carrying the same reason and vessel list.
    merged_exclusions: list[FleetExclusion] = []
    for ex in exclusions:
        key = (ex.reason, tuple(ex.ships))
        if (
            merged_exclusions
            and (
                merged_exclusions[-1].reason,
                tuple(merged_exclusions[-1].ships),
            )
            == key
            and to_epoch(merged_exclusions[-1].end) + 1 == to_epoch(ex.start)
        ):
            prev = merged_exclusions[-1]
            merged_exclusions[-1] = FleetExclusion(
                start=prev.start,
                end=ex.end,
                reason=ex.reason,
                ships=list(ex.ships),
                duration_seconds=to_epoch(ex.end) - to_epoch(prev.start),
            )
        else:
            merged_exclusions.append(ex)

    return FleetResult(
        ships=ship_results,
        common_departure_windows=common_windows,
        exclusions=merged_exclusions,
    )
