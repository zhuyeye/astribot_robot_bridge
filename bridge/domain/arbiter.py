"""Single-owner motion control arbitration."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from bridge.schemas.common import BridgeError, ControlBusyError


class ControlMode(str, Enum):
    IDLE = "idle"
    TRAJECTORY = "trajectory"
    MOVE_TO = "move_to"
    REALTIME = "realtime"


@dataclass
class ControlSnapshot:
    mode: ControlMode
    holder: str | None
    generation: int
    since: float | None
    last_error: str | None


class ControlArbiter:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mode = ControlMode.IDLE
        self._holder: str | None = None
        self._generation = 0
        self._since: float | None = None
        self._last_error: str | None = None
        self._cancel_callbacks: dict[int, Any] = {}

    def snapshot(self) -> ControlSnapshot:
        with self._lock:
            return ControlSnapshot(
                mode=self._mode,
                holder=self._holder,
                generation=self._generation,
                since=self._since,
                last_error=self._last_error,
            )

    def acquire(
        self,
        mode: ControlMode,
        holder: str,
        *,
        force: bool = False,
        on_cancel: Any | None = None,
    ) -> int:
        if mode == ControlMode.IDLE:
            raise BridgeError("invalid_mode", "cannot acquire idle mode")

        with self._lock:
            if self._mode != ControlMode.IDLE and not force:
                if self._mode == mode and self._holder == holder:
                    return self._generation
                raise ControlBusyError(self._holder or self._mode.value)

            if self._mode != ControlMode.IDLE and force:
                self._invoke_cancel_unlocked()

            self._generation += 1
            self._mode = mode
            self._holder = holder
            self._since = time.time()
            self._last_error = None
            if on_cancel is not None:
                self._cancel_callbacks[self._generation] = on_cancel
            return self._generation

    def release(self, generation: int | None = None, *, holder: str | None = None) -> None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            if holder is not None and holder != self._holder:
                return
            self._cancel_callbacks.pop(self._generation, None)
            self._mode = ControlMode.IDLE
            self._holder = None
            self._since = None

    def bump_and_clear(self) -> int:
        """Force-clear holder (e.g. estop). Returns new generation."""
        with self._lock:
            self._invoke_cancel_unlocked()
            self._generation += 1
            self._mode = ControlMode.IDLE
            self._holder = None
            self._since = None
            return self._generation

    def set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def require_mode(self, mode: ControlMode) -> int:
        with self._lock:
            if self._mode != mode:
                raise BridgeError(
                    "wrong_mode",
                    f"expected mode {mode.value}, current is {self._mode.value}",
                    status_code=409,
                    details={"mode": self._mode.value, "holder": self._holder},
                )
            return self._generation

    def _invoke_cancel_unlocked(self) -> None:
        cb = self._cancel_callbacks.pop(self._generation, None)
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def to_dict(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "mode": snap.mode.value,
            "holder": snap.holder,
            "generation": snap.generation,
            "since": snap.since,
            "last_error": snap.last_error,
        }
