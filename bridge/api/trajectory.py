"""Trajectory / action playback API."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bridge.schemas.common import ok
from bridge.schemas.requests import PlayActionRequest

router = APIRouter(prefix="/v1/actions", tags=["actions"])


def _state(request: Request):
    return request.app.state.bridge


@router.get("/")
async def list_actions(request: Request) -> dict:
    return ok(_state(request).trajectory.list_actions())


@router.get("/status")
async def action_status(request: Request) -> dict:
    return ok(_state(request).trajectory.get_status())


@router.get("/{action_id}")
async def get_action(action_id: str, request: Request) -> dict:
    return ok(_state(request).trajectory.get_action(action_id))


@router.post("/play")
async def play(request: Request, body: PlayActionRequest) -> dict:
    result = _state(request).trajectory.play(
        body.action_id,
        request_id=body.request_id,
        force=body.force,
        reacquire_if_needed=body.reacquire_if_needed,
    )
    result.update(
        {
            "resource": "actions.playback",
            "execution": "async",
            "operation_status": "accepted" if result.get("accepted") else "noop",
        }
    )
    status_code = 202 if result.get("accepted") else 200
    return JSONResponse(status_code=status_code, content=ok(result))


@router.post("/stop")
async def stop(request: Request) -> dict:
    result = _state(request).trajectory.stop()
    result.update({"resource": "actions.playback", "execution": "sync", "operation_status": "stopped"})
    return ok(result)
