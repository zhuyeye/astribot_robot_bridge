"""Unit tests that do not require the Astribot SDK."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.domain.arbiter import ControlArbiter, ControlMode
from bridge.domain.control_context import ControlContext
from bridge.domain.control_rights import ControlRightsManager
from bridge.config import ControlRightsConfig
from bridge.robot.joint_model import READABLE_DOF, TRAJECTORY_DOF, dict_to_groups, flat_to_groups, flatten_groups
from bridge.schemas.common import ControlBusyError
from bridge.safety.limits import check_joint_targets, clamp_group_step_delta


class _FakeRobot:
    def __init__(self, have_control_rights: bool = True) -> None:
        self.have_control_rights = have_control_rights
        self.set_joints_calls = 0
        self.move_joints_calls = 0
        self.astribot = self

    def get_control_rights_status(self) -> bool:
        return self.have_control_rights

    def run_sync_with_timeout(self, fn, *args, timeout_s: float, **kwargs):
        return fn(*args, **kwargs)

    def set_joints_position(self, names, values, **kwargs):
        self.set_joints_calls += 1
        return None

    def move_joints_position(self, names, values, **kwargs):
        self.move_joints_calls += 1
        return None

    def get_desired_joints_position(self, names):
        return [[0.0] * (2 if name == "astribot_head" else 1) for name in names]

    def reacquire_control_rights(self, *, force: bool = True) -> bool:
        self.have_control_rights = True
        return True


def _make_motion_service(cfg=None):
    from bridge.config import BridgeConfig
    from bridge.domain.motion_service import MotionService

    robot = _FakeRobot()
    arb = ControlArbiter()
    rights = ControlRightsManager(
        robot,
        arb,
        config=ControlRightsConfig(auto_reacquire=False, probe_interval_s=10.0),
    )
    svc = MotionService(robot, arb, rights, ControlContext(), cfg or BridgeConfig())
    return svc, robot, arb


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


def test_realtime_lerp_groups() -> None:
    from bridge.domain.motion_service import MotionService

    out = MotionService._lerp_groups([[0.0, 0.0], [1.0]], [[2.0, 4.0], [3.0]], 0.5)
    assert out == [[1.0, 2.0], [2.0]]
    assert MotionService._lerp_groups([[0.0]], [[10.0]], 0.0) == [[0.0]]
    assert MotionService._lerp_groups([[0.0]], [[10.0]], 1.0) == [[10.0]]


def test_realtime_blend_s_source_hz_and_default() -> None:
    from bridge.config import BridgeConfig, RealtimeConfig
    from bridge.domain.motion_service import MotionService

    robot = _FakeRobot()
    arb = ControlArbiter()
    rights = ControlRightsManager(
        robot,
        arb,
        config=ControlRightsConfig(auto_reacquire=False, probe_interval_s=10.0),
    )
    cfg = BridgeConfig(
        realtime=RealtimeConfig(
            control_hz=250.0,
            default_source_hz=30.0,
            min_blend_s=0.01,
            max_blend_s=0.2,
        )
    )
    svc = MotionService(robot, arb, rights, ControlContext(), cfg)

    svc._realtime_source_hz = 50.0
    assert abs(svc._compute_blend_s_unlocked(1000.0) - 0.02) < 1e-9

    svc._realtime_source_hz = None
    svc._rt_last_target_ts = None
    assert abs(svc._compute_blend_s_unlocked(1000.0) - (1.0 / 30.0)) < 1e-9

    svc._rt_last_target_ts = 999.95  # 50ms gap
    assert abs(svc._compute_blend_s_unlocked(1000.0) - 0.05) < 1e-9

    tick = svc._max_step_delta_per_sdk_tick_unlocked(50.0, 250.0)
    assert abs(tick - 0.35 * 50.0 / 250.0 * 2.0) < 1e-9


def test_clamp_group_step_delta() -> None:
    out = clamp_group_step_delta([[0.0, 1.0]], [[0.5, 2.0]], 0.2)
    assert out == [[0.2, 1.2]]


def test_realtime_client_frame_skips_step_delta_reject() -> None:
    from bridge.config import BridgeConfig, RealtimeConfig
    from bridge.domain.motion_service import MotionService

    robot = _FakeRobot()
    arb = ControlArbiter()
    rights = ControlRightsManager(
        robot,
        arb,
        config=ControlRightsConfig(auto_reacquire=False, probe_interval_s=10.0),
    )
    cfg = BridgeConfig(realtime=RealtimeConfig(max_step_delta_rad=0.01))
    svc = MotionService(robot, arb, rights, ControlContext(), cfg)
    svc._realtime_active = True
    svc._realtime_space = "joints"
    svc._prev_targets = {"astribot_head": [0.0, 0.0]}
    gen = arb.acquire(ControlMode.REALTIME, "rt")
    session_id, _ = svc.control_context.issue_session("realtime", gen)
    svc._realtime_session_id = session_id
    svc._control_gen = gen
    # Would fail old client step check (0.5 >> 0.01); should accept target for interp.
    result = svc.apply_realtime_command(
        session_id=session_id,
        targets={"astribot_head": [0.5, 0.0]},
        check_step_delta=True,
    )
    assert result["accepted"] is True
    assert result["queued"] is False
    assert result["prefer_latest"] is True



def test_realtime_prefer_latest_overwrites_target() -> None:
    from bridge.config import BridgeConfig, RealtimeConfig
    from bridge.domain.motion_service import MotionService

    robot = _FakeRobot()
    arb = ControlArbiter()
    rights = ControlRightsManager(
        robot,
        arb,
        config=ControlRightsConfig(auto_reacquire=False, probe_interval_s=10.0),
    )
    cfg = BridgeConfig(realtime=RealtimeConfig(max_step_delta_rad=0.35, step_delta_slack=2.0))
    svc = MotionService(robot, arb, rights, ControlContext(), cfg)
    opened = svc.open_realtime_session(
        source_hz=50.0,
        control_hz=250.0,
        space="joints",
        force=True,
        prefer_latest=True,
        ack_mode="drain_async",
    )
    assert opened["prefer_latest"] is True
    assert opened["ack_mode"] == "drain_async"
    assert opened["interpolate"] is False

    session_id = opened["session_id"]
    r1 = svc.apply_realtime_command(session_id=session_id, targets={"astribot_head": [0.1, 0.0]})
    assert r1["accepted"] is True
    assert r1["queued"] is False
    with svc._realtime_lock:
        assert svc._rt_to == [[0.1, 0.0]]

    r2 = svc.apply_realtime_command(session_id=session_id, targets={"astribot_head": [0.9, 0.2]})
    assert r2["accepted"] is True
    with svc._realtime_lock:
        # Latest wins: second frame replaces mailbox target.
        assert svc._rt_to == [[0.9, 0.2]]
        assert svc._rt_from is None

    svc.close_realtime_session(session_id=session_id)


def test_realtime_ack_mode_normalize() -> None:
    from bridge.domain.motion_service import MotionService

    assert MotionService._normalize_ack_mode("none") == "none"
    assert MotionService._normalize_ack_mode("drain_async") == "drain_async"
    assert MotionService._normalize_ack_mode("weird") == "every"
    assert MotionService._normalize_ack_mode(None) == "every"


def test_realtime_blend_path_when_prefer_latest_false() -> None:
    from bridge.config import BridgeConfig, RealtimeConfig
    from bridge.domain.motion_service import MotionService

    robot = _FakeRobot()
    arb = ControlArbiter()
    rights = ControlRightsManager(
        robot,
        arb,
        config=ControlRightsConfig(auto_reacquire=False, probe_interval_s=10.0),
    )
    cfg = BridgeConfig(
        realtime=RealtimeConfig(
            control_hz=250.0,
            default_source_hz=30.0,
            min_blend_s=0.01,
            max_blend_s=0.2,
        )
    )
    svc = MotionService(robot, arb, rights, ControlContext(), cfg)
    opened = svc.open_realtime_session(
        source_hz=50.0,
        control_hz=250.0,
        prefer_latest=False,
        ack_mode="every",
    )
    assert opened["prefer_latest"] is False
    assert opened["interpolate"] is True
    session_id = opened["session_id"]
    r = svc.apply_realtime_command(session_id=session_id, targets={"astribot_head": [0.1, 0.0]})
    assert r["accepted"] is True
    assert r["prefer_latest"] is False
    with svc._realtime_lock:
        assert svc._rt_from is not None
        # First frame snaps with min_blend_s.
        assert abs(svc._rt_blend_s - 0.01) < 1e-9
    r2 = svc.apply_realtime_command(session_id=session_id, targets={"astribot_head": [0.2, 0.0]})
    assert r2["accepted"] is True
    with svc._realtime_lock:
        assert abs(svc._rt_blend_s - 0.02) < 1e-9
    svc.close_realtime_session(session_id=session_id)


def test_control_context_matches_last_terminal_session() -> None:
    ctx = ControlContext()
    session_id, _ = ctx.issue_session("realtime", 12)
    assert ctx.matches_expected(session_id) is True
    assert ctx.terminate_session(session_id) is True
    assert ctx.matches_expected(session_id) is True


def test_realtime_command_rejects_stale_session() -> None:
    svc, _, _ = _make_motion_service()
    opened = svc.open_realtime_session(force=True)
    try:
        svc.apply_realtime_command(session_id="realtime:999", targets={"astribot_head": [0.1, 0.0]})
        assert False, "expected stale session"
    except Exception as exc:
        assert getattr(exc, "code", None) == "stale_session"
    finally:
        svc.close_realtime_session(session_id=opened["session_id"])


def test_move_to_rejects_stale_expected_session() -> None:
    svc, robot, _ = _make_motion_service()
    opened = svc.open_realtime_session(force=True)
    svc.close_realtime_session(session_id=opened["session_id"])
    result = svc.move_to_joints(
        {"astribot_head": [0.1, 0.0]},
        wait=True,
        expected_current_session_id=opened["session_id"],
    )
    assert result["accepted"] is True
    assert robot.move_joints_calls == 1
    next_opened = svc.open_realtime_session(
        force=True,
        expected_current_session_id=opened["session_id"],
    )
    try:
        svc.open_realtime_session(
            force=True,
            expected_current_session_id=opened["session_id"],
        )
        assert False, "expected stale session"
    except Exception as exc:
        assert getattr(exc, "code", None) == "stale_session"
    finally:
        svc.close_realtime_session(session_id=next_opened["session_id"])


if __name__ == "__main__":
    test_joint_dof_constants()
    test_flatten_roundtrip()
    test_dict_to_groups()
    test_arbiter_acquire_release()
    test_arbiter_force()
    test_safety_gripper()
    test_control_rights_loss_and_reacquire()
    test_realtime_lerp_groups()
    test_realtime_blend_s_source_hz_and_default()
    test_clamp_group_step_delta()
    test_realtime_client_frame_skips_step_delta_reject()
    test_realtime_prefer_latest_overwrites_target()
    test_realtime_ack_mode_normalize()
    test_realtime_blend_path_when_prefer_latest_false()
    test_control_context_matches_last_terminal_session()
    test_realtime_command_rejects_stale_session()
    test_move_to_rejects_stale_expected_session()
    print("ok")
