"""Tests for POST /sample-quality: service-level and end-to-end API checks."""

import math
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# Point the database at a throw-away file before importing the app.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TIDE_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.quality import audit_samples  # noqa: E402


def dt(hour: float, day: int = 21) -> datetime:
    return datetime(2026, 9, day, tzinfo=timezone.utc) + timedelta(
        seconds=hour * 3600
    )


def hourly(hours: list[int], day: int = 21) -> list[datetime]:
    return [dt(h, day) for h in hours]


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


def test_clean_linear_chain_is_ok():
    times = hourly(list(range(8)))
    heights = [1.0 + 0.5 * h for h in range(8)]
    report = audit_samples(times, heights, dt(0), dt(7), 3700)

    assert report.overall_status == "ok"
    assert report.sample_count == 8
    assert report.chain_count == 1
    assert report.gaps == []
    assert report.anomalies == []
    assert report.uncovered == []
    assert report.covered_seconds == 7 * 3600
    assert report.uncovered_seconds == 0.0


def test_smooth_tide_curve_has_no_false_positive():
    # The standard triangle tide shape is smooth: no jumps, no gaps.
    times = hourly(list(range(8)))
    heights = [1.0, 3.0, 6.0, 8.0, 8.0, 6.0, 3.0, 1.0]
    report = audit_samples(times, heights, dt(0), dt(7), 3700)
    assert report.overall_status == "ok"
    assert report.anomalies == []


def test_spike_is_flagged_with_original_values():
    times = hourly(list(range(8)))
    # Near-linear background (slopes ~0.5-0.6 m/h) with one sharp spike.
    heights = [1.0, 1.5, 2.1, 2.6, 8.0, 3.6, 4.2, 4.7]
    original = list(heights)
    report = audit_samples(times, heights, dt(0), dt(7), 3700)

    assert report.overall_status == "warn"
    assert len(report.anomalies) == 1
    a = report.anomalies[0]
    assert a.index == 4
    assert a.chain_index == 0
    # Reported verbatim: anomalies must never be silently corrected.
    assert a.time == times[4]
    assert a.height_m == 8.0
    assert heights == original  # input untouched
    # Entered via a steep rise, exited via a steep fall.
    assert a.slope_before > 0 and a.slope_after < 0
    assert a.slope_before == pytest.approx(5.4 / 3600, abs=1e-9)
    assert a.slope_after == pytest.approx(-4.4 / 3600, abs=1e-9)
    assert a.z_before is not None and abs(a.z_before) >= 3.5
    assert a.z_after is not None and abs(a.z_after) >= 3.5


def test_step_change_with_single_outlier_slope_is_not_flagged():
    # A one-time level jump (instrument reset) produces exactly one outlier
    # slope; the two knots around it each have one normal flank, so neither
    # is blamed as a jump anomaly.
    times = hourly(list(range(8)))
    heights = [1.0, 1.5, 2.0, 2.5, 3.0, 9.0, 9.5, 10.0]
    report = audit_samples(times, heights, dt(0), dt(7), 3700)
    assert report.anomalies == []


def test_zero_mad_flat_background_spike_is_flagged():
    times = hourly(list(range(7)))
    heights = [5.0, 5.0, 5.0, 9.0, 5.0, 5.0, 5.0]
    report = audit_samples(times, heights, dt(0), dt(6), 3700)

    assert report.overall_status == "warn"
    assert [a.index for a in report.anomalies] == [3]
    a = report.anomalies[0]
    # Degenerate (zero-MAD) scale: z-scores are reported as null, the
    # slopes still show the jump, and a perfectly flat chain scores nothing.
    assert a.z_before is None and a.z_after is None
    assert a.slope_before == pytest.approx(4.0 / 3600, abs=1e-9)
    assert a.slope_after == pytest.approx(-4.0 / 3600, abs=1e-9)

    flat = audit_samples(
        hourly(list(range(7))), [5.0] * 7, dt(0), dt(6), 3700
    )
    assert flat.anomalies == []
    assert flat.overall_status == "ok"


def test_gaps_split_chains_and_adjacent_breaks_merge():
    # 00:00-01:00, isolated 05:00, 10:00-11:00: two breaks (01->05, 05->10)
    # flank one isolated sample and touch at 05:00, so they merge into a
    # single gap 01:00 -> 10:00.
    times = hourly([0, 1, 5, 10, 11])
    heights = [8.0, 8.0, 8.0, 8.0, 8.0]
    report = audit_samples(times, heights, dt(0), dt(11), 3700)

    assert report.chain_count == 3
    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.start == dt(1)
    assert gap.end == dt(10)
    assert gap.duration_seconds == 9 * 3600
    assert gap.query_overlap is not None
    assert gap.query_overlap.start == dt(1)
    assert gap.query_overlap.end == dt(10)

    assert [(u.start, u.end, u.reason) for u in report.uncovered] == [
        (dt(1), dt(10), "sample_gap_exceeds_max_gap")
    ]
    assert report.covered_seconds == 2 * 3600
    assert report.uncovered_seconds == 9 * 3600
    assert report.overall_status == "warn"


def test_separate_gaps_do_not_merge():
    # Two breaks separated by a two-sample chain stay distinct.
    times = hourly([0, 1, 5, 6, 10, 11])
    heights = [8.0] * 6
    report = audit_samples(times, heights, dt(0), dt(11), 3700)
    assert report.chain_count == 3
    assert [(g.start, g.end) for g in report.gaps] == [(dt(1), dt(5)),
                                                       (dt(6), dt(10))]
    assert len(report.uncovered) == 2
    assert all(u.reason == "sample_gap_exceeds_max_gap"
               for u in report.uncovered)


def test_gap_outside_query_range_does_not_warn():
    times = hourly([0, 1, 5, 6, 7])
    heights = [8.0] * 5
    # Gap (01:00, 05:00) does not intersect the query [05:30, 07:00).
    report = audit_samples(times, heights, dt(5.5), dt(7), 3700)

    assert len(report.gaps) == 1
    assert report.gaps[0].query_overlap is None
    assert report.uncovered == []
    assert report.overall_status == "ok"
    assert report.covered_seconds == 1.5 * 3600


def test_gap_partially_intersecting_query_is_uncovered():
    times = hourly([0, 1, 5, 6])
    heights = [8.0] * 4
    # Gap (01:00, 05:00); query starts inside it.
    report = audit_samples(times, heights, dt(3), dt(6), 3700)

    assert report.overall_status == "warn"
    assert len(report.uncovered) == 1
    u = report.uncovered[0]
    assert (u.start, u.end) == (dt(3), dt(5))
    assert u.reason == "sample_gap_exceeds_max_gap"
    overlap = report.gaps[0].query_overlap
    assert (overlap.start, overlap.end) == (dt(3), dt(5))
    assert report.covered_seconds == 3600  # 05:00-06:00


def test_query_overhangs_report_no_sample_coverage():
    times = hourly([2, 3, 4, 5])
    heights = [8.0] * 4
    report = audit_samples(times, heights, dt(0), dt(7), 3700)

    assert report.overall_status == "warn"
    assert [(u.reason, u.start, u.end) for u in report.uncovered] == [
        ("no_sample_coverage", dt(0), dt(2)),
        ("no_sample_coverage", dt(5), dt(7)),
    ]
    assert report.covered_seconds == 3 * 3600
    assert report.uncovered_seconds == 4 * 3600
    assert report.gaps == []


def test_overhang_and_gap_segments_are_time_sorted():
    times = hourly([1, 2, 6, 7])
    heights = [8.0] * 4
    report = audit_samples(times, heights, dt(0), dt(9), 3700)

    assert [u.reason for u in report.uncovered] == [
        "no_sample_coverage",
        "sample_gap_exceeds_max_gap",
        "no_sample_coverage",
    ]
    starts = [u.start for u in report.uncovered]
    assert starts == sorted(starts)
    assert math.isclose(
        report.covered_seconds + report.uncovered_seconds, 9 * 3600
    )


def test_anomalies_are_detected_independently_per_chain():
    # Two chains separated by a 5-hour break; each contains one spike
    # within a flat background.
    times = hourly(list(range(0, 7)) + list(range(11, 18)))
    heights = [5.0, 5.0, 5.0, 9.0, 5.0, 5.0, 5.0] + \
              [2.0, 2.0, 2.0, 6.0, 2.0, 2.0, 2.0]
    report = audit_samples(times, heights, dt(0), dt(17), 3700)

    assert report.chain_count == 2
    assert [a.index for a in report.anomalies] == [3, 10]
    assert [a.chain_index for a in report.anomalies] == [0, 1]
    for a in report.anomalies:
        assert a.height_m == heights[a.index]


def test_two_sample_and_isolated_chains_have_no_anomalies():
    times = hourly([0, 1, 5])
    heights = [1.0, 9.0, 1.0]
    report = audit_samples(times, heights, dt(0), dt(5), 3700)
    # Chain of two samples and an isolated sample: no interior knot can be
    # entered and exited abnormally.
    assert report.chain_count == 2
    assert report.anomalies == []


# ---------------------------------------------------------------------------
# End-to-end API tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _payload():
    return {
        "samples": [
            {"time": f"2026-09-21T{h:02d}:00:00Z", "height_m": h_m}
            for h, h_m in [
                (0, 1.0), (1, 3.0), (2, 6.0), (3, 8.0),
                (4, 8.0), (5, 6.0), (6, 3.0), (7, 1.0),
            ]
        ],
        "query_start": "2026-09-21T00:00:00Z",
        "query_end": "2026-09-21T07:00:00Z",
        "max_gap_seconds": 3700,
    }


def test_api_clean_samples_ok(client):
    resp = client.post("/sample-quality", json=_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["overall_status"] == "ok"
    assert body["sample_count"] == 8
    assert body["chain_count"] == 1
    assert body["gaps"] == []
    assert body["anomalies"] == []
    assert body["uncovered"] == []
    assert body["covered_seconds"] == 7 * 3600
    assert body["uncovered_seconds"] == 0.0
    # Query bounds are echoed back.
    assert body["query_start"] == "2026-09-21T00:00:00Z"
    assert body["query_end"] == "2026-09-21T07:00:00Z"


def test_api_spike_reported_and_not_corrected(client):
    payload = _payload()
    # Near-linear background with one sharp spike at 04:00.
    payload["samples"] = [
        {"time": f"2026-09-21T{h:02d}:00:00Z", "height_m": h_m}
        for h, h_m in [
            (0, 1.0), (1, 1.5), (2, 2.1), (3, 2.6),
            (4, 8.0), (5, 3.6), (6, 4.2), (7, 4.7),
        ]
    ]
    resp = client.post("/sample-quality", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["overall_status"] == "warn"
    indices = [a["index"] for a in body["anomalies"]]
    assert indices == sorted(indices)
    assert len(body["anomalies"]) == 1
    anomaly = body["anomalies"][0]
    assert anomaly["index"] == 4
    # Original, uncorrected value returned with its location.
    assert anomaly["height_m"] == 8.0
    assert anomaly["time"] == "2026-09-21T04:00:00Z"
    assert abs(anomaly["z_before"]) >= 3.5
    assert abs(anomaly["z_after"]) >= 3.5


def test_api_merged_gaps_and_query_coverage(client):
    payload = _payload()
    payload["samples"] = [
        {"time": "2026-09-21T00:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T01:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T05:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T10:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T11:00:00Z", "height_m": 8.0},
    ]
    payload["query_end"] = "2026-09-21T11:00:00Z"
    resp = client.post("/sample-quality", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["overall_status"] == "warn"
    assert body["chain_count"] == 3
    # The two breaks around the isolated 05:00 sample merge into one gap.
    assert len(body["gaps"]) == 1
    gap = body["gaps"][0]
    assert gap["start"] == "2026-09-21T01:00:00Z"
    assert gap["end"] == "2026-09-21T10:00:00Z"
    assert gap["duration_seconds"] == 9 * 3600
    assert gap["query_overlap"]["start"] == "2026-09-21T01:00:00Z"
    assert gap["query_overlap"]["end"] == "2026-09-21T10:00:00Z"
    assert body["uncovered"][0]["reason"] == "sample_gap_exceeds_max_gap"
    assert body["covered_seconds"] == 2 * 3600
    assert body["uncovered_seconds"] == 9 * 3600


def test_api_query_overhang_warns(client):
    payload = _payload()
    payload["query_start"] = "2026-09-20T22:00:00Z"
    payload["query_end"] = "2026-09-21T09:00:00Z"
    body = client.post("/sample-quality", json=payload).json()

    assert body["overall_status"] == "warn"
    reasons = [(u["start"], u["end"], u["reason"]) for u in body["uncovered"]]
    assert reasons == [
        ("2026-09-20T22:00:00Z", "2026-09-21T00:00:00Z", "no_sample_coverage"),
        ("2026-09-21T07:00:00Z", "2026-09-21T09:00:00Z", "no_sample_coverage"),
    ]
    # No gaps between samples, and the gap list stays empty.
    assert body["gaps"] == []


def test_api_validation_errors(client):
    def expect_422(mutate, needle=""):
        payload = _payload()
        mutate(payload)
        resp = client.post("/sample-quality", json=payload)
        assert resp.status_code == 422, resp.text
        if needle:
            assert needle in resp.text

    expect_422(lambda p: p.__setitem__("samples", [p["samples"][0]]))
    expect_422(
        lambda p: p["samples"][3].__setitem__(
            "time", p["samples"][2]["time"]
        ),
        "递增",
    )
    expect_422(
        lambda p: p.__setitem__("query_end", p["query_start"]), "query_end"
    )
    expect_422(lambda p: p.__setitem__("max_gap_seconds", 0))
    # NaN is rejected at the schema layer (a raw NaN token cannot be
    # serialised back inside FastAPI's 422 detail, so assert on the model).
    from pydantic import ValidationError

    from app.schemas import SampleQualityRequest

    nan_payload = _payload()
    nan_payload["samples"][0]["height_m"] = float("nan")
    with pytest.raises(ValidationError) as exc:
        SampleQualityRequest.model_validate(nan_payload)
    assert "NaN" in str(exc.value)
    # The base payload itself is valid (guard against over-broad mutators).
    assert client.post("/sample-quality", json=_payload()).status_code == 200


def test_api_sample_quality_does_not_touch_tasks(client):
    # The audit endpoint is synchronous and persists nothing; task
    # creation/query behaviour is unchanged.
    task_payload = {
        **_payload(),
        "draft_m": 4.0,
        "clearance_m": 1.0,
    }
    resp = client.post("/tasks", json=task_payload)
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    got = client.get(f"/tasks/{task_id}")
    assert got.status_code == 200
    assert got.json()["status"] == "done"
    assert got.json()["result"]["unknown"] == []
