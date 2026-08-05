"""Motion services: move_to, realtime, gripper, estop."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from bridge.config import BridgeConfig
from bridge.domain.arbiter import ControlArbiter, ControlMode
from bridge.domain.control_rights import ControlRightsManager
from bridge.robot import joint_model as jm
from bridge.robot.adapter import RobotAdapter
from bridge.safety.limits import check_joint_targets
from bridge.schemas.common import BridgeError

logger = logging.getLogger(__name__)

TaskStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


@dataclass
class MotionTask:
    task_id: str
    kind: str
    status: TaskStatus = "pending"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    message: str | None = None
    result: dict[str, Any] | None = None


class MotionService:
    def __init__(
        self,
        robot: RobotAdapter,
        arbiter: ControlArbiter,
        control_rights: ControlRightsManager,
        config: BridgeConfig,
        *,
        on_estop_hooks: list[Any] | None = None,
    ) -> None:
        self.robot = robot
        self.arbiter = arbiter
        self.control_rights = control_rights
        self.config = config
        self._tasks: dict[str, MotionTask] = {}
        self._tasks_lock = threading.Lock()
        self._realtime_lock = threading.Lock()
        self._realtime_active = False
        self._realtime_space = "joints"
        self._realtime_control_way = config.realtime.control_way
        self._prev_targets: dict[str, list[float]] | None = None
        self._control_gen: int | None = None
        self._on_estop_hooks = on_estop_hooks or []

    # ---- tasks ----
    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise BridgeError("unknown_task", f"unknown task_id: {task_id}", status_code=404)
            return {
                "task_id": task.task_id,
                "kind": task.kind,
                "status": task.status,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "message": task.message,
                "result": task.result,
            }

    def _spawn_task(self, kind: str, fn: Any) -> MotionTask:
        task = MotionTask(task_id=str(uuid.uuid4()), kind=kind, status="pending")
        with self._tasks_lock:
            self._tasks[task.task_id] = task

        def _runner() -> None:
            task.status = "running"
            try:
                result = fn()
                task.status = "succeeded"
                task.result = result if isinstance(result, dict) else {"ok": True}
            except Exception as exc:
                task.status = "failed"
                task.message = str(exc)
                logger.exception("motion task failed: %s", kind)
            finally:
                task.finished_at = time.time()

        threading.Thread(target=_runner, name=f"motion-{kind}", daemon=True).start()
        return task

    # ---- move_to ----
    def move_to_joints(
        self,
        targets: dict[str, list[float]],
        *,
        duration: float = 3.0,
        use_wbc: bool = False,
        force: bool = False,
        wait: bool = False,
        reacquire_if_needed: bool = False,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        names, values = jm.dict_to_groups(targets)

        def _work() -> dict[str, Any]:
            gen = self.arbiter.acquire(ControlMode.MOVE_TO, holder="move_to:joints", force=force)
            self._control_gen = gen
            try:
                self.robot.run_sync_with_timeout(
                    self.robot.move_joints_position,
                    names,
                    values,
                    timeout_s=max(3.0, duration + 2.0),
                    duration=duration,
                    use_wbc=use_wbc,
                )
                return {"names": names, "duration": duration}
            finally:
                self.arbiter.release(gen)
                if self._control_gen == gen:
                    self._control_gen = None

        if wait:
            return {"accepted": True, **_work()}
        task = self._spawn_task("move_to_joints", _work)
        return {"accepted": True, "task_id": task.task_id, "status": task.status}

    def move_to_cartesian(
        self,
        names: list[str],
        poses: list[list[float]],
        *,
        duration: float = 3.0,
        use_wbc: bool = True,
        force: bool = False,
        wait: bool = False,
        reacquire_if_needed: bool = False,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        def _work() -> dict[str, Any]:
            gen = self.arbiter.acquire(ControlMode.MOVE_TO, holder="move_to:cartesian", force=force)
            self._control_gen = gen
            try:
                self.robot.run_sync_with_timeout(
                    self.robot.move_cartesian_pose,
                    names,
                    poses,
                    timeout_s=max(3.0, duration + 2.0),
                    duration=duration,
                    use_wbc=use_wbc,
                )
                return {"names": names, "duration": duration}
            finally:
                self.arbiter.release(gen)
                if self._control_gen == gen:
                    self._control_gen = None

        if wait:
            return {"accepted": True, **_work()}
        task = self._spawn_task("move_to_cartesian", _work)
        return {"accepted": True, "task_id": task.task_id, "status": task.status}

    def move_to_home(
        self,
        *,
        duration: float = -1.0,
        use_wbc: bool = False,
        force: bool = False,
        wait: bool = False,
        reacquire_if_needed: bool = False,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        def _work() -> dict[str, Any]:
            gen = self.arbiter.acquire(ControlMode.MOVE_TO, holder="move_to:home", force=force)
            self._control_gen = gen
            try:
                self.robot.run_sync_with_timeout(
                    self.robot.move_to_home,
                    timeout_s=30.0 if duration < 0 else max(5.0, duration + 5.0),
                    duration=duration,
                    use_wbc=use_wbc,
                )
                return {"duration": duration}
            finally:
                self.arbiter.release(gen)
                if self._control_gen == gen:
                    self._control_gen = None

        if wait:
            return {"accepted": True, **_work()}
        task = self._spawn_task("move_to_home", _work)
        return {"accepted": True, "task_id": task.task_id, "status": task.status}

    def move_to_waypoints(
        self,
        *,
        space: Literal["joints", "cartesian"] = "joints",
        names: list[str],
        waypoints: list[list[list[float]]],
        time_list: list[float],
        use_wbc: bool | None = None,
        force: bool = False,
        wait: bool = False,
        reacquire_if_needed: bool = False,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        if len(waypoints) != len(time_list):
            raise BridgeError("invalid_request", "waypoints and time_list length mismatch")

        def _work() -> dict[str, Any]:
            gen = self.arbiter.acquire(ControlMode.MOVE_TO, holder=f"move_to:waypoints:{space}", force=force)
            self._control_gen = gen
            try:
                if space == "cartesian":
                    self.robot.run_sync_with_timeout(
                        self.robot.move_cartesian_waypoints,
                        names,
                        waypoints,
                        time_list,
                        timeout_s=max(time_list[-1] + 5.0, 10.0),
                        use_wbc=True if use_wbc is None else use_wbc,
                    )
                else:
                    self.robot.run_sync_with_timeout(
                        self.robot.move_joints_waypoints,
                        names,
                        waypoints,
                        time_list,
                        timeout_s=max(time_list[-1] + 5.0, 10.0),
                        use_wbc=False if use_wbc is None else use_wbc,
                    )
                return {"space": space, "names": names, "count": len(waypoints)}
            finally:
                self.arbiter.release(gen)
                if self._control_gen == gen:
                    self._control_gen = None

        if wait:
            return {"accepted": True, **_work()}
        task = self._spawn_task("move_to_waypoints", _work)
        return {"accepted": True, "task_id": task.task_id, "status": task.status}

    # ---- realtime ----
    def open_realtime_session(
        self,
        *,
        rate_hz: float | None = None,
        control_way: str | None = None,
        space: Literal["joints", "cartesian"] = "joints",
        force: bool = False,
        reacquire_if_needed: bool = False,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        cfg = self.config.realtime
        hz = float(rate_hz or cfg.default_hz)
        hz = max(1.0, min(cfg.max_hz, hz))
        way = control_way or cfg.control_way

        def _cancel() -> None:
            with self._realtime_lock:
                self._realtime_active = False
                self._prev_targets = None

        gen = self.arbiter.acquire(
            ControlMode.REALTIME,
            holder="realtime",
            force=force,
            on_cancel=_cancel,
        )
        with self._realtime_lock:
            self._realtime_active = True
            self._realtime_space = space
            self._realtime_control_way = way
            self._prev_targets = None
            self._control_gen = gen
        return {
            "accepted": True,
            "rate_hz": hz,
            "control_way": way,
            "space": space,
            "generation": gen,
        }

    def close_realtime_session(self) -> dict[str, Any]:
        with self._realtime_lock:
            self._realtime_active = False
            self._prev_targets = None
            gen = self._control_gen
            self._control_gen = None
        if gen is not None:
            self.arbiter.release(gen, holder="realtime")
        return {"accepted": True, "active": False}

    def apply_realtime_command(
        self,
        *,
        targets: dict[str, list[float]] | None = None,
        q: list[float] | None = None,
        layout: list[str] | None = None,
        names: list[str] | None = None,
        poses: list[list[float]] | None = None,
        check_step_delta: bool = True,
        reacquire_if_needed: bool = False,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        self.arbiter.require_mode(ControlMode.REALTIME)
        cfg = self.config.realtime

        with self._realtime_lock:
            if not self._realtime_active:
                raise BridgeError("realtime_inactive", "realtime session not open", status_code=409)
            space = self._realtime_space
            control_way = self._realtime_control_way

        if space == "cartesian":
            if names is None or poses is None:
                raise BridgeError("invalid_request", "cartesian realtime requires names and poses")
            self.robot.run_sync_with_timeout(
                self.robot.set_cartesian_pose,
                names,
                poses,
                timeout_s=1.0,
                control_way=control_way,
                use_wbc=False,
            )
            return {"accepted": True, "space": "cartesian", "names": names}

        if targets is None:
            if q is None:
                raise BridgeError("invalid_request", "provide targets or q")
            parts = layout or jm.TRAJECTORY_PARTS
            groups = jm.flat_to_groups(q, parts)
            targets = {n: g for n, g in zip(parts, groups)}

        names_out, values = jm.dict_to_groups(targets, names=list(targets.keys()))
        if self.config.safety.enforce_limits:
            violations = check_joint_targets(
                names_out,
                values,
                prev=self._prev_targets,
                check_step_delta=check_step_delta,
                max_step_delta_rad=cfg.max_step_delta_rad,
                max_abs_rad=cfg.max_abs_rad,
                gripper_min=cfg.gripper_min,
                gripper_max=cfg.gripper_max,
            )
            if violations:
                return {
                    "accepted": False,
                    "violations": [v.to_dict() for v in violations],
                    "message": "command rejected by safety checks",
                }

        self.robot.run_sync_with_timeout(
            self.robot.set_joints_position,
            names_out,
            values,
            timeout_s=1.0,
            control_way=control_way,
            use_wbc=False,
        )
        with self._realtime_lock:
            self._prev_targets = {n: list(v) for n, v in zip(names_out, values)}
        return {"accepted": True, "space": "joints", "names": names_out}

    def on_control_rights_lost(self) -> None:
        with self._realtime_lock:
            self._realtime_active = False
            self._prev_targets = None
            self._control_gen = None

    # ---- gripper / settings / estop ----
    def gripper(self, action: Literal["open", "close"], *, names: list[str] | None = None, duration: float = 1.0) -> dict[str, Any]:
        if action == "open":
            self.robot.open_effector(names=names, duration=duration)
        else:
            self.robot.close_effector(names=names, duration=duration)
        return {"action": action, "names": names, "duration": duration}

    def set_settings(
        self,
        *,
        filter_scale: float | None = None,
        gripper_filter_scale: float | None = None,
        head_follow: bool | None = None,
        head_follow_arm: str = "dual",
        collision_avoidance: bool | None = None,
    ) -> dict[str, Any]:
        applied: dict[str, Any] = {}
        if filter_scale is not None and gripper_filter_scale is not None:
            self.robot.set_filter_parameters(filter_scale, gripper_filter_scale)
            applied["filter_scale"] = filter_scale
            applied["gripper_filter_scale"] = gripper_filter_scale
        if head_follow is not None:
            self.robot.set_head_follow_effector(enable=head_follow, arm_name=head_follow_arm)
            applied["head_follow"] = head_follow
            applied["head_follow_arm"] = head_follow_arm
        if collision_avoidance is not None:
            self.robot.set_wbc_collision_avoidance(enable=collision_avoidance)
            applied["collision_avoidance"] = collision_avoidance
        if not applied:
            raise BridgeError("invalid_request", "no settings provided")
        return applied

    def estop(self) -> dict[str, Any]:
        for hook in self._on_estop_hooks:
            try:
                hook()
            except Exception:
                logger.exception("estop hook failed")
        self.arbiter.bump_and_clear()
        with self._realtime_lock:
            self._realtime_active = False
            self._prev_targets = None
            self._control_gen = None
        try:
            self.robot.stop_robot()
            self.robot.restart_robot()
            return {"accepted": True, "message": "emergency stop applied"}
        except Exception as exc:
            self.arbiter.set_error(str(exc))
            raise BridgeError("estop_failed", str(exc), status_code=500) from exc

    def restart(self) -> dict[str, Any]:
        self.robot.restart_robot()
        return {"accepted": True}
