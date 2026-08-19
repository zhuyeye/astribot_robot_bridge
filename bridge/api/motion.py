"""Motion control REST API."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bridge.schemas.common import ok
from bridge.schemas.requests import (
    CloseRealtimeRequest,
    GripperRequest,
    MoveToCartesianRequest,
    MoveToHomeRequest,
    MoveToJointsRequest,
    MoveToWaypointsRequest,
    RealtimeCommandRequest,
    RealtimeSessionRequest,
    SettingsRequest,
)

router = APIRouter(prefix="/v1/motion", tags=["motion"])


def _state(request: Request):
    return request.app.state.bridge


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    return ok(_state(request).motion.get_task(task_id))


@router.post("/move_to/joints")
async def move_to_joints(request: Request, body: MoveToJointsRequest):
    result = _state(request).motion.move_to_joints(
        body.targets,
        duration=body.duration,
        use_wbc=body.use_wbc,
        force=body.force,
        wait=body.wait,
        reacquire_if_needed=body.reacquire_if_needed,
        request_id=body.request_id,
        expected_current_session_id=body.expected_current_session_id,
    )
    result.update(
        {
            "resource": "motion.move_to.joints",
            "execution": "sync" if body.wait else "async",
            "operation_status": "succeeded" if body.wait else "accepted",
        }
    )
    if body.wait:
        return ok(result)
    return JSONResponse(status_code=202, content=ok(result))


@router.post("/move_to/cartesian")
async def move_to_cartesian(request: Request, body: MoveToCartesianRequest):
    result = _state(request).motion.move_to_cartesian(
        body.names,
        body.poses,
        duration=body.duration,
        use_wbc=body.use_wbc,
        force=body.force,
        wait=body.wait,
        reacquire_if_needed=body.reacquire_if_needed,
        request_id=body.request_id,
        expected_current_session_id=body.expected_current_session_id,
    )
    result.update(
        {
            "resource": "motion.move_to.cartesian",
            "execution": "sync" if body.wait else "async",
            "operation_status": "succeeded" if body.wait else "accepted",
        }
    )
    if body.wait:
        return ok(result)
    return JSONResponse(status_code=202, content=ok(result))


@router.post("/move_to/home")
async def move_to_home(request: Request, body: MoveToHomeRequest | None = None):
    body = body or MoveToHomeRequest()
    result = _state(request).motion.move_to_home(
        duration=body.duration,
        use_wbc=body.use_wbc,
        force=body.force,
        wait=body.wait,
        reacquire_if_needed=body.reacquire_if_needed,
        request_id=body.request_id,
        expected_current_session_id=body.expected_current_session_id,
    )
    result.update(
        {
            "resource": "motion.move_to.home",
            "execution": "sync" if body.wait else "async",
            "operation_status": "succeeded" if body.wait else "accepted",
        }
    )
    if body.wait:
        return ok(result)
    return JSONResponse(status_code=202, content=ok(result))


@router.post("/move_to/waypoints")
async def move_to_waypoints(request: Request, body: MoveToWaypointsRequest):
    result = _state(request).motion.move_to_waypoints(
        space=body.space,
        names=body.names,
        waypoints=body.waypoints,
        time_list=body.time_list,
        use_wbc=body.use_wbc,
        force=body.force,
        wait=body.wait,
        reacquire_if_needed=body.reacquire_if_needed,
        request_id=body.request_id,
        expected_current_session_id=body.expected_current_session_id,
    )
    result.update(
        {
            "resource": "motion.move_to.waypoints",
            "execution": "sync" if body.wait else "async",
            "operation_status": "succeeded" if body.wait else "accepted",
        }
    )
    if body.wait:
        return ok(result)
    return JSONResponse(status_code=202, content=ok(result))


@router.post("/realtime/session")
async def open_realtime(request: Request, body: RealtimeSessionRequest) -> dict:
    result = _state(request).motion.open_realtime_session(
            rate_hz=body.rate_hz,
            source_hz=body.source_hz,
            control_hz=body.control_hz,
            control_way=body.control_way,
            space=body.space,
            force=body.force,
            reacquire_if_needed=bool(body.reacquire_if_needed),
            request_id=body.request_id,
            expected_current_session_id=body.expected_current_session_id,
            supersedes_session_id=body.supersedes_session_id,
            prefer_latest=body.prefer_latest,
            ack_mode=body.ack_mode,
        )
    result.update({"resource": "motion.realtime", "execution": "sync", "operation_status": "opened"})
    return ok(result)


@router.post("/realtime/command")
async def realtime_command(request: Request, body: RealtimeCommandRequest) -> dict:
    bridge = _state(request)
    result = await bridge.control_robot.run(
        bridge.motion.apply_realtime_command,
        session_id=body.session_id,
        targets=body.targets,
        q=body.q,
        layout=body.layout,
        names=body.names,
        poses=body.poses,
        check_step_delta=body.check_step_delta,
        reacquire_if_needed=body.reacquire_if_needed,
    )
    result.update({"resource": "motion.realtime", "execution": "sync", "operation_status": "applied"})
    return ok(result)


@router.post("/realtime/close")
async def close_realtime(request: Request, body: CloseRealtimeRequest) -> dict:
    result = _state(request).motion.close_realtime_session(session_id=body.session_id, terminal_reason="completed")
    result.update({"resource": "motion.realtime", "execution": "sync", "operation_status": "closed"})
    return ok(result)


@router.post("/gripper/open")
async def gripper_open(request: Request, body: GripperRequest | None = None) -> dict:
    body = body or GripperRequest()
    bridge = _state(request)
    result = await bridge.control_robot.run(
        bridge.motion.gripper, "open", names=body.names, duration=body.duration
    )
    result.update({"resource": "motion.gripper", "execution": "sync", "operation_status": "succeeded"})
    return ok(result)


@router.post("/gripper/close")
async def gripper_close(request: Request, body: GripperRequest | None = None) -> dict:
    body = body or GripperRequest()
    bridge = _state(request)
    result = await bridge.control_robot.run(
        bridge.motion.gripper, "close", names=body.names, duration=body.duration
    )
    result.update({"resource": "motion.gripper", "execution": "sync", "operation_status": "succeeded"})
    return ok(result)


@router.post("/settings")
async def settings(request: Request, body: SettingsRequest) -> dict:
    bridge = _state(request)
    result = await bridge.control_robot.run(
        bridge.motion.set_settings,
        filter_scale=body.filter_scale,
        gripper_filter_scale=body.gripper_filter_scale,
        head_follow=body.head_follow,
        head_follow_arm=body.head_follow_arm,
        collision_avoidance=body.collision_avoidance,
    )
    result.update({"resource": "motion.settings", "execution": "sync", "operation_status": "updated"})
    return ok(result)


@router.post("/estop")
async def estop(request: Request) -> dict:
    bridge = _state(request)
    result = await bridge.control_robot.run(bridge.motion.estop)
    result.update({"resource": "motion.estop", "execution": "sync", "operation_status": "succeeded"})
    return ok(result)


@router.post("/restart")
async def restart(request: Request) -> dict:
    bridge = _state(request)
    result = await bridge.control_robot.run(bridge.motion.restart)
    result.update({"resource": "motion.control", "execution": "sync", "operation_status": "restarted"})
    return ok(result)
