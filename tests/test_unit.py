"""Unit tests that do not require the Astribot SDK."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.domain.arbiter import ControlArbiter, ControlMode
from bridge.domain.control_rights import ControlRightsManager
from bridge.config import ControlRightsConfig
from bridge.robot.joint_model import READABLE_DOF, TRAJECTORY_DOF, dict_to_groups, flat_to_groups, flatten_groups
from bridge.schemas.common import ControlBusyError
from bridge.safety.limits import check_joint_targets


class _FakeRobot:
    def __init__(self, have_control_rights: bool = True) -> None:
        self.have_control_rights = have_control_rights

    def get_control_rights_status(self) -> bool:
        return self.have_control_rights

    def run_sync_with_timeout(self, fn, *args, timeout_s: float, **kwargs):
        return fn(*args, **kwargs)

    def reacquire_control_rights(self, *, force: bool = True) -> bool:
        self.have_control_rights = True
        return True


def test_joint_dof_constants() -> None:
    assert TRAJECTORY_DOF == 22
    assert READABLE_DOF == 25


def test_flatten_roundtrip() -> None:
    names = ["astribot_head"]
    groups = [[0.1, -0.2]]
    flat = flatten_groups(names, groups)
    assert flat_to_groups(flat, names) == groups


def test_dict_to_groups() -> None:
    names, values = dict_to_groups({"astribot_head": [0.0, 0.1]})
    assert names == ["astribot_head"]
    assert values == [[0.0, 0.1]]


def test_arbiter_acquire_release() -> None:
    arb = ControlArbiter()
    gen = arb.acquire(ControlMode.TRAJECTORY, "traj")
    assert arb.snapshot().mode == ControlMode.TRAJECTORY
    try:
        arb.acquire(ControlMode.MOVE_TO, "move")
        assert False, "expected busy"
    except ControlBusyError:
        pass
    arb.release(gen)
    assert arb.snapshot().mode == ControlMode.IDLE


def test_arbiter_force() -> None:
    arb = ControlArbiter()
    cancelled = []
    arb.acquire(ControlMode.TRAJECTORY, "traj", on_cancel=lambda: cancelled.append(1))
    arb.acquire(ControlMode.REALTIME, "rt", force=True)
    assert cancelled == [1]
    assert arb.snapshot().mode == ControlMode.REALTIME


def test_safety_gripper() -> None:
    violations = check_joint_targets(
        ["astribot_gripper_left"],
        [[150.0]],
        gripper_min=0.0,
        gripper_max=100.0,
    )
    assert violations and violations[0].reason == "gripper_out_of_range"


def test_control_rights_loss_and_reacquire() -> None:
    arb = ControlArbiter()
    robot = _FakeRobot(True)
    rights = ControlRightsManager(
        robot,
        arb,
        config=ControlRightsConfig(
            auto_reacquire=True,
            reacquire_cooldown_s=0.0,
            max_reacquire_attempts_per_loss=1,
            probe_interval_s=10.0,
        ),
    )
    cancelled = []
    arb.acquire(ControlMode.REALTIME, "rt", on_cancel=lambda: cancelled.append(1))

    robot.have_control_rights = False
    assert rights.refresh() is False
    assert rights.snapshot()["have_control_rights"] is False
    assert rights.snapshot()["degraded"] is True
    assert cancelled == [1]

    data = rights.reacquire()
    assert data["have_control_rights"] is True
    assert data["degraded"] is False


if __name__ == "__main__":
    test_joint_dof_constants()
    test_flatten_roundtrip()
    test_dict_to_groups()
    test_arbiter_acquire_release()
    test_arbiter_force()
    test_safety_gripper()
    test_control_rights_loss_and_reacquire()
    print("ok")
