"""Bridge configuration loader."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

BRIDGE_ROOT = Path(__file__).resolve().parent.parent


class HttpConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    api_key: str | None = None


class SdkConfig(BaseModel):
    root: str = "/home/astribot/Downloads/astribot_sdk_aarch64"
    control_hz: float = 250.0
    node_name: str = "astribot_robot_bridge"


class ActionsConfig(BaseModel):
    manifest: str = "assets/actions_link/manifest.json"


class StateConfig(BaseModel):
    default_hz: float = 50.0
    max_hz: float = 100.0
    default_fields: list[str] = Field(default_factory=lambda: ["joints.pos", "joints.vel"])


class RealtimeConfig(BaseModel):
    max_hz: float = 250.0
    default_hz: float = 50.0
    # Output rate to SDK set_joints_position. Prefer control_hz; rate_hz remains an alias.
    control_hz: float = 250.0
    # Used only when session omits source_hz and no prior frame interval is known yet.
    default_source_hz: float = 30.0
    min_blend_s: float = 0.01
    max_blend_s: float = 0.2
    control_way: str = "filter"
    # Max joint change between client frames at source_hz; scaled to per-SDK-tick in worker.
    max_step_delta_rad: float = 0.35
    # Loosen per-tick clamp (1.0 = strict scale; 2.0 = allow 2x).
    step_delta_slack: float = 2.0
    max_abs_rad: float = 3.5
    gripper_min: float = 0.0
    gripper_max: float = 100.0


class AudioConfig(BaseModel):
    dataset_dir: str = "/home/astribot/audio_dataset"
    chunk_seconds: float = 0.1
    default_mode: str = "service"
    # When stream/start omits backend, use this (sdk | system).
    # system = paplay/pacat → Pulse Yundea USB speaker (not built-in APE).
    default_stream_backend: Literal["sdk", "system"] = "sdk"
    # If set, all PCM streams use this backend; client cannot override.
    force_stream_backend: Literal["sdk", "system"] | None = None
    # Empty = auto-detect a Pulse sink whose name contains system_sink_match.
    system_sink: str = ""
    system_sink_match: str = "Yundea"
    # Applied at Bridge startup. Later POST /v1/audio/system-volume overrides it.
    default_system_volume_percent: int = Field(default=75, ge=0, le=150)
    dump_received_wav: bool = False
    dump_dir: str = "logs/audio_dumps"


class ControlRightsConfig(BaseModel):
    auto_reacquire: bool = True
    reacquire_cooldown_s: float = 3.0
    max_reacquire_attempts_per_loss: int = 1
    probe_interval_s: float = 1.0


class SafetyConfig(BaseModel):
    enforce_limits: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = "logs/bridge.log"
    rotate_mb: int = 20
    backup_count: int = 5


class BridgeConfig(BaseModel):
    http: HttpConfig = Field(default_factory=HttpConfig)
    sdk: SdkConfig = Field(default_factory=SdkConfig)
    actions: ActionsConfig = Field(default_factory=ActionsConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    realtime: RealtimeConfig = Field(default_factory=RealtimeConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    control_rights: ControlRightsConfig = Field(default_factory=ControlRightsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return (BRIDGE_ROOT / path).resolve()

    @property
    def manifest_path(self) -> Path:
        return self.resolve_path(self.actions.manifest)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping: {path}")
    return data


@lru_cache(maxsize=1)
def get_config() -> BridgeConfig:
    raw = os.environ.get("BRIDGE_CONFIG", str(BRIDGE_ROOT / "config" / "default.yaml"))
    path = Path(raw)
    return BridgeConfig.model_validate(_load_yaml(path))


def reload_config() -> BridgeConfig:
    get_config.cache_clear()
    return get_config()
