"""Audit a batch of tide samples before navigation windows are computed.

Two independent checks are performed on samples assumed already sorted in
strictly increasing time order (the schema layer enforces that):

1. **Chain breaks.** Every adjacent spacing strictly greater than
   ``max_gap_seconds`` is a break; the affected open interval ``(t_i, t_j)``
   is a sampling gap.  Breaks are reported time-sorted and neighbouring
   breaks (two consecutive breaks leave a single isolated sample between
   them, so the two open intervals touch at that sample instant) are merged
   into one gap segment.  Each gap also reports its intersection with the
   query range.

2. **Jump anomalies.** Within every contiguous chain, the adjacent slopes
   ``(h[i+1] - h[i]) / (t[i+1] - t[i])`` are scored with a robust modified
   z-score based on the chain's median slope and the median absolute
   deviation (MAD, scaled by 1.4826 so it is consistent with the standard
   deviation of a normal sample).  A slope with ``|z| >= 3.5`` is an outlier;
   an interior knot is flagged as an anomaly only when BOTH flanking slopes
   are outliers — a sharp spike/dip must be entered and exited abnormally,
   so a single outlier slope (e.g. a step jump at the end of the chain) is
   not blamed on a normal point.

   When the MAD degenerates to zero (slopes taking only a few discrete
   values, as with flat synthetic input), the scale is taken to be zero
   instead: any slope different from the median slope is then an outlier,
   while slopes equal to the median score exactly zero.  This still never
   flags points of a perfectly constant-slope chain.

   Very short chains (four or fewer slopes) give an unstable MAD: an odd
   jump can then stay below the 3.5 threshold.  This is an inherent
   small-sample limitation, not a correction — nothing is silently dropped.

Anomalous samples are **never corrected or filtered**: their original index
(0-based, into the request array), time and height are returned verbatim
together with the diagnostic slopes/z-scores, so callers decide what to do.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from app.services.tides import _split_chains, to_datetime, to_epoch

# Modified z-score rejection threshold (Iglewicz & Hoaglin, 1993).
_Z_LIMIT = 3.5
_MAD_SCALE = 1.4826


@dataclass(frozen=True)
class GapQueryOverlap:
    """Intersection of one gap segment with the audited query range."""

    start: datetime
    end: datetime
    duration_seconds: float


@dataclass(frozen=True)
class GapSegment:
    """One merged sampling gap ``(end of a chain, start of the next)``."""

    start: datetime
    end: datetime
    duration_seconds: float
    query_overlap: GapQueryOverlap | None


@dataclass(frozen=True)
class SampleAnomaly:
    """One flagged sample; values are reported, never modified."""

    index: int
    time: datetime
    height_m: float
    chain_index: int
    slope_before: float
    slope_after: float
    z_before: float | None
    z_after: float | None


@dataclass(frozen=True)
class UncoveredSegment:
    """Part of the query range not covered by valid sample chains."""

    start: datetime
    end: datetime
    reason: str
    duration_seconds: float


@dataclass(frozen=True)
class SampleQualityReport:
    sample_count: int
    chain_count: int
    gaps: list[GapSegment]
    anomalies: list[SampleAnomaly]
    query_start: datetime
    query_end: datetime
    uncovered: list[UncoveredSegment]
    covered_seconds: float
    uncovered_seconds: float
    overall_status: str


def _robust_slope_scores(slopes: np.ndarray) -> np.ndarray:
    """Modified z-scores of the slopes within one continuous chain.

    With a positive MAD the usual ``(slope - median) / (1.4826*MAD)`` score
    is returned.  With a zero MAD every slope unequal to the median diverges
    and is represented by ``+/-inf``; a perfectly constant chain (all slopes
    equal) scores all zeros, so nothing can ever be flagged there.
    """
    median = float(np.median(slopes))
    mad = float(np.median(np.abs(slopes - median)))
    scale = _MAD_SCALE * mad
    if scale > 0.0:
        return (slopes - median) / scale
    # Zero MAD: either all slopes coincide (nothing anomalous) or the
    # deviations are discrete (nonzero deviation is unambiguously strange).
    if np.all(slopes == median):
        return np.zeros_like(slopes)
    return np.where(
        slopes == median,
        0.0,
        np.where(slopes > median, np.inf, -np.inf),
    )


def _is_outlier(z: float) -> bool:
    if math.isnan(z):  # no statistic available: not an outlier
        return False
    return math.isinf(z) or abs(z) >= _Z_LIMIT


def _z_or_none(value: float) -> float | None:
    """Round a robust z-score; the degenerate +/-inf scale maps to null."""
    if math.isnan(value) or math.isinf(value):
        return None
    return round(value, 3)


def _audit_chain(
    times: np.ndarray,
    heights: np.ndarray,
    i0: int,
    i1: int,
    chain_index: int,
) -> list[SampleAnomaly]:
    """Flag jump anomalies inside samples ``i0..i1`` (inclusive)."""
    if i1 - i0 < 2:
        # Fewer than three points: fewer than two slopes, no interior knot
        # can be entered AND exited abnormally.
        return []
    dt_seg = np.diff(times[i0 : i1 + 1])
    slopes = np.diff(heights[i0 : i1 + 1]) / dt_seg
    z = _robust_slope_scores(slopes)

    anomalies: list[SampleAnomaly] = []
    # Interior knot k (global index) is flanked by local slopes k-i0-1 and
    # k-i0; both must be outliers to blame the knot itself.
    for local in range(1, i1 - i0):
        zb = float(z[local - 1])
        za = float(z[local])
        if _is_outlier(zb) and _is_outlier(za):
            k = i0 + local
            anomalies.append(
                SampleAnomaly(
                    index=k,
                    time=to_datetime(float(times[k])),
                    height_m=float(heights[k]),
                    chain_index=chain_index,
                    slope_before=round(float(slopes[local - 1]), 9),
                    slope_after=round(float(slopes[local]), 9),
                    z_before=_z_or_none(zb),
                    z_after=_z_or_none(za),
                )
            )
    return anomalies


def audit_samples(
    sample_times: Sequence[datetime],
    sample_heights: Sequence[float],
    query_start: datetime,
    query_end: datetime,
    max_gap_seconds: float,
) -> SampleQualityReport:
    """Audit chronologically ordered tide samples (see module docstring)."""
    times = np.array([to_epoch(t) for t in sample_times], dtype=float)
    heights = np.asarray(sample_heights, dtype=float)
    qs = to_epoch(query_start)
    qe = to_epoch(query_end)

    chains = _split_chains(times, max_gap_seconds)

    # ---- Gaps: raw open intervals (t_i, t_{i+1}) at every chain break,
    # merged when they touch (two breaks flanking one isolated sample).
    raw_gaps: list[tuple[float, float]] = []
    for c in range(len(chains) - 1):
        end_of_chain = float(times[chains[c][1]])
        start_of_next = float(times[chains[c + 1][0]])
        raw_gaps.append((end_of_chain, start_of_next))
    raw_gaps.sort()

    merged_gaps: list[list[float]] = []
    for s, e in raw_gaps:
        if merged_gaps and s <= merged_gaps[-1][1]:
            if e > merged_gaps[-1][1]:
                merged_gaps[-1][1] = e
        else:
            merged_gaps.append([s, e])

    gap_segments: list[GapSegment] = []
    for s, e in merged_gaps:
        overlap_lo = max(s, qs)
        overlap_hi = min(e, qe)
        overlap = (
            GapQueryOverlap(
                start=to_datetime(overlap_lo),
                end=to_datetime(overlap_hi),
                duration_seconds=round(overlap_hi - overlap_lo, 3),
            )
            if overlap_hi > overlap_lo
            else None
        )
        gap_segments.append(
            GapSegment(
                start=to_datetime(s),
                end=to_datetime(e),
                duration_seconds=round(e - s, 3),
                query_overlap=overlap,
            )
        )

    # ---- Jump anomalies within each chain independently.
    anomalies: list[SampleAnomaly] = []
    for chain_index, (i0, i1) in enumerate(chains):
        anomalies.extend(_audit_chain(times, heights, i0, i1, chain_index))
    anomalies.sort(key=lambda a: a.index)

    # ---- Coverage of the query range: portions never reachable by
    # interpolation (gaps inside the sample span + overhangs outside it).
    uncovered_raw: list[tuple[float, float, str]] = []
    first_time = float(times[0])
    last_time = float(times[-1])
    if qs < first_time:
        uncovered_raw.append(
            (qs, min(qe, first_time), "no_sample_coverage")
        )
    for s, e in merged_gaps:
        lo = max(s, qs)
        hi = min(e, qe)
        if hi > lo:
            uncovered_raw.append((lo, hi, "sample_gap_exceeds_max_gap"))
    if qe > last_time:
        uncovered_raw.append(
            (max(qs, last_time), qe, "no_sample_coverage")
        )

    uncovered_raw.sort(key=lambda x: (x[0], x[1]))
    uncovered: list[UncoveredSegment] = []
    total_uncovered = 0.0
    for s, e, reason in uncovered_raw:
        uncovered.append(
            UncoveredSegment(
                start=to_datetime(s),
                end=to_datetime(e),
                reason=reason,
                duration_seconds=round(e - s, 3),
            )
        )
        total_uncovered += e - s
    query_span = qe - qs
    covered_seconds = max(0.0, query_span - total_uncovered)

    overall_status = "warn" if (anomalies or uncovered) else "ok"

    return SampleQualityReport(
        sample_count=len(times),
        chain_count=len(chains),
        gaps=gap_segments,
        anomalies=anomalies,
        query_start=query_start,
        query_end=query_end,
        uncovered=uncovered,
        covered_seconds=round(covered_seconds, 3),
        uncovered_seconds=round(total_uncovered, 3),
        overall_status=overall_status,
    )
