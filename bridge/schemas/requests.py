"""Request/response pydantic models for control APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PlayActionRequest(BaseModel):
    action_id: str
    request_id: str = ""
    force: bool = False
    reacquire_if_needed: bool | None = None
    expected_current_session_id: str | None = None
    supersedes_session_id: str | None = None


class StopActionRequest(BaseModel):
    session_id: str
    request_id: str = ""


class MoveToJointsRequest(BaseModel):
    targets: dict[str, list[float]]
    duration: float = 3.0
    use_wbc: bool = False
    force: bool = False
    wait: bool = False
    reacquire_if_needed: bool | None = None
    request_id: str = ""
    expected_current_session_id: str | None = None


class MoveToCartesianRequest(BaseModel):
    names: list[str]
    poses: list[list[float]]
    duration: float = 3.0
    use_wbc: bool = True
    force: bool = False
    wait: bool = False
    reacquire_if_needed: bool | None = None
    request_id: str = ""
    expected_current_session_id: str | None = None


class MoveToHomeRequest(BaseModel):
    duration: float = -1.0
    use_wbc: bool = False
    force: bool = False
    wait: bool = False
    reacquire_if_needed: bool | None = None
    request_id: str = ""
    expected_current_session_id: str | None = None


class MoveToWaypointsRequest(BaseModel):
    space: Literal["joints", "cartesian"] = "joints"
    names: list[str]
    waypoints: list[list[list[float]]]
    time_list: list[float]
    use_wbc: bool | None = None
    force: bool = False
    wait: bool = False
    reacquire_if_needed: bool | None = None
    request_id: str = ""
    expected_current_session_id: str | None = None


class RealtimeSessionRequest(BaseModel):
    # Legacy: alone → treated as source_hz (upsample to control_hz). With source_hz → control alias.
    rate_hz: float | None = None
    # Client send rate for qpos (optional). Improves blend timing when known.
    source_hz: float | None = None
    # Robot output rate; defaults to config realtime.control_hz (250).
    control_hz: float | None = None
    control_way: str | None = None
    space: Literal["joints", "cartesian"] = "joints"
    force: bool = False
    reacquire_if_needed: bool | None = None
    request_id: str = ""
    expected_current_session_id: str | None = None
    supersedes_session_id: str | None = None
    # True: overwrite latest target each command; control loop clamps toward it (no time blend).
    prefer_latest: bool = True
    # every/drain_async: reply per-command ack; none: skip WS ack. Unknown values ignored upstream.
    ack_mode: Literal["every", "drain_async", "none"] | None = None


class RealtimeCommandRequest(BaseModel):
    session_id: str
    targets: dict[str, list[float]] | None = None
    q: list[float] | None = None
    layout: list[str] | None = None
    names: list[str] | None = None
    poses: list[list[float]] | None = None
    check_step_delta: bool = True
    # When true, step limiting runs on SDK output (after interp), not on incoming client frames.
    reacquire_if_needed: bool | None = None


class CloseRealtimeRequest(BaseModel):
    session_id: str
    request_id: str = ""


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


class AudioSystemPlayRequest(BaseModel):
    clip_id: str | None = None
    path: str | None = None
    force: bool = False


class AudioStreamStartRequest(BaseModel):
    force: bool = False
    reacquire_if_needed: bool | None = None
    # sdk: ROS /astribot_audio/speaker/stream
    # system: pacat → Pulse Yundea 8MICA USB (not built-in APE)
    backend: Literal["sdk", "system"] | None = None


class AudioSystemVolumeRequest(BaseModel):
    volume_percent: int = Field(ge=0, le=150)
    unmute: bool = True
