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
from bridge.domain.control_context import ControlContext
from bridge.domain.control_rights import ControlRightsManager
from bridge.robot import joint_model as jm
from bridge.robot.adapter import RobotAdapter
from bridge.safety.limits import check_joint_targets, clamp_group_step_delta
from bridge.schemas.common import BridgeError, StaleSessionError

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
        control_context: ControlContext,
        config: BridgeConfig,
        *,
        on_estop_hooks: list[Any] | None = None,
    ) -> None:
        self.robot = robot
        self.arbiter = arbiter
        self.control_rights = control_rights
        self.control_context = control_context
        self.config = config
        self._tasks: dict[str, MotionTask] = {}
        self._tasks_lock = threading.Lock()
        self._realtime_lock = threading.RLock()
        self._realtime_active = False
        self._realtime_space = "joints"
        self._realtime_control_way = config.realtime.control_way
        self._realtime_source_hz: float | None = None
        self._realtime_control_hz = float(config.realtime.control_hz)
        self._prev_targets: dict[str, list[float]] | None = None
        self._control_gen: int | None = None
        self._on_estop_hooks = on_estop_hooks or []
        self._rt_stop = threading.Event()
        self._rt_worker: threading.Thread | None = None
        self._rt_names: list[str] | None = None
        self._rt_cmd: list[list[float]] | None = None
        self._rt_from: list[list[float]] | None = None
        self._rt_to: list[list[float]] | None = None
        self._rt_blend_start = 0.0
        self._rt_blend_s = 0.0
        self._rt_last_target_ts: float | None = None
        self._rt_ticks = 0
        self._rt_targets_received = 0
        self._realtime_check_step_delta = True
        self._prefer_latest = True
        self._ack_mode = "every"
        self._realtime_session_id: str | None = None
        self._realtime_request_id: str | None = None
        self._last_terminal_reason: str | None = None

    @staticmethod
    def _max_abs_delta(groups_a: list[list[float]], groups_b: list[list[float]]) -> float:
        max_delta = 0.0
        for ga, gb in zip(groups_a, groups_b):
            for va, vb in zip(ga, gb):
                max_delta = max(max_delta, abs(vb - va))
        return max_delta

    def _raise_stale(
        self,
        *,
        provided_session_id: str | None = None,
        expected_session_id: str | None = None,
    ) -> None:
        snap = self.control_context.snapshot()
        logger.warning(
            "motion stale_request provided_session_id=%s expected_current_session_id=%s active_session_id=%s active_mode=%s",
            provided_session_id,
            expected_session_id,
            snap.active_session_id,
            snap.active_mode,
        )
        raise StaleSessionError(
            provided_session_id=provided_session_id,
            expected_session_id=expected_session_id,
            active_session_id=snap.active_session_id,
            active_mode=snap.active_mode,
        )

    def _check_expected_context(self, expected_current_session_id: str | None) -> None:
        if self.control_context.matches_expected(expected_current_session_id):
            return
        self._raise_stale(expected_session_id=expected_current_session_id)

    def _check_active_realtime_session(self, session_id: str) -> None:
        if not session_id:
            raise BridgeError("invalid_request", "session_id is required")
        if session_id == self._realtime_session_id:
            return
        self._raise_stale(provided_session_id=session_id)

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
        request_id: str = "",
        expected_current_session_id: str | None = None,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        self._check_expected_context(expected_current_session_id)
        names, values = jm.dict_to_groups(targets)
        logger.info(
            "motion move_to_joints requested request_id=%s expected_current_session_id=%s force=%s wait=%s names=%s duration=%.3f",
            request_id,
            expected_current_session_id,
            force,
            wait,
            names,
            duration,
        )

        def _work() -> dict[str, Any]:
            gen = self.arbiter.acquire(ControlMode.MOVE_TO, holder="move_to:joints", force=force)
            epoch = self.control_context.start_transient("move_to")
            self._control_gen = gen
            try:
                current = self.robot.astribot.get_desired_joints_position(names)
                max_delta = self._max_abs_delta(current, values)
                logger.info(
                    "motion move_to_joints executing request_id=%s gen=%s epoch=%s max_delta_rad=%.4f",
                    request_id,
                    gen,
                    epoch,
                    max_delta,
                )
                self.robot.run_sync_with_timeout(
                    self.robot.move_joints_position,
                    names,
                    values,
                    timeout_s=max(3.0, duration + 2.0),
                    duration=duration,
                    use_wbc=use_wbc,
                )
                logger.info(
                    "motion move_to_joints completed request_id=%s gen=%s epoch=%s",
                    request_id,
                    gen,
                    epoch,
                )
                return {"names": names, "duration": duration, "request_id": request_id}
            finally:
                self.arbiter.release(gen)
                if self._control_gen == gen:
                    self._control_gen = None
                self.control_context.finish_transient(epoch)

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
        request_id: str = "",
        expected_current_session_id: str | None = None,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        self._check_expected_context(expected_current_session_id)
        logger.info(
            "motion move_to_cartesian requested request_id=%s expected_current_session_id=%s force=%s wait=%s names=%s duration=%.3f",
            request_id,
            expected_current_session_id,
            force,
            wait,
            names,
            duration,
        )
        def _work() -> dict[str, Any]:
            gen = self.arbiter.acquire(ControlMode.MOVE_TO, holder="move_to:cartesian", force=force)
            epoch = self.control_context.start_transient("move_to")
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
                logger.info(
                    "motion move_to_cartesian completed request_id=%s gen=%s epoch=%s",
                    request_id,
                    gen,
                    epoch,
                )
                return {"names": names, "duration": duration, "request_id": request_id}
            finally:
                self.arbiter.release(gen)
                if self._control_gen == gen:
                    self._control_gen = None
                self.control_context.finish_transient(epoch)

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
        request_id: str = "",
        expected_current_session_id: str | None = None,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        self._check_expected_context(expected_current_session_id)
        logger.info(
            "motion move_to_home requested request_id=%s expected_current_session_id=%s force=%s wait=%s duration=%.3f",
            request_id,
            expected_current_session_id,
            force,
            wait,
            duration,
        )
        def _work() -> dict[str, Any]:
            gen = self.arbiter.acquire(ControlMode.MOVE_TO, holder="move_to:home", force=force)
            epoch = self.control_context.start_transient("move_to")
            self._control_gen = gen
            try:
                self.robot.run_sync_with_timeout(
                    self.robot.move_to_home,
                    timeout_s=30.0 if duration < 0 else max(5.0, duration + 5.0),
                    duration=duration,
                    use_wbc=use_wbc,
                )
                logger.info(
                    "motion move_to_home completed request_id=%s gen=%s epoch=%s",
                    request_id,
                    gen,
                    epoch,
                )
                return {"duration": duration, "request_id": request_id}
            finally:
                self.arbiter.release(gen)
                if self._control_gen == gen:
                    self._control_gen = None
                self.control_context.finish_transient(epoch)

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
        request_id: str = "",
        expected_current_session_id: str | None = None,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        self._check_expected_context(expected_current_session_id)
        if len(waypoints) != len(time_list):
            raise BridgeError("invalid_request", "waypoints and time_list length mismatch")
        logger.info(
            "motion move_to_waypoints requested request_id=%s expected_current_session_id=%s force=%s wait=%s space=%s names=%s count=%s",
            request_id,
            expected_current_session_id,
            force,
            wait,
            space,
            names,
            len(waypoints),
        )

        def _work() -> dict[str, Any]:
            gen = self.arbiter.acquire(ControlMode.MOVE_TO, holder=f"move_to:waypoints:{space}", force=force)
            epoch = self.control_context.start_transient("move_to")
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
                logger.info(
                    "motion move_to_waypoints completed request_id=%s gen=%s epoch=%s count=%s",
                    request_id,
                    gen,
                    epoch,
                    len(waypoints),
                )
                return {"space": space, "names": names, "count": len(waypoints), "request_id": request_id}
            finally:
                self.arbiter.release(gen)
                if self._control_gen == gen:
                    self._control_gen = None
                self.control_context.finish_transient(epoch)

        if wait:
            return {"accepted": True, **_work()}
        task = self._spawn_task("move_to_waypoints", _work)
        return {"accepted": True, "task_id": task.task_id, "status": task.status}

    # ---- realtime ----
    @staticmethod
    def _normalize_ack_mode(ack_mode: str | None) -> str:
        if ack_mode in ("every", "drain_async", "none"):
            return str(ack_mode)
        return "every"

    def open_realtime_session(
        self,
        *,
        rate_hz: float | None = None,
        source_hz: float | None = None,
        control_hz: float | None = None,
        control_way: str | None = None,
        space: Literal["joints", "cartesian"] = "joints",
        force: bool = False,
        reacquire_if_needed: bool = False,
        request_id: str = "",
        expected_current_session_id: str | None = None,
        supersedes_session_id: str | None = None,
        prefer_latest: bool = True,
        ack_mode: str | None = None,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        self._check_expected_context(expected_current_session_id)
        logger.info(
            "motion realtime_open requested request_id=%s expected_current_session_id=%s supersedes_session_id=%s force=%s space=%s prefer_latest=%s ack_mode=%s",
            request_id,
            expected_current_session_id,
            supersedes_session_id,
            force,
            space,
            prefer_latest,
            ack_mode,
        )
        cfg = self.config.realtime
        # Output rate: explicit control_hz wins. If only legacy rate_hz is given (no source_hz),
        # treat rate_hz as the client send rate and upsample to config control_hz.
        # If source_hz + rate_hz are both given (no control_hz), rate_hz is the control alias.
        if control_hz is not None:
            out_hz = float(control_hz)
        elif source_hz is not None and rate_hz is not None:
            out_hz = float(rate_hz)
        else:
            out_hz = float(cfg.control_hz)
        out_hz = max(1.0, min(cfg.max_hz, out_hz))

        if source_hz is not None:
            src_hz: float | None = float(source_hz)
        elif rate_hz is not None and control_hz is None:
            src_hz = float(rate_hz)
        else:
            src_hz = None
        if src_hz is not None and src_hz <= 0:
            raise BridgeError("invalid_request", "source_hz must be > 0")
        way = control_way or cfg.control_way
        prefer = bool(prefer_latest)
        ack = self._normalize_ack_mode(ack_mode)

        def _cancel() -> None:
            self._stop_realtime_worker()

        gen = self.arbiter.acquire(
            ControlMode.REALTIME,
            holder="realtime",
            force=force,
            on_cancel=_cancel,
        )
        self._stop_realtime_worker()
        session_id, _ = self.control_context.issue_session("realtime", gen)
        with self._realtime_lock:
            self._realtime_active = True
            self._realtime_space = space
            self._realtime_control_way = way
            self._realtime_source_hz = src_hz
            self._realtime_control_hz = out_hz
            self._prev_targets = None
            self._control_gen = gen
            self._realtime_session_id = session_id
            self._realtime_request_id = request_id
            self._last_terminal_reason = None
            self._rt_names = None
            self._rt_cmd = None
            self._rt_from = None
            self._rt_to = None
            self._rt_blend_start = 0.0
            self._rt_blend_s = 0.0
            self._rt_last_target_ts = None
            self._rt_ticks = 0
            self._rt_targets_received = 0
            self._realtime_check_step_delta = True
            self._prefer_latest = prefer
            self._ack_mode = ack
            if space == "joints":
                self._rt_stop = threading.Event()
                self._rt_worker = threading.Thread(
                    target=self._realtime_worker_loop,
                    name="realtime-interp",
                    daemon=True,
                )
                self._rt_worker.start()
        logger.info(
            "motion realtime_opened session_id=%s request_id=%s space=%s control_hz=%.1f source_hz=%s control_way=%s prefer_latest=%s ack_mode=%s gen=%s",
            session_id,
            request_id,
            space,
            out_hz,
            src_hz,
            way,
            prefer,
            ack,
            gen,
        )
        return {
            "accepted": True,
            "rate_hz": out_hz,
            "control_hz": out_hz,
            "source_hz": src_hz,
            "interpolate": space == "joints" and not prefer,
            "prefer_latest": prefer,
            "ack_mode": ack,
            "control_way": way,
            "space": space,
            "session_id": session_id,
            "request_id": request_id,
            "supersedes_session_id": supersedes_session_id,
            "generation": gen,
        }

    def realtime_ack_mode(self) -> str:
        with self._realtime_lock:
            return self._ack_mode

    def realtime_prefer_latest(self) -> bool:
        with self._realtime_lock:
            return self._prefer_latest

    def realtime_session_id(self) -> str | None:
        with self._realtime_lock:
            return self._realtime_session_id

    def close_realtime_session(
        self,
        *,
        session_id: str,
        terminal_reason: str = "completed",
    ) -> dict[str, Any]:
        self._check_active_realtime_session(session_id)
        logger.info(
            "motion realtime_close requested session_id=%s terminal_reason=%s request_id=%s",
            session_id,
            terminal_reason,
            self._realtime_request_id,
        )

        self._stop_realtime_worker()
        with self._realtime_lock:
            gen = self._control_gen
            self._control_gen = None
            self._realtime_active = False
            self._prev_targets = None
            self._last_terminal_reason = terminal_reason
            self.control_context.terminate_session(session_id)
            self._realtime_session_id = None
            self._realtime_request_id = None
        if gen is not None:
            self.arbiter.release(gen, holder="realtime")
        logger.info(
            "motion realtime_closed session_id=%s terminal_reason=%s gen=%s",
            session_id,
            terminal_reason,
            gen,
        )
        return {
            "accepted": True,
            "active": False,
            "session_id": session_id,
            "terminal_reason": terminal_reason,
        }

    def apply_realtime_command(
        self,
        *,
        session_id: str,
        targets: dict[str, list[float]] | None = None,
        q: list[float] | None = None,
        layout: list[str] | None = None,
        names: list[str] | None = None,
        poses: list[list[float]] | None = None,
        check_step_delta: bool = True,
        reacquire_if_needed: bool = False,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        self._check_active_realtime_session(session_id)
        self.arbiter.require_mode(ControlMode.REALTIME)
        cfg = self.config.realtime

        with self._realtime_lock:
            if not self._realtime_active:
                raise BridgeError("realtime_inactive", "realtime session not open", status_code=409)
            space = self._realtime_space
            control_way = self._realtime_control_way
            received_before = self._rt_targets_received

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
            logger.info(
                "motion realtime_command_applied session_id=%s space=cartesian names=%s seq_targets_received=%s",
                session_id,
                names,
                received_before + 1,
            )
            return {"accepted": True, "space": "cartesian", "names": names, "session_id": session_id}

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
                check_step_delta=False,
                max_step_delta_rad=cfg.max_step_delta_rad,
                max_abs_rad=cfg.max_abs_rad,
                gripper_min=cfg.gripper_min,
                gripper_max=cfg.gripper_max,
            )
            if violations:
                logger.warning(
                    "motion realtime_command_rejected session_id=%s names=%s violations=%s",
                    session_id,
                    names_out,
                    [v.reason for v in violations],
                )
                return {
                    "accepted": False,
                    "violations": [v.to_dict() for v in violations],
                    "message": "command rejected by safety checks",
                }

        with self._realtime_lock:
            self._realtime_check_step_delta = check_step_delta
            prefer_latest = self._prefer_latest

        now = time.time()
        with self._realtime_lock:
            if prefer_latest:
                # Latest-wins mailbox: overwrite target only; worker clamps each tick.
                if self._rt_cmd is None or self._rt_names != names_out:
                    self._rt_cmd = [list(g) for g in values]
                self._rt_from = None
                self._rt_to = [list(g) for g in values]
                self._rt_names = list(names_out)
                self._rt_blend_start = now
                self._rt_blend_s = 0.0
                blend_s = 0.0
            else:
                blend_s = self._compute_blend_s_unlocked(now)
                current = self._rt_cmd
                if current is None or self._rt_names != names_out:
                    # First frame or layout change: snap.
                    self._rt_from = [list(g) for g in values]
                    self._rt_cmd = [list(g) for g in values]
                    blend_s = min(blend_s, cfg.min_blend_s)
                else:
                    self._rt_from = [list(g) for g in current]
                self._rt_to = [list(g) for g in values]
                self._rt_names = list(names_out)
                self._rt_blend_start = now
                self._rt_blend_s = blend_s
            self._rt_last_target_ts = now
            self._rt_targets_received += 1
            self._prev_targets = {n: list(v) for n, v in zip(names_out, values)}
            control_hz = self._realtime_control_hz
            source_hz = self._realtime_source_hz
            ack_mode = self._ack_mode
            targets_received = self._rt_targets_received

        if targets_received == 1 or targets_received % 50 == 0:
            max_delta = 0.0
            with self._realtime_lock:
                if self._rt_cmd is not None and len(self._rt_cmd) == len(values):
                    max_delta = self._max_abs_delta(self._rt_cmd, values)
            logger.info(
                "motion realtime_command_accepted session_id=%s targets_received=%s names=%s max_delta_rad=%.4f prefer_latest=%s control_hz=%s source_hz=%s",
                session_id,
                targets_received,
                names_out,
                max_delta,
                prefer_latest,
                control_hz,
                source_hz,
            )

        return {
            "accepted": True,
            "space": "joints",
            "names": names_out,
            "blend_s": blend_s,
            "queued": False,
            "prefer_latest": prefer_latest,
            "ack_mode": ack_mode,
            "control_hz": control_hz,
            "source_hz": source_hz,
            "session_id": session_id,
        }

    def on_control_rights_lost(self) -> None:
        logger.warning(
            "motion control_rights_lost active_realtime_session=%s request_id=%s",
            self._realtime_session_id,
            self._realtime_request_id,
        )
        self._stop_realtime_worker()
        with self._realtime_lock:
            self._prev_targets = None
            self._control_gen = None
            self._realtime_session_id = None
            self._realtime_request_id = None
            self._last_terminal_reason = "control_lost"
        self.control_context.clear_for_estop()

    def _compute_blend_s_unlocked(self, now: float) -> float:
        cfg = self.config.realtime
        if self._realtime_source_hz is not None and self._realtime_source_hz > 0:
            blend = 1.0 / self._realtime_source_hz
        elif self._rt_last_target_ts is not None:
            blend = now - self._rt_last_target_ts
        else:
            blend = 1.0 / max(1.0, float(cfg.default_source_hz))
        return max(float(cfg.min_blend_s), min(float(cfg.max_blend_s), float(blend)))

    def _max_step_delta_per_sdk_tick_unlocked(self, source_hz: float | None, control_hz: float) -> float:
        cfg = self.config.realtime
        src = float(source_hz) if source_hz is not None and source_hz > 0 else float(cfg.default_source_hz)
        # Treat max_step_delta_rad as inter-client-frame limit; scale to control_hz output.
        return cfg.max_step_delta_rad * src / max(1.0, control_hz) * float(cfg.step_delta_slack)

    @staticmethod
    def _lerp_groups(start: list[list[float]], end: list[list[float]], alpha: float) -> list[list[float]]:
        a = max(0.0, min(1.0, alpha))
        out: list[list[float]] = []
        for g0, g1 in zip(start, end):
            out.append([v0 + (v1 - v0) * a for v0, v1 in zip(g0, g1)])
        return out

    def _realtime_worker_loop(self) -> None:
        logger.info("realtime interp worker started")
        while not self._rt_stop.is_set():
            loop_start = time.perf_counter()
            with self._realtime_lock:
                active = self._realtime_active and self._realtime_space == "joints"
                names = self._rt_names
                frm = self._rt_from
                to = self._rt_to
                blend_start = self._rt_blend_start
                blend_s = self._rt_blend_s
                control_way = self._realtime_control_way
                control_hz = self._realtime_control_hz
                source_hz = self._realtime_source_hz
                check_step = self._realtime_check_step_delta
                prev_cmd = self._rt_cmd
                prefer_latest = self._prefer_latest
            if not active:
                break
            if names is not None and to is not None and (prefer_latest or frm is not None):
                if prefer_latest:
                    cmd = [list(g) for g in to]
                else:
                    if blend_s <= 1e-6:
                        alpha = 1.0
                    else:
                        alpha = (time.time() - blend_start) / blend_s
                    cmd = self._lerp_groups(frm, to, alpha)  # type: ignore[arg-type]
                if (
                    check_step
                    and prev_cmd is not None
                    and len(prev_cmd) == len(cmd)
                ):
                    max_tick = self._max_step_delta_per_sdk_tick_unlocked(source_hz, control_hz)
                    cmd = clamp_group_step_delta(prev_cmd, cmd, max_tick)
                try:
                    self.robot.run_sync_with_timeout(
                        self.robot.set_joints_position,
                        names,
                        cmd,
                        timeout_s=1.0,
                        control_way=control_way,
                        use_wbc=False,
                    )
                    with self._realtime_lock:
                        self._rt_cmd = cmd
                        self._rt_ticks += 1
                except Exception:
                    logger.exception(
                        "realtime interp set_joints failed session_id=%s names=%s tick=%s",
                        self._realtime_session_id,
                        names,
                        self._rt_ticks,
                    )
            period = 1.0 / max(1.0, control_hz)
            elapsed = time.perf_counter() - loop_start
            sleep_s = period - elapsed
            if sleep_s > 0:
                self._rt_stop.wait(timeout=sleep_s)
        logger.info(
            "realtime interp worker stopped session_id=%s ticks=%s targets_received=%s",
            self._realtime_session_id,
            self._rt_ticks,
            self._rt_targets_received,
        )

    def _stop_realtime_worker(self) -> None:
        with self._realtime_lock:
            self._realtime_active = False
            self._rt_stop.set()
            worker = self._rt_worker
            self._rt_worker = None
            self._rt_names = None
            self._rt_from = None
            self._rt_to = None
            self._rt_cmd = None
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=1.0)

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
        self._stop_realtime_worker()
        with self._realtime_lock:
            self._control_gen = None
            self._prev_targets = None
            self._realtime_session_id = None
            self._realtime_request_id = None
            self._last_terminal_reason = "stopped"
        self.control_context.clear_for_estop()
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
