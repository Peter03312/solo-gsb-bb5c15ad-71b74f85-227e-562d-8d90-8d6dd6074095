"""API tests for POST /sample-quality (pre-computation sample audit)."""

import os
import tempfile

import pytest
from pydantic import ValidationError

# Point the database at a throw-away file before importing the app.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TIDE_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.schemas import SampleQualityRequest  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _hourly_payload(heights, max_gap_seconds=3700):
    return {
        "samples": [
            {"time": f"2026-09-21T{h:02d}:00:00Z", "height_m": h_m}
            for h, h_m in enumerate(heights)
        ],
        "query_start": "2026-09-21T00:00:00Z",
        "query_end": f"2026-09-21T{len(heights) - 1:02d}:00:00Z",
        "max_gap_seconds": max_gap_seconds,
    }


def _payload():
    # Smooth tide-like curve; nothing should be flagged.
    return _hourly_payload(
        [2.0, 2.8, 3.4, 3.8, 4.0, 4.0, 3.6, 3.0, 2.4, 2.0]
    )


def test_clean_samples_ok(client):
    resp = client.post("/sample-quality", json=_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["overall_status"] == "ok"
    assert body["sample_count"] == 10
    assert body["chain_count"] == 1
    assert body["gaps"] == []
    assert body["anomalous_indices"] == []
    assert body["anomalies"] == []
    cov = body["coverage"]
    assert cov["query_start_covered"] is True
    assert cov["query_end_covered"] is True
    assert cov["leading_gap_seconds"] == 0.0
    assert cov["trailing_gap_seconds"] == 0.0
    assert cov["first_sample_time"] == "2026-09-21T00:00:00Z"
    assert cov["last_sample_time"] == "2026-09-21T09:00:00Z"
    assert body["notes"] == ["样本链完整覆盖查询范围，未发现断链或突变异常"]


def test_spike_flagged_and_never_corrected(client):
    payload = _payload()
    payload["samples"][4]["height_m"] = 9.5  # isolated spike at 04:00
    resp = client.post("/sample-quality", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["overall_status"] == "warn"
    assert body["gaps"] == []  # sampling itself is gap-free
    assert body["anomalous_indices"] == [4]
    assert len(body["anomalies"]) == 1
    a = body["anomalies"][0]
    assert a["index"] == 4
    assert a["time"] == "2026-09-21T04:00:00Z"
    # The original height comes back verbatim: reported, never corrected.
    assert a["height_m"] == 9.5
    assert a["chain_index"] == 0
    # Both adjacent slopes are outliers; robust z is the weaker of the two.
    assert a["robust_z"] == pytest.approx(4.3, abs=1e-3)
    assert a["slope_in_m_per_s"] == pytest.approx(5.7 / 3600)
    assert a["slope_out_m_per_s"] == pytest.approx(-5.5 / 3600)
    assert a["mad_slope_m_per_s"] > 0
    assert any("突变" in n and "不做任何修正" in n for n in body["notes"])


def test_spike_threshold_is_configurable(client):
    payload = _payload()
    payload["samples"][4]["height_m"] = 9.5
    payload["spike_z_threshold"] = 100.0  # too strict: nothing flagged
    body = client.post("/sample-quality", json=payload).json()
    assert body["overall_status"] == "ok"
    assert body["anomalous_indices"] == []
    assert body["spike_z_threshold"] == 100.0


def test_multiple_spikes_sorted(client):
    payload = _hourly_payload([1, 1, 9, 1, 1, 1, 1, 1, 9, 1, 1])
    body = client.post("/sample-quality", json=payload).json()
    assert body["anomalous_indices"] == [2, 8]
    assert [a["index"] for a in body["anomalies"]] == [2, 8]
    # Flat chain => slope MAD is zero => robust z is undefined (null).
    assert all(a["robust_z"] is None for a in body["anomalies"])


def test_zero_mad_single_spike(client):
    payload = _hourly_payload([1, 1, 1, 1, 8, 1, 1, 1, 1])
    body = client.post("/sample-quality", json=payload).json()
    assert body["overall_status"] == "warn"
    assert body["anomalous_indices"] == [4]
    assert body["anomalies"][0]["robust_z"] is None
    assert body["anomalies"][0]["mad_slope_m_per_s"] == 0.0


def test_level_step_is_not_a_spike(client):
    # A genuine level shift produces ONE outlier slope only: not flagged.
    payload = _hourly_payload([1, 1, 1, 5, 5, 5, 5])
    body = client.post("/sample-quality", json=payload).json()
    assert body["overall_status"] == "ok"
    assert body["anomalous_indices"] == []


def test_chain_endpoints_never_flagged(client):
    # Spikes sitting at chain boundaries have a single neighbour: not flagged.
    payload = {
        "samples": [
            {"time": "2026-09-21T00:00:00Z", "height_m": 1.0},
            {"time": "2026-09-21T01:00:00Z", "height_m": 1.0},
            {"time": "2026-09-21T02:00:00Z", "height_m": 1.0},
            {"time": "2026-09-21T03:00:00Z", "height_m": 1.0},
            {"time": "2026-09-21T04:00:00Z", "height_m": 9.0},  # chain 1 tail
            {"time": "2026-09-21T06:00:00Z", "height_m": 9.0},  # chain 2 head
            {"time": "2026-09-21T07:00:00Z", "height_m": 1.0},
            {"time": "2026-09-21T08:00:00Z", "height_m": 1.0},
            {"time": "2026-09-21T09:00:00Z", "height_m": 1.0},
            {"time": "2026-09-21T10:00:00Z", "height_m": 1.0},
        ],
        "query_start": "2026-09-21T00:00:00Z",
        "query_end": "2026-09-21T10:00:00Z",
        "max_gap_seconds": 3700,
    }
    body = client.post("/sample-quality", json=payload).json()
    assert body["chain_count"] == 2
    assert body["anomalous_indices"] == []
    # The gap between the chains is still reported.
    assert [g["reason"] for g in body["gaps"]] == ["sample_gap_exceeds_max_gap"]
    assert body["gaps"][0]["start"] == "2026-09-21T04:00:00Z"
    assert body["gaps"][0]["end"] == "2026-09-21T06:00:00Z"


def test_consecutive_gaps_merge_into_one_segment(client):
    # Every adjacent pair exceeds max_gap: 4 singleton chains, one merged gap.
    payload = _hourly_payload([2.0, 2.0, 2.0, 2.0], max_gap_seconds=1800)
    body = client.post("/sample-quality", json=payload).json()
    assert body["overall_status"] == "warn"
    assert body["chain_count"] == 4
    assert body["gaps"] == [
        {
            "start": "2026-09-21T00:00:00Z",
            "end": "2026-09-21T03:00:00Z",
            "reason": "sample_gap_exceeds_max_gap",
            "duration_seconds": 10800.0,
        }
    ]


def test_coverage_gaps_at_query_boundaries(client):
    payload = {
        "samples": [
            {"time": "2026-09-21T02:00:00Z", "height_m": 2.0},
            {"time": "2026-09-21T03:00:00Z", "height_m": 3.0},
        ],
        "query_start": "2026-09-21T00:00:00Z",
        "query_end": "2026-09-21T05:00:00Z",
        "max_gap_seconds": 3700,
    }
    body = client.post("/sample-quality", json=payload).json()
    assert body["overall_status"] == "warn"
    assert [(g["start"], g["end"], g["reason"]) for g in body["gaps"]] == [
        ("2026-09-21T00:00:00Z", "2026-09-21T02:00:00Z", "no_sample_coverage"),
        ("2026-09-21T03:00:00Z", "2026-09-21T05:00:00Z", "no_sample_coverage"),
    ]
    cov = body["coverage"]
    assert cov["query_start_covered"] is False
    assert cov["query_end_covered"] is False
    assert cov["leading_gap_seconds"] == 7200.0
    assert cov["trailing_gap_seconds"] == 7200.0
    assert any("query_start" in n for n in body["notes"])
    assert any("query_end" in n for n in body["notes"])


def test_adjacent_segments_with_different_reasons_not_merged(client):
    payload = {
        "samples": [
            {"time": "2026-09-21T01:00:00Z", "height_m": 2.0},
            {"time": "2026-09-21T03:00:00Z", "height_m": 3.0},
        ],
        "query_start": "2026-09-21T00:00:00Z",
        "query_end": "2026-09-21T04:00:00Z",
        "max_gap_seconds": 1800,
    }
    body = client.post("/sample-quality", json=payload).json()
    # Touching segments keep their own reasons: coverage / break / coverage.
    assert [(g["start"], g["end"], g["reason"]) for g in body["gaps"]] == [
        ("2026-09-21T00:00:00Z", "2026-09-21T01:00:00Z", "no_sample_coverage"),
        ("2026-09-21T01:00:00Z", "2026-09-21T03:00:00Z",
         "sample_gap_exceeds_max_gap"),
        ("2026-09-21T03:00:00Z", "2026-09-21T04:00:00Z", "no_sample_coverage"),
    ]


def test_gap_clipped_to_query_range(client):
    payload = {
        "samples": [
            {"time": f"2026-09-21T{h:02d}:00:00Z", "height_m": 2.0}
            for h in (0, 1, 4, 5)
        ],
        "query_start": "2026-09-21T02:00:00Z",
        "query_end": "2026-09-21T03:00:00Z",
        "max_gap_seconds": 3700,
    }
    body = client.post("/sample-quality", json=payload).json()
    # The 01:00-04:00 break is clipped to the query range.
    assert body["gaps"] == [
        {
            "start": "2026-09-21T02:00:00Z",
            "end": "2026-09-21T03:00:00Z",
            "reason": "sample_gap_exceeds_max_gap",
            "duration_seconds": 3600.0,
        }
    ]
    assert body["coverage"]["query_start_covered"] is True
    assert body["coverage"]["query_end_covered"] is True


def test_validation_errors(client):
    base = _payload()

    def expect_422(mutate, needle=None):
        payload = _payload()
        mutate(payload)
        resp = client.post("/sample-quality", json=payload)
        assert resp.status_code == 422, resp.text
        if needle is not None:
            assert needle in resp.text

    expect_422(lambda p: p["samples"][1].__setitem__(
        "time", p["samples"][0]["time"]), "递增")
    expect_422(lambda p: p.__setitem__("query_end", p["query_start"]))
    expect_422(lambda p: p.__setitem__("max_gap_seconds", 0))
    expect_422(lambda p: p.__setitem__("max_gap_seconds", -5))
    expect_422(lambda p: p.__setitem__("spike_z_threshold", 0))
    expect_422(lambda p: p.__setitem__("spike_z_threshold", -1))
    expect_422(lambda p: p["samples"].__delitem__(slice(1, None)))  # 1 sample
    # The base payload itself is valid (guard against over-broad mutators).
    assert client.post("/sample-quality", json=base).status_code == 200


def test_validation_rejects_nan_height():
    # The schema layer rejects NaN heights (same as WindowRequest).  Tested at
    # the model layer: over HTTP the 422 error echo itself cannot serialise
    # the NaN input — a pre-existing quirk shared with POST /tasks.
    payload = _payload()
    payload["samples"][2]["height_m"] = float("nan")
    with pytest.raises(ValidationError, match="NaN"):
        SampleQualityRequest.model_validate(payload)


def test_existing_task_endpoints_unaffected(client):
    # The audit endpoint is additive: /tasks still works exactly as before.
    resp = client.post("/tasks", json={
        "samples": [
            {"time": "2026-09-21T00:00:00Z", "height_m": 1.0},
            {"time": "2026-09-21T01:00:00Z", "height_m": 6.0},
            {"time": "2026-09-21T02:00:00Z", "height_m": 1.0},
        ],
        "query_start": "2026-09-21T00:00:00Z",
        "query_end": "2026-09-21T02:00:00Z",
        "draft_m": 4.0,
        "clearance_m": 1.0,
        "max_gap_seconds": 3700,
    })
    assert resp.status_code == 201, resp.text
    got = client.get(f"/tasks/{resp.json()['task_id']}")
    assert got.status_code == 200
    assert got.json()["status"] == "done"
    assert got.json()["result"]["threshold_m"] == 5.0
