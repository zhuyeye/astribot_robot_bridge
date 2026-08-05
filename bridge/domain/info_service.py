"""Robot information / state sampling service."""

from __future__ import annotations

import time
from typing import Any

from bridge.domain.arbiter import ControlArbiter
from bridge.domain.control_rights import ControlRightsManager
from bridge.robot import joint_model as jm
from bridge.robot.adapter import RobotAdapter
from bridge.schemas.common import BridgeError


class InfoService:
    def __init__(
        self,
        robot: RobotAdapter,
        arbiter: ControlArbiter,
        control_rights: ControlRightsManager | None = None,
    ) -> None:
        self.robot = robot
        self.arbiter = arbiter
        self.control_rights = control_rights

    def robot_info(self) -> dict[str, Any]:
        info = self.robot.get_robot_info()
        if self.control_rights is not None:
            info["control_rights"] = self.control_rights.have_control_rights()
            info["control_rights_state"] = self.control_rights.snapshot()
        info["control"] = self.arbiter.to_dict()
        return info

    def joints(
        self,
        *,
        names: list[str] | None = None,
        which: str = "current",
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        include = fields or ["pos", "vel"]
        try:
            return self.robot.get_joints(names=names, which=which, include=include)
        except ValueError as exc:
            raise BridgeError("invalid_request", str(exc)) from exc

    def joint_limits(self, names: list[str] | None = None) -> dict[str, Any]:
        return self.robot.get_joint_limits(names=names)

    def cartesian(
        self,
        *,
        names: list[str] | None = None,
        frame: str = "chassis",
        which: str = "current",
    ) -> dict[str, Any]:
        return self.robot.get_cartesian(names=names, frame=frame, which=which)

    def fk(self, names: list[str], joints: list[list[float]]) -> dict[str, Any]:
        poses = self.robot.forward_kinematics(names, joints)
        return {"names": names, "poses": poses}

    def ik(self, names: list[str], poses: list[list[float]]) -> dict[str, Any]:
        joints = self.robot.inverse_kinematics(names, poses)
        return {"names": names, "joints": joints}

    def closest_point(
        self,
        torso: list[float] | None = None,
        arm_left: list[float] | None = None,
        arm_right: list[float] | None = None,
    ) -> dict[str, Any]:
        return {"result": self.robot.closest_point(torso, arm_left, arm_right)}

    def cameras(self) -> dict[str, Any]:
        return self.robot.get_cameras_info()

    def sample_state(self, fields: list[str] | None = None) -> dict[str, Any]:
        fields = fields or ["joints.pos", "joints.vel"]
        payload: dict[str, Any] = {
            "t": time.time(),
            "mode": self.arbiter.snapshot().mode.value,
        }
        want_pos = "joints.pos" in fields or "joints" in fields
        want_vel = "joints.vel" in fields
        include: list[str] = []
        if want_pos:
            include.append("pos")
        if want_vel:
            include.append("vel")
        if include:
            joints = self.robot.get_joints(names=jm.READABLE_PARTS, which="current", include=include)
            payload["joints"] = {
                "parts": joints["parts"],
                "joint_names": joints["joint_names"],
            }
            if "pos" in joints:
                payload["joints"]["pos"] = joints["pos"]
                payload["joints"]["pos_flat"] = joints["pos_flat"]
            if "vel" in joints:
                payload["joints"]["vel"] = joints["vel"]
                payload["joints"]["vel_flat"] = joints["vel_flat"]

        if any(f.startswith("cartesian") for f in fields):
            payload["cartesian"] = self.robot.get_cartesian(which="current")
        return payload
