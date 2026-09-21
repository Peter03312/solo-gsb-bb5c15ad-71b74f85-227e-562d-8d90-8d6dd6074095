"""Pydantic models: request/response schemas and validation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

from app.services.tides import to_epoch


def _utc_z(value: datetime) -> str:
    """Serialise a datetime as ISO-8601 with a trailing ``Z`` (UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


UtcZ = Annotated[
    datetime,
    PlainSerializer(_utc_z, return_type=str),
]


class TideSample(BaseModel):
    time: datetime = Field(..., description="潮位采样时间（无时区按 UTC 处理）")
    height_m: float = Field(..., description="潮高（米，需与吃水/富余同一垂直基准面）")

    model_config = ConfigDict(json_schema_extra={
        "example": {"time": "2026-09-21T00:00:00Z", "height_m": 2.15}
    })


class MaintenanceBlock(BaseModel):
    start: datetime = Field(..., description="封航起始时间（UTC，无时区按 UTC 处理）")
    end: datetime = Field(..., description="封航结束时间（UTC，须晚于起始时间）")

    model_config = ConfigDict(json_schema_extra={
        "example": {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
    })

    @model_validator(mode="after")
    def _validate(self) -> "MaintenanceBlock":
        if to_epoch(self.end) <= to_epoch(self.start):
            raise ValueError("maintenance_blocks 的 end 必须晚于 start")
        return self


class WindowRequest(BaseModel):
    samples: list[TideSample] = Field(..., min_length=2, description="按 UTC 递增的潮位样本")
    query_start: datetime = Field(..., description="查询起始时间（UTC）")
    query_end: datetime = Field(..., description="查询结束时间（UTC）")
    draft_m: float = Field(..., ge=0, description="船舶吃水（米）")
    clearance_m: float = Field(..., ge=0, description="船底最小富余水深（米）")
    max_gap_seconds: float = Field(
        ..., gt=0, description="相邻样本最大允许间隔（秒），超过则禁止插值"
    )
    transit_duration_seconds: float | None = Field(
        None, gt=0,
        description="航程占用时长（秒）；提供后结果附带 departure_windows",
    )
    maintenance_blocks: list[MaintenanceBlock] | None = Field(
        None,
        description="维护封航区间（UTC 起止）；裁剪到查询范围并合并重叠/首尾相接区间",
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "samples": [
                {"time": "2026-09-21T00:00:00Z", "height_m": 1.0},
                {"time": "2026-09-21T01:00:00Z", "height_m": 3.0},
                {"time": "2026-09-21T02:00:00Z", "height_m": 6.0},
                {"time": "2026-09-21T03:00:00Z", "height_m": 8.0},
                {"time": "2026-09-21T04:00:00Z", "height_m": 8.0},
                {"time": "2026-09-21T05:00:00Z", "height_m": 6.0},
                {"time": "2026-09-21T06:00:00Z", "height_m": 3.0},
                {"time": "2026-09-21T07:00:00Z", "height_m": 1.0},
            ],
            "query_start": "2026-09-21T00:00:00Z",
            "query_end": "2026-09-21T07:00:00Z",
            "draft_m": 4.0,
            "clearance_m": 1.0,
            "max_gap_seconds": 3700,
            "transit_duration_seconds": 1800,
            "maintenance_blocks": [
                {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
            ],
        }
    })

    @model_validator(mode="after")
    def _validate(self) -> "WindowRequest":
        # Naive datetimes are interpreted as UTC before any comparison.
        epochs = [to_epoch(s.time) for s in self.samples]
        qs = to_epoch(self.query_start)
        qe = to_epoch(self.query_end)

        for i in range(1, len(epochs)):
            if epochs[i] <= epochs[i - 1]:
                raise ValueError("samples.time 必须严格按 UTC 递增")
        if qe <= qs:
            raise ValueError("query_end 必须晚于 query_start")
        for name, value in (
            ("draft_m", self.draft_m),
            ("clearance_m", self.clearance_m),
            ("max_gap_seconds", self.max_gap_seconds),
        ):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{name} 必须为有限数值")
        if self.transit_duration_seconds is not None:
            v = self.transit_duration_seconds
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError("transit_duration_seconds 必须为有限数值")
        if self.maintenance_blocks and self.transit_duration_seconds is None:
            raise ValueError(
                "maintenance_blocks 需配合 transit_duration_seconds 一起使用"
            )
        for i, s in enumerate(self.samples):
            if s.height_m != s.height_m:
                raise ValueError(f"samples[{i}].height_m 不能为 NaN")
        return self


class FleetShipInput(BaseModel):
    """One vessel in a fleet coordination task."""

    name: str = Field(..., min_length=1, description="船舶名称（队内需唯一，用于标记排除责任）")
    draft_m: float = Field(..., ge=0, description="船舶吃水（米）")
    clearance_m: float = Field(..., ge=0, description="船底最小富余水深（米）")
    transit_duration_seconds: float = Field(
        ..., gt=0, description="航程占用时长（秒）；船队协同任务必须提供"
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "name": "海巡01",
            "draft_m": 4.0,
            "clearance_m": 1.0,
            "transit_duration_seconds": 1800,
        }
    })

    @model_validator(mode="after")
    def _validate(self) -> "FleetShipInput":
        for fname in ("draft_m", "clearance_m", "transit_duration_seconds"):
            value = getattr(self, fname)
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{fname} 必须为有限数值")
        if not self.name.strip():
            raise ValueError("name 不能为空白字符")
        return self


class FleetTaskRequest(BaseModel):
    """Request for POST /fleet-tasks: shared tide context + vessel list."""

    ships: list[FleetShipInput] = Field(
        ..., min_length=1, description="参与协同的船队（至少 1 艘，名称需唯一）"
    )
    samples: list[TideSample] = Field(..., min_length=2, description="全队共享的潮位样本")
    query_start: datetime = Field(..., description="查询起始时间（UTC）")
    query_end: datetime = Field(..., description="查询结束时间（UTC）")
    max_gap_seconds: float = Field(
        ..., gt=0, description="相邻样本最大允许间隔（秒），超过则禁止插值（全队约束）"
    )
    maintenance_blocks: list[MaintenanceBlock] | None = Field(
        None,
        description="全队共享的维护封航区间（UTC 起止）；裁剪到查询范围并合并重叠/首尾相接区间",
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "ships": [
                {"name": "海巡01", "draft_m": 4.0, "clearance_m": 1.0,
                 "transit_duration_seconds": 1800},
                {"name": "远洋货轮", "draft_m": 6.0, "clearance_m": 1.0,
                 "transit_duration_seconds": 3600},
            ],
            "samples": [
                {"time": "2026-09-21T00:00:00Z", "height_m": 1.0},
                {"time": "2026-09-21T01:00:00Z", "height_m": 3.0},
                {"time": "2026-09-21T02:00:00Z", "height_m": 6.0},
                {"time": "2026-09-21T03:00:00Z", "height_m": 8.0},
                {"time": "2026-09-21T04:00:00Z", "height_m": 8.0},
                {"time": "2026-09-21T05:00:00Z", "height_m": 6.0},
                {"time": "2026-09-21T06:00:00Z", "height_m": 3.0},
                {"time": "2026-09-21T07:00:00Z", "height_m": 1.0},
            ],
            "query_start": "2026-09-21T00:00:00Z",
            "query_end": "2026-09-21T07:00:00Z",
            "max_gap_seconds": 3700,
            "maintenance_blocks": [
                {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T04:00:00Z"}
            ],
        }
    })

    @model_validator(mode="after")
    def _validate(self) -> "FleetTaskRequest":
        # Naive datetimes are interpreted as UTC before any comparison.
        epochs = [to_epoch(s.time) for s in self.samples]
        qs = to_epoch(self.query_start)
        qe = to_epoch(self.query_end)

        for i in range(1, len(epochs)):
            if epochs[i] <= epochs[i - 1]:
                raise ValueError("samples.time 必须严格按 UTC 递增")
        if qe <= qs:
            raise ValueError("query_end 必须晚于 query_start")
        if self.max_gap_seconds != self.max_gap_seconds or \
                self.max_gap_seconds in (float("inf"), float("-inf")):
            raise ValueError("max_gap_seconds 必须为有限数值")
        for i, s in enumerate(self.samples):
            if s.height_m != s.height_m:
                raise ValueError(f"samples[{i}].height_m 不能为 NaN")
        names = [ship.name for ship in self.ships]
        if len(set(names)) != len(names):
            raise ValueError("ships[].name 在队内需唯一")
        return self


class WindowOut(BaseModel):
    start: UtcZ
    end: UtcZ
    start_height_m: float
    end_height_m: float
    min_height_m: float
    duration_seconds: int


class UnknownOut(BaseModel):
    start: UtcZ
    end: UtcZ
    reason: str
    duration_seconds: float


class DepartureWindowOut(BaseModel):
    start: UtcZ
    end: UtcZ
    duration_seconds: int


class ResultOut(BaseModel):
    threshold_m: float
    max_gap_seconds: float
    windows: list[WindowOut]
    unknown: list[UnknownOut]
    departure_windows: list[DepartureWindowOut] | None = None


class TaskOut(BaseModel):
    task_id: str
    status: str
    created_at: UtcZ
    request: WindowRequest
    result: ResultOut | None = None
    error: str | None = None


class FleetShipResultOut(BaseModel):
    """Per-vessel window outcome inside a fleet task result."""

    name: str
    threshold_m: float
    windows: list[WindowOut]
    unknown: list[UnknownOut]
    departure_windows: list[DepartureWindowOut]


class FleetExclusionOut(BaseModel):
    """A fleet-candidate stretch excluded from the common departure windows.

    ``ships`` carries the names (in request order) of the vessels that cannot
    depart on this stretch; an empty list means the stretch is barred by a
    fleet-wide constraint — a maintenance closure or an unknown sampling
    segment — rather than by an individual vessel's depth limit.
    """

    start: UtcZ
    end: UtcZ
    reason: str = Field(
        ...,
        description=(
            "排除原因：ship_constraint（船舶自身水深/航程约束，见 ships）、"
            "maintenance_closure（全队封航）、unknown_sample_segment（全队未知采样段）"
        ),
    )
    ships: list[str]
    duration_seconds: int


class FleetResultOut(BaseModel):
    ships: list[FleetShipResultOut]
    common_departure_windows: list[DepartureWindowOut]
    exclusions: list[FleetExclusionOut]


class FleetTaskOut(BaseModel):
    task_id: str
    status: str
    created_at: UtcZ
    request: FleetTaskRequest
    result: FleetResultOut | None = None
    error: str | None = None
