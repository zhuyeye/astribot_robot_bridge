"""Information query REST API."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from bridge.schemas.common import ok
from bridge.schemas.requests import ClosestPointRequest, KinematicsRequest

router = APIRouter(prefix="/v1", tags=["info"])


def _state(request: Request):
    return request.app.state.bridge


def _split_csv(value: str | None) -> list[str] | None:
    if value is None or value.strip() == "":
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


@router.get("/robot")
async def robot_info(request: Request) -> dict:
    bridge = _state(request)
    return ok(await bridge.reader_robot.run(bridge.info.robot_info))


@router.get("/joints")
async def joints(
    request: Request,
    names: str | None = Query(None, description="comma-separated part names"),
    which: str = Query("current", pattern="^(current|desired)$"),
    fields: str | None = Query("pos,vel", description="comma-separated: pos,vel,acc,torque"),
) -> dict:
    bridge = _state(request)
    data = await bridge.reader_robot.run(
        bridge.info.joints,
        names=_split_csv(names),
        which=which,
        fields=_split_csv(fields),
    )
    return ok(data)


@router.get("/joints/limits")
async def joint_limits(request: Request, names: str | None = None) -> dict:
    bridge = _state(request)
    data = await bridge.reader_robot.run(bridge.info.joint_limits, names=_split_csv(names))
    return ok(data)


@router.get("/cartesian")
async def cartesian(
    request: Request,
    names: str | None = None,
    frame: str = "chassis",
    which: str = Query("current", pattern="^(current|desired|wbc)$"),
) -> dict:
    bridge = _state(request)
    data = await bridge.reader_robot.run(
        bridge.info.cartesian,
        names=_split_csv(names),
        frame=frame,
        which=which,
    )
    return ok(data)


@router.post("/kinematics/fk")
async def fk(request: Request, body: KinematicsRequest) -> dict:
    if body.joints is None:
        from bridge.schemas.common import BridgeError

        raise BridgeError("invalid_request", "joints required for fk")
    bridge = _state(request)
    data = await bridge.reader_robot.run(bridge.info.fk, body.names, body.joints)
    return ok(data)


@router.post("/kinematics/ik")
async def ik(request: Request, body: KinematicsRequest) -> dict:
    if body.poses is None:
        from bridge.schemas.common import BridgeError

        raise BridgeError("invalid_request", "poses required for ik")
    bridge = _state(request)
    data = await bridge.reader_robot.run(bridge.info.ik, body.names, body.poses)
    return ok(data)


@router.post("/safety/closest-point")
async def closest_point(request: Request, body: ClosestPointRequest) -> dict:
    bridge = _state(request)
    data = await bridge.reader_robot.run(
        bridge.info.closest_point,
        body.torso,
        body.arm_left,
        body.arm_right,
    )
    return ok(data)


@router.get("/cameras")
async def cameras(request: Request) -> dict:
    bridge = _state(request)
    data = await bridge.reader_robot.run(bridge.info.cameras)
    return ok(data)
