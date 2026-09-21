"""Tidal navigation window computation.

The algorithm performs a piecewise cubic Hermite interpolating polynomial
(PCHIP) interpolation of the tide samples.  PCHIP is shape preserving and
monotonic on every monotonic data interval, hence it never overshoots the
two endpoints of an interval: an interval whose endpoint heights are both
below the safety threshold cannot contain a (real) window and vice versa.

Threshold crossings are located with Brent's method (scipy.optimize.brentq)
to far below one-second accuracy and then rounded conservatively to whole
seconds (start up, end down) so that every reported instant satisfies the
safety depth requirement.

Sample pairs whose spacing exceeds ``max_gap_seconds`` are never bridged:
they split the samples into independent chains, and the affected parts of
the query range are returned as ``unknown`` intervals.  Windows never cross
those gaps.

Optionally, when a transit duration is supplied, departure windows are
derived on top of the safe windows: a departure at ``t`` occupies
``[t, t + duration)`` and is only reported when the whole occupancy stays
inside ONE safe window and clears every maintenance closure and every
``unknown`` stretch.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import brentq

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ROOT_XTOL = 1e-6  # seconds; well below the required one-second accuracy


@dataclass(frozen=True)
class Window:
    """One continuous interval where tide height >= safety depth."""

    start: datetime
    end: datetime
    start_height_m: float
    end_height_m: float
    min_height_m: float
    duration_seconds: int


@dataclass(frozen=True)
class UnknownInterval:
    """Part of the query range where no answer can be given."""

    start: datetime
    end: datetime
    reason: str
    duration_seconds: float


@dataclass(frozen=True)
class DepartureWindow:
    """Departure interval whose whole transit fits inside one safe window."""

    start: datetime
    end: datetime
    duration_seconds: int


@dataclass(frozen=True)
class NavigationResult:
    threshold_m: float
    max_gap_seconds: float
    windows: list[Window]
    unknown: list[UnknownInterval]
    # Only computed when a transit duration is supplied; None otherwise.
    departure_windows: list[DepartureWindow] | None = None


def to_epoch(t: datetime) -> float:
    """Convert a datetime to POSIX seconds; naive input is treated as UTC."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def to_datetime(x: float) -> datetime:
    return datetime.fromtimestamp(x, tz=timezone.utc)


def _split_chains(times: np.ndarray, max_gap: float) -> list[tuple[int, int]]:
    """Return [start_index, end_index] pairs of gap-delimited chains.

    A gap exceeding ``max_gap`` splits the chains: the knot on each side
    belongs only to its own chain, and no cubic piece ever bridges the gap.
    """
    chains: list[tuple[int, int]] = []
    start = 0
    gaps = np.diff(times)
    for i, gap in enumerate(gaps):
        if gap > max_gap:
            chains.append((start, i))  # indices start..i (inclusive)
            start = i + 1
    chains.append((start, len(times) - 1))
    return chains


def _windows_for_chain(
    pchip: PchipInterpolator,
    times: np.ndarray,
    heights: np.ndarray,
    i0: int,
    i1: int,
    query_start: float,
    query_end: float,
    threshold: float,
) -> list[Window]:
    """Find all safe windows within one continuous sample chain.

    PCHIP is shape preserving (it does not overshoot past data extrema),
    but a single cubic piece can still contain an interior extremum when
    the surrounding data are non-monotonic.  The event grid therefore
    consists of the raw knots *plus* every stationary point of the
    interpolant: between consecutive events the curve is monotonic, and a
    threshold crossing exists there iff the endpoint signs differ.

    Only the portion of the chain intersecting the query range is analysed;
    interpolation is never used outside the chain's sample coverage.
    """
    chain_lo = float(times[i0])
    chain_hi = float(times[i1])
    if max(chain_lo, query_start) >= min(chain_hi, query_end):
        return []

    def h(xx: float) -> float:
        return float(pchip(xx)) - threshold

    # Interior stationary points (local extrema) of the piecewise curve.
    try:
        stationary = list(np.atleast_1d(pchip.derivative().roots()))
    except ValueError:
        stationary = []
    event_set = {float(times[k]) for k in range(i0, i1 + 1)}
    for s in stationary:
        s = float(s)
        if chain_lo < s < chain_hi:
            event_set.add(s)
    # Restrict to the intersection of chain coverage and the query range:
    # insert both clipped boundaries so runs cannot extend past coverage.
    event_set.add(max(chain_lo, query_start))
    event_set.add(min(chain_hi, query_end))
    events = sorted(event_set)
    values = [h(x) for x in events]

    # Runs of events at/above the threshold mark safe stretches.
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for j, v in enumerate(values):
        if v >= 0.0:
            if run_start is None:
                run_start = j
        elif run_start is not None:
            runs.append((run_start, j - 1))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(values) - 1))

    out: list[Window] = []
    for rs, re_ in runs:
        a: float = events[rs]
        b: float = events[re_]
        # Refine the entry crossing in the flanking event interval (if any).
        if rs > 0 and values[rs - 1] < 0.0:
            a = brentq(h, events[rs - 1], events[rs],
                       xtol=_ROOT_XTOL, rtol=1e-12)
        # Refine the exit crossing in the flanking event interval (if any).
        if re_ < len(events) - 1 and values[re_ + 1] < 0.0:
            b = brentq(h, events[re_], events[re_ + 1],
                       xtol=_ROOT_XTOL, rtol=1e-12)

        # Clip the window to the requested range; no extrapolation.
        a = max(a, query_start)
        b = min(b, query_end)
        if b <= a:
            continue

        # Conservative rounding to whole seconds: shrink the window so
        # that every reported instant is guaranteed to satisfy the depth.
        a2 = float(math.ceil(a))
        b2 = float(math.floor(b))
        if a2 > b2:
            continue

        # Minimum height over the rounded window: on a monotonic piece the
        # minimum is at an endpoint, so evaluating the safe events (knots
        # and interior extrema) plus the rounded boundaries is sufficient.
        candidates = [
            values[k] + threshold
            for k in range(rs, re_ + 1)
            if a2 <= events[k] <= b2
        ]
        candidates.extend((h(a2) + threshold, h(b2) + threshold))

        out.append(
            Window(
                start=to_datetime(a2),
                end=to_datetime(b2),
                start_height_m=round(h(a2) + threshold, 3),
                end_height_m=round(h(b2) + threshold, 3),
                min_height_m=round(float(min(candidates)), 3),
                duration_seconds=int(b2 - a2),
            )
        )
    return out


def _append_unknown(
    intervals: list[UnknownInterval], start: float, end: float, reason: str
) -> None:
    if end - start > 0.0:
        intervals.append(
            UnknownInterval(
                start=to_datetime(start),
                end=to_datetime(end),
                reason=reason,
                duration_seconds=round(end - start, 3),
            )
        )


def _classify_unknown(
    intervals: list[UnknownInterval],
    start: float,
    end: float,
    first_time: float,
    last_time: float,
) -> None:
    """Register [start, end] as unknown, splitting on raw sample coverage.

    Portions within [first_time, last_time] are gaps where interpolation is
    forbidden because the spacing exceeds max_gap; portions outside are
    simply not covered by any sample.
    """
    if start < first_time:
        _append_unknown(intervals, start, min(end, first_time),
                        "no_sample_coverage")
    inner_lo = max(start, first_time)
    inner_hi = min(end, last_time)
    if inner_hi > inner_lo:
        _append_unknown(intervals, inner_lo, inner_hi,
                        "sample_gap_exceeds_max_gap")
    if end > last_time:
        _append_unknown(intervals, max(start, last_time), end,
                        "no_sample_coverage")


def _merge_intervals(
    intervals: Iterable[tuple[float, float]], lo: float, hi: float
) -> list[tuple[float, float]]:
    """Clip [start, end) intervals to [lo, hi] and merge overlaps/touches."""
    clipped = sorted(
        (max(s, lo), min(e, hi))
        for s, e in intervals
        if min(e, hi) > max(s, lo)
    )
    merged: list[list[float]] = []
    for s, e in clipped:
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def compute_departure_windows(
    windows: Sequence[Window],
    unknown: Sequence[UnknownInterval],
    query_start: datetime,
    query_end: datetime,
    transit_duration_seconds: float,
    maintenance_blocks: Sequence[tuple[datetime, datetime]] | None = None,
) -> list[DepartureWindow]:
    """Derive departure windows from the safe windows.

    A departure at ``t`` occupies ``[t, t + transit_duration_seconds)``; the
    occupancy must lie entirely within ONE safe window and must not intersect
    any maintenance closure or ``unknown`` stretch.  Closures are clipped to
    the query range and merged (overlapping or touching) together with the
    unknown intervals; each merged closure ``[s, e)`` then bars exactly the
    departures in the open interval ``(s - d, e)`` — equivalently, the
    closure is shifted earlier by the transit duration ``d`` and subtracted
    from the candidate departure intervals.  Because the occupancy is
    half-open, a departure exactly at ``s - d`` or ``e`` is still allowed.
    """
    d = float(transit_duration_seconds)
    qs = to_epoch(query_start)
    qe = to_epoch(query_end)

    sources: list[tuple[float, float]] = [
        (to_epoch(u.start), to_epoch(u.end)) for u in unknown
    ]
    if maintenance_blocks:
        sources.extend((to_epoch(s), to_epoch(e)) for s, e in maintenance_blocks)
    merged = _merge_intervals(sources, qs, qe)
    blocked = [(s - d, e) for s, e in merged]

    out: list[DepartureWindow] = []
    for w in windows:
        lo = to_epoch(w.start)
        hi = to_epoch(w.end) - d  # occupancy [t, t+d) must end by the window end
        if hi < lo:
            continue  # window shorter than the transit: no safe departure
        # Subtract the open blocked intervals from the closed candidate
        # interval [lo, hi]; the cut points themselves remain departable.
        pieces = [(lo, hi)]
        for bs, be in blocked:
            remaining: list[tuple[float, float]] = []
            for a, b in pieces:
                if be <= a or bs >= b:
                    remaining.append((a, b))
                    continue
                if a <= bs:
                    remaining.append((a, min(bs, b)))
                if be <= b:
                    remaining.append((max(be, a), b))
            pieces = remaining
            if not pieces:
                break
        for a, b in pieces:
            # Conservative rounding to whole seconds, as for the windows.
            a2 = float(math.ceil(a))
            b2 = float(math.floor(b))
            if a2 > b2:
                continue
            out.append(
                DepartureWindow(
                    start=to_datetime(a2),
                    end=to_datetime(b2),
                    duration_seconds=int(b2 - a2),
                )
            )
    return out


def compute_navigation_windows(
    sample_times: Sequence[datetime],
    sample_heights: Sequence[float],
    query_start: datetime,
    query_end: datetime,
    draft_m: float,
    clearance_m: float,
    max_gap_seconds: float,
    transit_duration_seconds: float | None = None,
    maintenance_blocks: Sequence[tuple[datetime, datetime]] | None = None,
) -> NavigationResult:
    """Compute all continuous safe navigation windows in the query range.

    ``draft_m + clearance_m`` is the safety threshold (same vertical datum
    and metres as the tide heights).  When ``transit_duration_seconds`` is
    given, departure windows are additionally derived from the safe windows
    (see :func:`compute_departure_windows`).
    """
    threshold = draft_m + clearance_m
    qs = to_epoch(query_start)
    qe = to_epoch(query_end)

    times = np.array([to_epoch(t) for t in sample_times], dtype=float)
    heights = np.asarray(sample_heights, dtype=float)

    first_time = float(times[0])
    last_time = float(times[-1])

    windows: list[Window] = []
    # Full (unclipped) intervals where interpolation is valid.
    chain_intervals: list[tuple[float, float]] = []
    chains = _split_chains(times, max_gap_seconds)

    for i0, i1 in chains:
        if i1 - i0 < 1:
            # A single isolated sample cannot be interpolated at all.
            continue
        chain_lo = float(times[i0])
        chain_hi = float(times[i1])
        chain_intervals.append((chain_lo, chain_hi))
        c_lo = max(chain_lo, qs)
        c_hi = min(chain_hi, qe)
        if c_hi <= c_lo:
            continue
        # Each chain gets its OWN interpolant: a gap larger than max_gap
        # must not be bridged by any cubic piece.
        chain_pchip = PchipInterpolator(
            times[i0 : i1 + 1], heights[i0 : i1 + 1], extrapolate=False
        )
        windows.extend(
            _windows_for_chain(
                chain_pchip, times, heights, i0, i1, qs, qe, threshold
            )
        )

    windows.sort(key=lambda w: w.start)

    # Build the unknown intervals: complement of the valid chain intervals
    # within the query range.  The part inside the raw sample span is
    # forbidden interpolation (over-large gaps); the part outside is plain
    # absence of samples (extrapolation is never performed).
    unknown: list[UnknownInterval] = []
    cursor = qs
    for chain_lo, chain_hi in chain_intervals:
        if chain_hi <= qs or chain_lo >= qe:
            continue
        cov_lo = max(chain_lo, qs)
        if cov_lo > cursor:
            _classify_unknown(unknown, cursor, cov_lo, first_time, last_time)
        cursor = max(cursor, min(chain_hi, qe))
    if cursor < qe:
        _classify_unknown(unknown, cursor, qe, first_time, last_time)

    departure_windows: list[DepartureWindow] | None = None
    if transit_duration_seconds is not None:
        departure_windows = compute_departure_windows(
            windows=windows,
            unknown=unknown,
            query_start=query_start,
            query_end=query_end,
            transit_duration_seconds=transit_duration_seconds,
            maintenance_blocks=maintenance_blocks,
        )

    return NavigationResult(
        threshold_m=threshold,
        max_gap_seconds=float(max_gap_seconds),
        windows=windows,
        unknown=unknown,
        departure_windows=departure_windows,
    )
