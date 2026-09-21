"""Sample quality audit: chain breaks, coverage gaps and spike anomalies.

This audit runs BEFORE any navigation-window computation and looks only at
the raw tide samples:

* adjacent samples spaced further than ``max_gap_seconds`` break the sampling
  chain — the same rule the window computation uses to forbid interpolation
  (:func:`app.services.tides._split_chains`);
* the parts of the query range lying outside the sample coverage are
  reported as coverage gaps (extrapolation is never performed);
* inside every continuous chain, isolated spikes are flagged from the median
  and the MAD (median absolute deviation) of the chain's adjacent slopes: a
  sample is anomalous only when BOTH its incoming and outgoing slopes are
  outliers (a spike must leave the chain's typical slope and come back; a
  single outlier slope is a level step and is not flagged, and chain
  endpoints with only one neighbour are never flagged).

Flagged samples are reported verbatim — never corrected, smoothed or removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import numpy as np

from app.services.tides import _split_chains, to_datetime, to_epoch

# Gap reasons, mirroring the ``unknown`` reasons of the window computation.
REASON_GAP = "sample_gap_exceeds_max_gap"
REASON_NO_COVERAGE = "no_sample_coverage"

# Iglewicz & Hoaglin's recommended cutoff for the modified z-score.
DEFAULT_SPIKE_Z_THRESHOLD = 3.5
# For normal data, MAD / 0.6745 estimates the standard deviation.
_MAD_TO_SIGMA = 0.6745


@dataclass(frozen=True)
class GapSegment:
    """Part of the query range without trustworthy interpolation."""

    start: datetime
    end: datetime
    reason: str
    duration_seconds: float


@dataclass(frozen=True)
class SampleAnomaly:
    """One sample flagged as an isolated spike (reported, never corrected)."""

    index: int
    time: datetime
    height_m: float
    chain_index: int
    robust_z: float | None  # None when the chain's slope MAD is zero
    slope_in_m_per_s: float
    slope_out_m_per_s: float
    median_slope_m_per_s: float
    mad_slope_m_per_s: float


@dataclass(frozen=True)
class QualityResult:
    overall_status: str  # "ok" | "warn"
    chain_count: int
    gaps: list[GapSegment]
    anomalies: list[SampleAnomaly]
    query_start_covered: bool
    query_end_covered: bool
    leading_gap_seconds: float
    trailing_gap_seconds: float


def _chain_anomalies(
    chain_index: int,
    i0: int,
    i1: int,
    times: np.ndarray,
    heights: np.ndarray,
    spike_z_threshold: float,
) -> list[SampleAnomaly]:
    """Flag isolated spikes inside one continuous chain (indices i0..i1).

    A sample is flagged only when BOTH adjacent slopes are outliers against
    the chain's median slope (robust z-score via MAD): a spike's incoming
    and outgoing slopes both deviate, while a genuine level step produces
    only one outlier slope and is left alone.  Chain endpoints have a single
    neighbour and are never flagged.  With fewer than three samples there is
    no interior sample to judge.
    """
    n = i1 - i0 + 1
    if n < 3:
        return []
    t = times[i0 : i1 + 1]
    h = heights[i0 : i1 + 1]
    slopes = np.diff(h) / np.diff(t)
    median = float(np.median(slopes))
    mad = float(np.median(np.abs(slopes - median)))

    if mad > 0.0:
        z = _MAD_TO_SIGMA * np.abs(slopes - median) / mad
        outliers = z > spike_z_threshold
    else:
        # Zero MAD: more than half the slopes equal the median exactly, so
        # ANY deviation is suspicious; the robust z-score is then undefined.
        z = None
        tol = 1e-9 * max(1.0, abs(median))
        outliers = np.abs(slopes - median) > tol

    out: list[SampleAnomaly] = []
    for j in range(1, n - 1):
        if not (outliers[j - 1] and outliers[j]):
            continue
        robust_z = None if z is None else round(float(min(z[j - 1], z[j])), 3)
        out.append(
            SampleAnomaly(
                index=i0 + j,
                time=to_datetime(float(t[j])),
                height_m=float(h[j]),
                chain_index=chain_index,
                robust_z=robust_z,
                slope_in_m_per_s=float(slopes[j - 1]),
                slope_out_m_per_s=float(slopes[j]),
                median_slope_m_per_s=median,
                mad_slope_m_per_s=mad,
            )
        )
    return out


def audit_sample_quality(
    sample_times: Sequence[datetime],
    sample_heights: Sequence[float],
    query_start: datetime,
    query_end: datetime,
    max_gap_seconds: float,
    spike_z_threshold: float = DEFAULT_SPIKE_Z_THRESHOLD,
) -> QualityResult:
    """Audit tide samples before a navigation-window computation.

    Gap segments are clipped to the query range, sorted by start, and
    touching segments with the same reason are merged.  Anomalies are a
    property of the samples themselves and are detected over ALL chains,
    independent of the query range.  Nothing is corrected: flagged samples
    come back exactly as supplied.
    """
    times = np.array([to_epoch(t) for t in sample_times], dtype=float)
    heights = np.asarray(sample_heights, dtype=float)
    qs = to_epoch(query_start)
    qe = to_epoch(query_end)

    chains = _split_chains(times, max_gap_seconds)

    anomalies: list[SampleAnomaly] = []
    for chain_index, (i0, i1) in enumerate(chains):
        anomalies.extend(
            _chain_anomalies(
                chain_index, i0, i1, times, heights, spike_z_threshold
            )
        )

    # Gap segments inside the query range: forbidden interpolation between
    # over-spaced samples, plus the stretches outside the sample coverage.
    first_time = float(times[0])
    last_time = float(times[-1])
    raw: list[tuple[float, float, str]] = []
    if qs < first_time:
        raw.append((qs, min(qe, first_time), REASON_NO_COVERAGE))
    for i in range(len(times) - 1):
        if times[i + 1] - times[i] > max_gap_seconds:
            a = max(float(times[i]), qs)
            b = min(float(times[i + 1]), qe)
            if b > a:
                raw.append((a, b, REASON_GAP))
    if qe > last_time:
        raw.append((max(qs, last_time), qe, REASON_NO_COVERAGE))

    # Sort by start and merge touching segments that carry the same reason
    # (e.g. consecutive over-gap intervals sharing one sample).
    raw.sort(key=lambda seg: (seg[0], seg[1]))
    merged: list[list] = []
    for a, b, reason in raw:
        if merged and merged[-1][2] == reason and a <= merged[-1][1]:
            if b > merged[-1][1]:
                merged[-1][1] = b
        else:
            merged.append([a, b, reason])
    gaps = [
        GapSegment(
            start=to_datetime(a),
            end=to_datetime(b),
            reason=reason,
            duration_seconds=round(b - a, 3),
        )
        for a, b, reason in merged
    ]

    leading = min(qe, first_time) - qs if qs < first_time else 0.0
    trailing = qe - max(qs, last_time) if qe > last_time else 0.0

    return QualityResult(
        overall_status="warn" if gaps or anomalies else "ok",
        chain_count=len(chains),
        gaps=gaps,
        anomalies=anomalies,
        query_start_covered=qs >= first_time,
        query_end_covered=qe <= last_time,
        leading_gap_seconds=round(leading, 3),
        trailing_gap_seconds=round(trailing, 3),
    )
