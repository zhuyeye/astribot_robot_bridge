"""Trajectory playback service (pre-recorded HDF5 actions)."""

from __future__ import annotations

import logging
import random
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge.domain.arbiter import ControlArbiter, ControlMode
from bridge.domain.control_rights import ControlRightsManager
from bridge.domain.trajectory_lib import ActionTrajectory, load_action_library, load_action_tags
from bridge.robot.adapter import RobotAdapter
from bridge.schemas.common import BridgeError

logger = logging.getLogger(__name__)

STATE_HOLDING = "holding"
STATE_PLAYING = "playing"


@dataclass
class PlayCommand:
    action_id: str
    request_id: str
    force: bool
    generation: int


class TrajectoryService:
    def __init__(
        self,
        robot: RobotAdapter,
        arbiter: ControlArbiter,
        control_rights: ControlRightsManager,
        *,
        manifest_path: Path,
        force_rebuild: bool = False,
    ) -> None:
        self.robot = robot
        self.arbiter = arbiter
        self.control_rights = control_rights
        self.manifest_path = manifest_path
        self.actions: dict[str, ActionTrajectory] = {}
        self.action_tags: dict[str, list[str]] = {}
        # play(force=True) may cancel the current trajectory while already holding
        # the condition lock in this thread, so this lock must be re-entrant.
        self._lock = threading.RLock()
        self._play_cv = threading.Condition(self._lock)
        self._shutdown = threading.Event()
        self._pending: PlayCommand | None = None
        self._local_generation = 0
        self.state = STATE_HOLDING
        self.current_action_id: str | None = None
        self.current_request_id: str | None = None
        self.last_error: str | None = None
        self._last_cmd: list[list[float]] | None = None
        self._run_state = STATE_HOLDING
        self._control_gen: int | None = None
        self._worker: threading.Thread | None = None

        if manifest_path.is_file():
            self.reload(force_rebuild=force_rebuild)
        else:
            logger.warning("action manifest not found: %s", manifest_path)

        self._worker = threading.Thread(target=self._worker_loop, name="traj-worker", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._shutdown.set()
        with self._play_cv:
            self._play_cv.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=2.0)

    def reload(self, *, force_rebuild: bool = False) -> dict[str, Any]:
        self.actions = load_action_library(self.manifest_path, force_rebuild=force_rebuild)
        self.action_tags = {
            tag: [aid for aid in aids if aid in self.actions]
            for tag, aids in load_action_tags(self.manifest_path).items()
        }
        return {"count": len(self.actions), "actions": self.list_action_ids()}

    def list_action_ids(self) -> list[str]:
        requestable = set(self.actions.keys())
        requestable.update(tag for tag, aids in self.action_tags.items() if aids)
        return sorted(requestable)

    def get_action(self, action_id: str) -> dict[str, Any]:
        resolved = self._resolve_action_id(action_id)
        if resolved is None or resolved not in self.actions:
            raise BridgeError("unknown_action", f"unknown action_id: {action_id}", status_code=404)
        action = self.actions[resolved]
        return {
            "action_id": action.action_id,
            "path": str(action.path),
            "frames": action.frames,
            "duration_s": action.duration_s,
            "loop": action.loop,
            "transition_s": action.transition_s,
            "control_hz": action.control_hz,
            "names": action.names,
        }

    def list_actions(self) -> list[dict[str, Any]]:
        return [self.get_action(aid) for aid in sorted(self.actions.keys())]

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "run_state": self._run_state,
                "action_id": self.current_action_id,
                "request_id": self.current_request_id,
                "generation": self._local_generation,
                "error": self.last_error,
            }

    def play(
        self,
        action_id: str,
        *,
        request_id: str = "",
        force: bool = False,
        reacquire_if_needed: bool = False,
    ) -> dict[str, Any]:
        self.control_rights.ensure(reacquire_if_needed=reacquire_if_needed)
        requested_tag_members = self.action_tags.get(action_id, [])
        resolved = self._resolve_action_id(action_id)
        if resolved is None:
            raise BridgeError("unknown_action", f"unknown action_id: {action_id}", status_code=404)
        logger.info(
            "trajectory play requested action=%s resolved=%s force=%s request_id=%s state=%s current=%s",
            action_id,
            resolved,
            force,
            request_id,
            self.state,
            self.current_action_id,
        )

        with self._play_cv:
            if (
                not force
                and self.state == STATE_PLAYING
                and self.current_action_id == resolved
            ):
                return {
                    "accepted": False,
                    "message": f"already playing action_id={resolved}",
                    "state": self.state,
                    "action_id": self.current_action_id,
                    "request_id": self.current_request_id,
                }
            if (
                not force
                and self.state == STATE_PLAYING
                and requested_tag_members
                and self.current_action_id in requested_tag_members
            ):
                return {
                    "accepted": False,
                    "message": f"already playing tag={action_id} via action_id={self.current_action_id}",
                    "state": self.state,
                    "action_id": self.current_action_id,
                    "request_id": self.current_request_id,
                }

            def _cancel() -> None:
                with self._play_cv:
                    self._local_generation += 1
                    self._pending = None
                    self._play_cv.notify_all()

            self._control_gen = self.arbiter.acquire(
                ControlMode.TRAJECTORY,
                holder=f"trajectory:{resolved}",
                force=force,
                on_cancel=_cancel,
            )
            self._local_generation += 1
            self._pending = PlayCommand(
                action_id=resolved,
                request_id=request_id,
                force=force,
                generation=self._local_generation,
            )
            self.state = STATE_PLAYING
            self._run_state = "transitioning"
            self.current_action_id = resolved
            self.current_request_id = request_id
            self.last_error = None
            self._play_cv.notify_all()

        return {
            "accepted": True,
            "state": STATE_PLAYING,
            "action_id": resolved,
            "request_id": request_id,
            "generation": self._local_generation,
        }

    def stop(self) -> dict[str, Any]:
        with self._play_cv:
            self._local_generation += 1
            self._pending = None
            self.state = STATE_HOLDING
            self._run_state = STATE_HOLDING
            self._play_cv.notify_all()
        if self._control_gen is not None:
            self.arbiter.release(self._control_gen)
            self._control_gen = None
        return self.get_status()

    def interrupt_for_estop(self) -> None:
        with self._play_cv:
            self._local_generation += 1
            self._pending = None
            self.state = STATE_HOLDING
            self._run_state = STATE_HOLDING
            self.current_action_id = None
            self.current_request_id = None
            self._play_cv.notify_all()
        self._control_gen = None

    def on_control_rights_lost(self) -> None:
        with self._play_cv:
            self._local_generation += 1
            self._pending = None
            self.state = STATE_HOLDING
            self._run_state = "control_lost"
            self.current_action_id = None
            self.current_request_id = None
            self.last_error = "control rights lost"
            self._play_cv.notify_all()
        self._control_gen = None

    def _resolve_action_id(self, requested: str) -> str | None:
        tag_matches = self.action_tags.get(requested, [])
        if tag_matches:
            return random.choice(tag_matches)
        if requested in self.actions:
            return requested
        return None

    def _worker_loop(self) -> None:
        while not self._shutdown.is_set():
            with self._play_cv:
                while self._pending is None and not self._shutdown.is_set():
                    self._play_cv.wait(timeout=0.1)
                if self._shutdown.is_set():
                    return
                cmd = self._pending
                self._pending = None

            assert cmd is not None
            try:
                self._play_action(cmd)
            except Exception as exc:
                with self._lock:
                    self.last_error = str(exc)
                    self.state = STATE_HOLDING
                    self._run_state = "error"
                self.arbiter.set_error(str(exc))
                logger.exception("playback error")
                traceback.print_exc()

            with self._lock:
                if self._pending is None and self._local_generation == cmd.generation:
                    self.state = STATE_HOLDING
                    self._run_state = STATE_HOLDING
                    if self._control_gen is not None:
                        self.arbiter.release(self._control_gen)
                        self._control_gen = None

    def _play_action(self, cmd: PlayCommand) -> None:
        action = self.actions[cmd.action_id]
        self._transition_to_first_frame(action, cmd)
        if self._shutdown.is_set() or self._local_generation != cmd.generation:
            return
        self._play_frames(action, cmd)

    def _calc_transition_duration(
        self,
        current: list[list[float]],
        target: list[list[float]],
        action: ActionTrajectory,
    ) -> float:
        if action.transition_s is not None:
            return float(action.transition_s)
        max_delta = 0.0
        for current_group, target_group in zip(current, target):
            for a, b in zip(current_group, target_group):
                max_delta = max(max_delta, abs(a - b))
        if max_delta < 0.2:
            return 0.4
        if max_delta < 0.6:
            return 0.7
        return 1.0

    def _get_current_command(self, action: ActionTrajectory) -> list[list[float]]:
        if self._last_cmd is not None:
            return [list(g) for g in self._last_cmd]
        current = self.robot.astribot.get_desired_joints_position(action.names)
        return [list(g) for g in current]

    def _transition_to_first_frame(self, action: ActionTrajectory, cmd: PlayCommand) -> None:
        target = [list(g) for g in action.waypoints[0]]
        current = self._get_current_command(action)
        duration = self._calc_transition_duration(current, target, action)
        with self._lock:
            self._run_state = "transitioning"
        self.robot.run_sync_with_timeout(
            self.robot.move_joints_position,
            action.names,
            target,
            timeout_s=max(3.0, duration + 2.0),
            duration=duration,
            use_wbc=False,
        )
        self._last_cmd = target
        if self._local_generation != cmd.generation:
            return

    def _play_frames(self, action: ActionTrajectory, cmd: PlayCommand) -> None:
        dt = 1.0 / action.control_hz
        with self._lock:
            self._run_state = STATE_PLAYING

        while not self._shutdown.is_set() and self._local_generation == cmd.generation:
            for waypoint in action.waypoints:
                if self._shutdown.is_set() or self._local_generation != cmd.generation:
                    return
                t0 = time.perf_counter()
                self.robot.run_sync_with_timeout(
                    self.robot.set_joints_position,
                    action.names,
                    waypoint,
                    timeout_s=1.0,
                    control_way="filter",
                    use_wbc=False,
                )
                self._last_cmd = [list(g) for g in waypoint]
                elapsed = time.perf_counter() - t0
                sleep_s = dt - elapsed
                if sleep_s > 0:
                    time.sleep(sleep_s)
            if not action.loop:
                return
