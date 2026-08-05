"""Thin Astribot SDK adapter with thread-pool offload for blocking calls."""

from __future__ import annotations

import logging
import builtins
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Callable

from bridge.robot import joint_model as jm
from bridge.robot.sdk_env import import_astribot_sdk

logger = logging.getLogger(__name__)


class RobotAdapter:
    def __init__(
        self,
        *,
        sdk_root: Path | None = None,
        freq: float = 250.0,
        node_name: str = "astribot_robot_bridge",
        high_control_rights: bool = True,
        workers: int = 4,
    ) -> None:
        self._ros_mw, Astribot = import_astribot_sdk(sdk_root)
        self._Astribot = Astribot
        if high_control_rights:
            self.astribot = Astribot(
                freq=freq,
                high_control_rights=True,
                node_name=node_name,
            )
        else:
            original_input = builtins.input
            try:
                builtins.input = lambda *args, **kwargs: ""
                self.astribot = Astribot(
                    freq=freq,
                    high_control_rights=False,
                    node_name=node_name,
                )
            finally:
                builtins.input = original_input
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="robot-sdk")
        self._ready = True
        self._audio_pub = None
        self._audio_activated = False

    @property
    def ready(self) -> bool:
        return bool(self._ready and getattr(self.astribot, "is_alive", False))

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            if self._audio_activated:
                self.astribot.deactivate_audio()
        except Exception:
            logger.exception("deactivate_audio failed")
        try:
            self._ros_mw.shutdown()
        except Exception:
            logger.exception("ros middleware shutdown failed")
        self._ready = False

    def run_sync(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return self._pool.submit(fn, *args, **kwargs).result()

    def run_sync_with_timeout(
        self,
        fn: Callable[..., Any],
        *args: Any,
        timeout_s: float,
        **kwargs: Any,
    ) -> Any:
        future = self._pool.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"{getattr(fn, '__name__', 'sdk_call')} timed out after {timeout_s:.1f}s") from exc

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(*args, **kwargs))

    # ---- info ----
    def get_robot_info(self) -> dict[str, Any]:
        return {
            "alive": bool(getattr(self.astribot, "is_alive", False)),
            "control_rights": bool(self.get_control_rights_status()),
            "parts": list(jm.READABLE_PARTS),
            "dofs": {name: jm.PART_DOFS[name] for name in jm.READABLE_PARTS},
            "readable_dof": jm.READABLE_DOF,
            "trajectory_parts": list(jm.TRAJECTORY_PARTS),
            "trajectory_dof": jm.TRAJECTORY_DOF,
            "whole_body_names": list(getattr(self.astribot, "whole_body_names", jm.READABLE_PARTS)),
            "whole_body_dofs": list(getattr(self.astribot, "whole_body_dofs", [jm.PART_DOFS[n] for n in jm.READABLE_PARTS])),
        }

    def _call_joint_getter(self, method_name: str, names: list[str]) -> dict[str, list[float]]:
        method = getattr(self.astribot, method_name)
        groups = method(names)
        return jm.groups_to_dict(names, [list(g) for g in groups])

    def get_joints(
        self,
        *,
        names: list[str] | None = None,
        which: str = "current",
        include: list[str] | None = None,
    ) -> dict[str, Any]:
        parts = jm.expand_names(names)
        include = include or ["pos"]
        out: dict[str, Any] = {
            "parts": parts,
            "joint_names": jm.joint_names_expanded(parts),
        }
        mapping = {
            "pos": ("current", "get_current_joints_position", "get_desired_joints_position"),
            "vel": ("current", "get_current_joints_velocity", "get_desired_joints_velocity"),
            "acc": ("current", "get_current_joints_acceleration", None),
            "torque": ("current", "get_current_joints_torque", "get_desired_joints_torque"),
        }
        for key in include:
            if key not in mapping:
                raise ValueError(f"unsupported joint field: {key}")
            _, current_fn, desired_fn = mapping[key]
            if which == "desired":
                if desired_fn is None:
                    raise ValueError(f"{key} has no desired getter")
                fn = desired_fn
            else:
                fn = current_fn
            groups_dict = self._call_joint_getter(fn, parts)
            out[key] = groups_dict
            out[f"{key}_flat"] = jm.flatten_groups(parts, [groups_dict[p] for p in parts])
        return out

    def get_joint_limits(self, names: list[str] | None = None) -> dict[str, Any]:
        parts = jm.expand_names(names)
        return {
            "parts": parts,
            "position": jm.groups_to_dict(parts, [list(g) for g in self.astribot.get_joints_position_limit(parts)]),
            "velocity": jm.groups_to_dict(parts, [list(g) for g in self.astribot.get_joints_velocity_limit(parts)]),
            "torque": jm.groups_to_dict(parts, [list(g) for g in self.astribot.get_joints_torque_limit(parts)]),
        }

    def get_cartesian(
        self,
        *,
        names: list[str] | None = None,
        frame: str = "chassis",
        which: str = "current",
    ) -> dict[str, Any]:
        parts = names or ["astribot_arm_left", "astribot_arm_right"]
        if which == "desired":
            poses = self.astribot.get_desired_cartesian_pose(parts, frame=frame)
        elif which == "wbc":
            poses = self.astribot.get_desired_wbc_pose(parts, frame=frame)
        else:
            poses = self.astribot.get_current_cartesian_pose(parts, frame=frame)
        return {
            "names": parts,
            "frame": frame,
            "which": which,
            "poses": [list(p) for p in poses],
        }

    def forward_kinematics(self, names: list[str], joints: list[list[float]]) -> list[list[float]]:
        return [list(p) for p in self.astribot.get_forward_kinematics(names, joints)]

    def inverse_kinematics(self, names: list[str], poses: list[list[float]]) -> list[list[float]]:
        return [list(p) for p in self.astribot.get_inverse_kinematics(names, poses)]

    def closest_point(
        self,
        torso: list[float] | None = None,
        arm_left: list[float] | None = None,
        arm_right: list[float] | None = None,
    ) -> Any:
        return self.astribot.get_self_closest_point(torso, arm_left, arm_right)

    def get_cameras_info(self) -> dict[str, Any]:
        return dict(self.astribot.get_cameras_info())

    def get_control_rights_status(self) -> bool:
        return bool(self.astribot.get_control_rights_status())

    def reacquire_control_rights(self, *, force: bool = True) -> bool:
        self.astribot.astribot_interface.acquire_control_rights(force)
        return self.get_control_rights_status()

    # ---- motion ----
    def move_joints_position(
        self,
        names: list[str],
        commands: list[list[float]],
        *,
        duration: float = 5.0,
        use_wbc: bool = False,
    ) -> Any:
        return self.astribot.move_joints_position(names, commands, duration=duration, use_wbc=use_wbc)

    def move_cartesian_pose(
        self,
        names: list[str],
        commands: list[list[float]],
        *,
        duration: float = 5.0,
        use_wbc: bool = True,
    ) -> Any:
        return self.astribot.move_cartesian_pose(names, commands, duration=duration, use_wbc=use_wbc)

    def move_to_home(self, *, duration: float = -1.0, use_wbc: bool = False) -> Any:
        return self.astribot.move_to_home(duration=duration, use_wbc=use_wbc)

    def move_joints_waypoints(
        self,
        names: list[str],
        waypoints: list[list[list[float]]],
        time_list: list[float],
        *,
        use_wbc: bool = False,
    ) -> Any:
        return self.astribot.move_joints_waypoints(names, waypoints, time_list, use_wbc=use_wbc)

    def move_cartesian_waypoints(
        self,
        names: list[str],
        waypoints: list[list[list[float]]],
        time_list: list[float],
        *,
        use_wbc: bool = True,
    ) -> Any:
        return self.astribot.move_cartesian_waypoints(names, waypoints, time_list, use_wbc=use_wbc)

    def set_joints_position(
        self,
        names: list[str],
        position: list[list[float]],
        *,
        control_way: str = "filter",
        use_wbc: bool = False,
    ) -> Any:
        return self.astribot.set_joints_position(
            names, position, control_way=control_way, use_wbc=use_wbc
        )

    def set_cartesian_pose(
        self,
        names: list[str],
        poses: list[list[float]],
        *,
        control_way: str = "filter",
        use_wbc: bool = False,
    ) -> Any:
        return self.astribot.set_cartesian_pose(
            names, poses, control_way=control_way, use_wbc=use_wbc
        )

    def open_effector(self, names: list[str] | None = None, duration: float = 1.0) -> Any:
        return self.astribot.open_effector(names=names, duration=duration)

    def close_effector(self, names: list[str] | None = None, duration: float = 1.0) -> Any:
        return self.astribot.close_effector(names=names, duration=duration)

    def stop_robot(self) -> Any:
        return self.astribot.stop_robot()

    def restart_robot(self) -> Any:
        return self.astribot.restart_robot()

    def set_filter_parameters(self, filter_scale: float, gripper_filter_scale: float) -> Any:
        return self.astribot.set_filter_parameters(filter_scale, gripper_filter_scale)

    def set_head_follow_effector(self, enable: bool = True, arm_name: str = "dual") -> Any:
        return self.astribot.set_head_follow_effector(enable=enable, arm_name=arm_name)

    def set_wbc_collision_avoidance(self, enable: bool = True) -> Any:
        return self.astribot.set_wbc_collision_avoidance(enable=enable)

    # ---- audio helpers ----
    def activate_audio(self, setting: dict[str, Any] | None = None) -> bool:
        result = self.astribot.activate_audio(setting)
        self._audio_activated = result is not False
        return self._audio_activated

    def deactivate_audio(self) -> Any:
        self._audio_activated = False
        return self.astribot.deactivate_audio()

    @property
    def audio_activated(self) -> bool:
        return self._audio_activated

    def raw_interface(self) -> Any:
        return self.astribot
