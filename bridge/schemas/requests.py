"""Request/response pydantic models for control APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PlayActionRequest(BaseModel):
    action_id: str
    request_id: str = ""
    force: bool = False
    reacquire_if_needed: bool | None = None


class MoveToJointsRequest(BaseModel):
    targets: dict[str, list[float]]
    duration: float = 3.0
    use_wbc: bool = False
    force: bool = False
    wait: bool = False
    reacquire_if_needed: bool | None = None


class MoveToCartesianRequest(BaseModel):
    names: list[str]
    poses: list[list[float]]
    duration: float = 3.0
    use_wbc: bool = True
    force: bool = False
    wait: bool = False
    reacquire_if_needed: bool | None = None


class MoveToHomeRequest(BaseModel):
    duration: float = -1.0
    use_wbc: bool = False
    force: bool = False
    wait: bool = False
    reacquire_if_needed: bool | None = None


class MoveToWaypointsRequest(BaseModel):
    space: Literal["joints", "cartesian"] = "joints"
    names: list[str]
    waypoints: list[list[list[float]]]
    time_list: list[float]
    use_wbc: bool | None = None
    force: bool = False
    wait: bool = False
    reacquire_if_needed: bool | None = None


class RealtimeSessionRequest(BaseModel):
    rate_hz: float | None = None
    control_way: str | None = None
    space: Literal["joints", "cartesian"] = "joints"
    force: bool = False
    reacquire_if_needed: bool | None = None


class RealtimeCommandRequest(BaseModel):
    targets: dict[str, list[float]] | None = None
    q: list[float] | None = None
    layout: list[str] | None = None
    names: list[str] | None = None
    poses: list[list[float]] | None = None
    check_step_delta: bool = True
    reacquire_if_needed: bool | None = None


class GripperRequest(BaseModel):
    names: list[str] | None = None
    duration: float = 1.0


class SettingsRequest(BaseModel):
    filter_scale: float | None = None
    gripper_filter_scale: float | None = None
    head_follow: bool | None = None
    head_follow_arm: str = "dual"
    collision_avoidance: bool | None = None


class KinematicsRequest(BaseModel):
    names: list[str]
    joints: list[list[float]] | None = None
    poses: list[list[float]] | None = None


class ClosestPointRequest(BaseModel):
    torso: list[float] | None = None
    arm_left: list[float] | None = None
    arm_right: list[float] | None = None


class AudioPlayRequest(BaseModel):
    clip_id: str | None = None
    path: str | None = None
    mode: Literal["service", "stream"] | None = None
    force: bool = False
    reacquire_if_needed: bool | None = None


class AudioStreamStartRequest(BaseModel):
    force: bool = False
    reacquire_if_needed: bool | None = None


class AudioSystemVolumeRequest(BaseModel):
    volume_percent: int = Field(ge=0, le=150)
    unmute: bool = True
