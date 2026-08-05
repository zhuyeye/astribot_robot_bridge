"""Control-rights inspection and reacquire API."""

from __future__ import annotations

from fastapi import APIRouter, Request

from bridge.schemas.common import ok

router = APIRouter(prefix="/v1/control-rights", tags=["control-rights"])


def _state(request: Request):
    return request.app.state.bridge


@router.get("")
async def get_control_rights(request: Request) -> dict:
    return ok(_state(request).control_rights.snapshot())


@router.post("/reacquire")
async def reacquire_control_rights(request: Request) -> dict:
    result = _state(request).control_rights.reacquire()
    result.update({"resource": "control_rights", "execution": "sync", "operation_status": "updated"})
    return ok(result)
