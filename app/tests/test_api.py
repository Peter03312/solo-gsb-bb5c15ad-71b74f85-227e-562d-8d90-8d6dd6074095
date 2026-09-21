import os
import tempfile
from datetime import datetime, timedelta

import pytest

# Point the database at a throw-away file before importing the app.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TIDE_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


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
        "draft_m": 4.0,
        "clearance_m": 1.0,
        "max_gap_seconds": 3700,
    }


def test_create_and_get_task(client):
    resp = client.post("/tasks", json=_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    task_id = body["task_id"]
    assert body["status"] in {"pending", "done"}
    assert body["request"]["draft_m"] == 4.0

    got = client.get(f"/tasks/{task_id}")
    assert got.status_code == 200
    result = got.json()["result"]
    assert got.json()["status"] == "done"
    assert result["threshold_m"] == 5.0
    assert result["unknown"] == []
    assert len(result["windows"]) == 1
    w = result["windows"][0]
    # Threshold crossings lie inside (01:00,02:00) and (05:00,06:00).
    assert w["start"].startswith("2026-09-21T01:")
    assert w["end"].startswith("2026-09-21T05:")
    assert w["min_height_m"] >= 5.0


def test_task_not_found(client):
    assert client.get("/tasks/doesnotexist").status_code == 404


def test_validation_rejects_non_increasing(client):
    payload = _payload()
    payload["samples"][3]["time"] = payload["samples"][2]["time"]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 422
    assert "递增" in resp.text


def test_gap_returns_unknown(client):
    payload = _payload()
    payload["samples"] = [
        {"time": "2026-09-21T00:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T01:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T05:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T06:00:00Z", "height_m": 8.0},
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]
    assert len(result["windows"]) == 2
    reasons = {u["reason"] for u in result["unknown"]}
    assert "sample_gap_exceeds_max_gap" in reasons


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_departure_windows_end_to_end(client):
    payload = _payload()
    payload["transit_duration_seconds"] = 1800
    payload["maintenance_blocks"] = [
        {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]

    assert len(result["windows"]) == 1
    w = result["windows"][0]
    deps = result["departure_windows"]
    assert deps is not None and len(deps) == 2
    # Closure [03:00, 04:00) shifted back by the 30 min transit bars
    # departures in (02:30, 04:00); the cut points remain departable.
    assert deps[0]["start"] == w["start"]
    assert deps[0]["end"] == "2026-09-21T02:30:00Z"
    assert deps[1]["start"] == "2026-09-21T04:00:00Z"
    # Every departure's occupancy [t, t+1800) fits inside the safe window.
    assert _parse(deps[1]["end"]) == _parse(w["end"]) - timedelta(seconds=1800)
    assert deps[0]["duration_seconds"] == int(
        (_parse(deps[0]["end"]) - _parse(deps[0]["start"])).total_seconds()
    )


def test_departure_windows_omitted_by_default(client):
    resp = client.post("/tasks", json=_payload())
    assert resp.status_code == 201, resp.text
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]
    assert result["departure_windows"] is None


def test_maintenance_requires_transit_duration(client):
    payload = _payload()
    payload["maintenance_blocks"] = [
        {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 422
    assert "transit_duration_seconds" in resp.text


def test_maintenance_block_end_after_start(client):
    payload = _payload()
    payload["transit_duration_seconds"] = 1800
    payload["maintenance_blocks"] = [
        {"start": "2026-09-21T04:00:00Z", "end": "2026-09-21T03:00:00Z"}
    ]
    assert client.post("/tasks", json=payload).status_code == 422


# ---------------------------------------------------------------------------
# Fleet coordination tasks: POST /fleet-tasks + GET /fleet-tasks/{id}
# ---------------------------------------------------------------------------


def _fleet_payload():
    return {
        "ships": [
            {"name": "甲轮", "draft_m": 4.0, "clearance_m": 1.0,
             "transit_duration_seconds": 1800},
            {"name": "乙轮", "draft_m": 4.0, "clearance_m": 1.0,
             "transit_duration_seconds": 3600},
        ],
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
        "maintenance_blocks": [
            {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
        ],
    }


def _poll_fleet(client, payload):
    resp = client.post("/fleet-tasks", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] in {"pending", "done"}
    got = client.get(f"/fleet-tasks/{body['task_id']}")
    assert got.status_code == 200
    got_body = got.json()
    assert got_body["status"] == "done", got_body
    return got_body


def test_fleet_create_and_get_task(client):
    body = _poll_fleet(client, _fleet_payload())
    result = body["result"]

    # Per-ship results: two ships, request order preserved.
    assert [s["name"] for s in result["ships"]] == ["甲轮", "乙轮"]
    assert all(s["threshold_m"] == 5.0 for s in result["ships"])
    assert all(len(s["windows"]) == 1 for s in result["ships"])

    # Common departure windows: intersection of both ships' windows, i.e.
    # A's windows shortened by B's longer transit.
    common = result["common_departure_windows"]
    assert [(d["start"], d["end"]) for d in common] == [
        ("2026-09-21T01:39:11Z", "2026-09-21T02:00:00Z"),
        ("2026-09-21T04:00:00Z", "2026-09-21T04:20:49Z"),
    ]
    for d in common:
        assert d["duration_seconds"] == int(
            (_parse(d["end"]) - _parse(d["start"])).total_seconds()
        )


def test_fleet_exclusions_identify_ships_and_fleet_constraints(client):
    result = _poll_fleet(client, _fleet_payload())["result"]
    by_reason = {}
    for ex in result["exclusions"]:
        by_reason.setdefault(ex["reason"], []).append(ex)

    # Closure [03:00,04:00) shifted by the LONGEST transit (1 h) bars fleet
    # departures on (02:00, 04:00); it is a fleet-wide exclusion with an empty
    # ship list (cut points 02:00:00 and 04:00:00 remain departable).
    closure = by_reason["maintenance_closure"]
    assert len(closure) == 1
    assert closure[0]["ships"] == []
    assert closure[0]["start"] == "2026-09-21T02:00:01Z"
    assert closure[0]["end"] == "2026-09-21T03:59:59Z"

    # B's longer transit rules out the tail of the second window; the first
    # window is shared because 30 min fits both transits.
    ship_excl = by_reason["ship_constraint"]
    assert ship_excl and all(e["ships"] == ["乙轮"] for e in ship_excl)
    assert ship_excl[0]["start"] == "2026-09-21T04:20:50Z"
    assert ship_excl[0]["end"] == "2026-09-21T04:50:49Z"


def test_fleet_task_not_found(client):
    assert client.get("/fleet-tasks/doesnotexist").status_code == 404


def test_fleet_unknown_sample_segment_is_fleet_constraint(client):
    payload = _fleet_payload()
    payload["maintenance_blocks"] = None
    payload["samples"] = [
        {"time": "2026-09-21T00:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T01:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T05:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T06:00:00Z", "height_m": 8.0},
    ]
    result = _poll_fleet(client, payload)["result"]

    # Windows never span the forbidden gap for any ship.
    for ship in result["ships"]:
        assert len(ship["windows"]) == 2
        reasons = {u["reason"] for u in ship["unknown"]}
        assert "sample_gap_exceeds_max_gap" in reasons
    for d in result["common_departure_windows"]:
        assert d["end"] <= "2026-09-21T02:00:00Z" or \
            d["start"] >= "2026-09-21T05:00:00Z"
    unknown_excl = [e for e in result["exclusions"]
                    if e["reason"] == "unknown_sample_segment"]
    assert unknown_excl and all(e["ships"] == [] for e in unknown_excl)


def test_fleet_no_common_window_when_thresholds_disjoint(client):
    payload = _fleet_payload()
    payload["maintenance_blocks"] = None
    # 乙轮 needs more water than the tide ever provides.
    payload["ships"][1]["draft_m"] = 9.0
    result = _poll_fleet(client, payload)["result"]

    assert result["common_departure_windows"] == []
    assert result["ships"][1]["departure_windows"] == []
    assert all(
        ex["ships"] == ["乙轮"]
        for ex in result["exclusions"]
        if ex["reason"] == "ship_constraint"
    )


def test_fleet_validation_duplicate_names(client):
    payload = _fleet_payload()
    payload["ships"][1]["name"] = payload["ships"][0]["name"]
    resp = client.post("/fleet-tasks", json=payload)
    assert resp.status_code == 422
    assert "唯一" in resp.text


def test_fleet_validation_errors(client):
    base = _fleet_payload()

    def expect_422(mutate):
        payload = _fleet_payload()
        mutate(payload)
        resp = client.post("/fleet-tasks", json=payload)
        assert resp.status_code == 422, resp.text

    expect_422(lambda p: p["ships"].clear())
    expect_422(lambda p: p["ships"][0].pop("transit_duration_seconds"))
    expect_422(lambda p: p["ships"][0].__setitem__("draft_m", -0.5))
    expect_422(lambda p: p["ships"][0].__setitem__("name", "  "))
    expect_422(lambda p: p["samples"][3].__setitem__(
        "time", p["samples"][2]["time"]))
    expect_422(lambda p: p.__setitem__("query_end", p["query_start"]))
    expect_422(lambda p: p.__setitem__("max_gap_seconds", 0))
    expect_422(lambda p: p["maintenance_blocks"].__setitem__(
        0, {"start": "2026-09-21T05:00:00Z", "end": "2026-09-21T03:00:00Z"}))
    # The base payload itself is valid (guard against over-broad mutators).
    assert client.post("/fleet-tasks", json=base).status_code == 201


def test_fleet_result_persisted_across_polls(client):
    payload = _fleet_payload()
    resp = client.post("/fleet-tasks", json=payload)
    task_id = resp.json()["task_id"]
    first = client.get(f"/fleet-tasks/{task_id}").json()
    second = client.get(f"/fleet-tasks/{task_id}").json()
    assert first["status"] == second["status"] == "done"
    assert first["result"] == second["result"]
    # The stored request round-trips verbatim.
    assert [s["name"] for s in second["request"]["ships"]] == ["甲轮", "乙轮"]
