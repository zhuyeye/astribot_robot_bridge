"""Application context / dependency container."""

from __future__ import annotations

from dataclasses import dataclass

from bridge.config import BridgeConfig
from bridge.domain.arbiter import ControlArbiter
from bridge.domain.audio_service import AudioService
from bridge.domain.control_rights import ControlRightsManager
from bridge.domain.info_service import InfoService
from bridge.domain.motion_service import MotionService
from bridge.domain.trajectory_service import TrajectoryService
from bridge.robot.adapter import RobotAdapter


@dataclass
class AppState:
    config: BridgeConfig
    control_robot: RobotAdapter
    reader_robot: RobotAdapter
    audio_robot: RobotAdapter
    arbiter: ControlArbiter
    control_rights: ControlRightsManager
    info: InfoService
    motion: MotionService
    trajectory: TrajectoryService
    audio: AudioService
