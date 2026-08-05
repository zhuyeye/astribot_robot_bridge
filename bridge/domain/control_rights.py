"""Control-rights state, probing, and reacquire helpers."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from bridge.config import ControlRightsConfig
from bridge.domain.arbiter import ControlArbiter
from bridge.robot.adapter import RobotAdapter
from bridge.schemas.common import BridgeError

logger = logging.getLogger(__name__)


class ControlRightsManager:
    def __init__(
        self,
        control_robot: RobotAdapter,
        arbiter: ControlArbiter,
        *,
        config: ControlRightsConfig,
    ) -> None:
        self.control_robot = control_robot
        self.arbiter = arbiter
        self.config = config
        self.probe_interval_s = config.probe_interval_s
        self._lock = threading.RLock()
        self._have_control_rights = bool(control_robot.get_control_rights_status())
        self._degraded = False
        self._last_change_ts = time.time()
        self._last_reacquire_ts: float | None = None
        self._last_error: str | None = None
        self._last_loss_ts: float | None = None
        self._loss_epoch = 0
        self._reacquire_attempts_since_loss = 0
        self._shutdown = threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._probe = threading.Thread(target=self._probe_loop, name="control-rights-probe", daemon=True)

    def start(self) -> None:
        self._probe.start()

    def close(self) -> None:
        self._shutdown.set()
        self._probe.join(timeout=2.0)

    def register_loss_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "have_control_rights": self._have_control_rights,
                "degraded": self._degraded,
                "last_change_ts": self._last_change_ts,
                "last_loss_ts": self._last_loss_ts,
                "last_reacquire_ts": self._last_reacquire_ts,
                "last_error": self._last_error,
                "auto_reacquire": self.config.auto_reacquire,
                "reacquire_cooldown_s": self.config.reacquire_cooldown_s,
                "reacquire_attempts_since_loss": self._reacquire_attempts_since_loss,
                "max_reacquire_attempts_per_loss": self.config.max_reacquire_attempts_per_loss,
                "bridge_mode": self.arbiter.snapshot().mode.value,
                "holder": self.arbiter.snapshot().holder,
            }

    def have_control_rights(self) -> bool:
        with self._lock:
            return self._have_control_rights

    def mark_lost(self, *, reason: str | None = None) -> None:
        with self._lock:
            previous = self._have_control_rights
            self._have_control_rights = False
            self._degraded = True
            self._last_change_ts = time.time()
            self._last_loss_ts = self._last_change_ts
            self._loss_epoch += 1
            self._reacquire_attempts_since_loss = 0
            if reason:
                self._last_error = reason
        if previous:
            logger.warning("control rights lost%s", f": {reason}" if reason else "")
            self.arbiter.bump_and_clear()
            for callback in self._callbacks:
                try:
                    callback()
                except Exception:
                    logger.exception("control-rights loss callback failed")

    def refresh(self) -> bool:
        try:
            current = bool(self.control_robot.get_control_rights_status())
        except Exception as exc:
            self.mark_lost(reason=str(exc))
            return False

        trigger_loss = False
        with self._lock:
            previous = self._have_control_rights
            if previous != current:
                self._last_change_ts = time.time()
                if current:
                    self._have_control_rights = True
                    self._degraded = False
                    self._last_error = None
                else:
                    trigger_loss = True
            elif current:
                self._have_control_rights = True

        if trigger_loss:
            self.mark_lost(reason="control rights transferred away")
        return current

    def ensure(self, *, reacquire_if_needed: bool | None = None) -> None:
        if self.refresh():
            return
        if reacquire_if_needed is None:
            should_reacquire = self.config.auto_reacquire
        else:
            should_reacquire = reacquire_if_needed
        if should_reacquire:
            self.reacquire(ignore_cooldown=False)
            return
        raise BridgeError(
            "control_rights_lost",
            "bridge no longer has robot control rights",
            status_code=409,
            details=self.snapshot(),
        )

    def reacquire(self, *, ignore_cooldown: bool = True) -> dict[str, Any]:
        if not ignore_cooldown:
            self._guard_auto_reacquire()
        try:
            ok = self.control_robot.run_sync_with_timeout(
                self.control_robot.reacquire_control_rights,
                timeout_s=3.0,
                force=True,
            )
        except Exception as exc:
            with self._lock:
                self._degraded = True
                self._last_error = str(exc)
            raise BridgeError(
                "control_rights_reacquire_failed",
                str(exc),
                status_code=503,
                details=self.snapshot(),
            ) from exc

        with self._lock:
            self._have_control_rights = bool(ok)
            self._degraded = not bool(ok)
            self._last_change_ts = time.time()
            self._last_reacquire_ts = self._last_change_ts
            self._reacquire_attempts_since_loss += 1
            self._last_error = None if ok else "reacquire returned false"

        if not ok:
            raise BridgeError(
                "control_rights_reacquire_failed",
                "SDK reacquire returned false",
                status_code=503,
                details=self.snapshot(),
            )
        return self.snapshot()

    def _guard_auto_reacquire(self) -> None:
        with self._lock:
            attempts = self._reacquire_attempts_since_loss
            last_loss_ts = self._last_loss_ts
            last_reacquire_ts = self._last_reacquire_ts
        if attempts >= self.config.max_reacquire_attempts_per_loss:
            raise BridgeError(
                "control_rights_lost",
                "auto reacquire attempts exhausted for current loss event",
                status_code=409,
                details=self.snapshot(),
            )
        if (
            last_reacquire_ts is not None
            and last_loss_ts is not None
            and last_reacquire_ts >= last_loss_ts
            and (time.time() - last_reacquire_ts) < self.config.reacquire_cooldown_s
        ):
            raise BridgeError(
                "control_rights_cooldown",
                "waiting for control-rights reacquire cooldown",
                status_code=409,
                details=self.snapshot(),
            )

    def _probe_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.refresh()
            except Exception:
                logger.exception("control-rights probe failed")
            self._shutdown.wait(self.probe_interval_s)
