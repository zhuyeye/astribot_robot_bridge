"""Shared control context for session fencing across motion services."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

ControlContextMode = Literal["holding", "playback", "realtime", "move_to"]
SessionMode = Literal["playback", "realtime"]


@dataclass
class ControlContextSnapshot:
    active_session_id: str | None
    active_mode: ControlContextMode
    active_epoch: int
    last_terminal_session_id: str | None
    last_terminal_epoch: int | None


class ControlContext:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_session_id: str | None = None
        self._active_mode: ControlContextMode = "holding"
        self._active_epoch = 0
        self._last_terminal_session_id: str | None = None
        self._last_terminal_epoch: int | None = None

    def snapshot(self) -> ControlContextSnapshot:
        with self._lock:
            return ControlContextSnapshot(
                active_session_id=self._active_session_id,
                active_mode=self._active_mode,
                active_epoch=self._active_epoch,
                last_terminal_session_id=self._last_terminal_session_id,
                last_terminal_epoch=self._last_terminal_epoch,
            )

    def to_dict(self) -> dict[str, object]:
        snap = self.snapshot()
        return {
            "active_session_id": snap.active_session_id,
            "active_mode": snap.active_mode,
            "active_epoch": snap.active_epoch,
            "last_terminal_session_id": snap.last_terminal_session_id,
            "last_terminal_epoch": snap.last_terminal_epoch,
        }

    def matches_expected(self, expected_session_id: str | None) -> bool:
        if not expected_session_id:
            return True
        with self._lock:
            if self._active_session_id == expected_session_id:
                return True
            if self._active_session_id is not None:
                return False
            return self._last_terminal_session_id == expected_session_id

    def issue_session(self, mode: SessionMode, generation: int) -> tuple[str, int]:
        with self._lock:
            prev_session_id = self._active_session_id
            prev_mode = self._active_mode
            self._active_epoch += 1
            session_id = f"{mode}:{generation}"
            self._active_session_id = session_id
            self._active_mode = mode
            logger.info(
                "control_context session_issued session_id=%s mode=%s epoch=%s prev_session_id=%s prev_mode=%s",
                session_id,
                mode,
                self._active_epoch,
                prev_session_id,
                prev_mode,
            )
            return session_id, self._active_epoch

    def start_transient(self, mode: Literal["move_to"]) -> int:
        with self._lock:
            prev_mode = self._active_mode
            self._active_epoch += 1
            self._active_mode = mode
            logger.info(
                "control_context transient_started mode=%s epoch=%s active_session_id=%s prev_mode=%s",
                mode,
                self._active_epoch,
                self._active_session_id,
                prev_mode,
            )
            return self._active_epoch

    def finish_transient(self, epoch: int) -> None:
        with self._lock:
            if self._active_epoch != epoch or self._active_session_id is not None:
                return
            self._active_mode = "holding"
            logger.info(
                "control_context transient_finished mode=holding epoch=%s active_session_id=%s",
                epoch,
                self._active_session_id,
            )

    def terminate_session(self, session_id: str, *, clear_mode: bool = True) -> bool:
        with self._lock:
            if self._active_session_id != session_id:
                logger.info(
                    "control_context terminate_skipped session_id=%s active_session_id=%s active_mode=%s",
                    session_id,
                    self._active_session_id,
                    self._active_mode,
                )
                return False
            self._last_terminal_session_id = session_id
            self._last_terminal_epoch = self._active_epoch
            self._active_session_id = None
            if clear_mode:
                self._active_mode = "holding"
            logger.info(
                "control_context session_terminated session_id=%s active_mode=%s epoch=%s",
                session_id,
                self._active_mode,
                self._active_epoch,
            )
            return True

    def clear_for_estop(self) -> None:
        with self._lock:
            prev_session_id = self._active_session_id
            prev_mode = self._active_mode
            if self._active_session_id is not None:
                self._last_terminal_session_id = self._active_session_id
                self._last_terminal_epoch = self._active_epoch
            self._active_session_id = None
            self._active_mode = "holding"
            logger.warning(
                "control_context cleared_for_estop prev_session_id=%s prev_mode=%s epoch=%s",
                prev_session_id,
                prev_mode,
                self._active_epoch,
            )
