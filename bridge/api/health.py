"""Health and status endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from bridge.schemas.common import ok

router = APIRouter(tags=["system"])


def _state(request: Request):
    return request.app.state.bridge


@router.get("/health")
async def health() -> dict:
    return ok({"status": "ok", "web_alive": True})


@router.get("/ready")
async def ready(request: Request) -> dict:
    bridge = _state(request)
    rights = bridge.control_rights.snapshot()
    audio_status = await bridge.audio_robot.run(bridge.audio.status)
    ready_flag = bool(bridge.reader_robot.ready and bridge.control_robot.ready and bridge.audio_robot.ready)
    payload = {
        "web_alive": True,
        "reader_ready": bridge.reader_robot.ready,
        "control_ready": bridge.control_robot.ready,
        "audio_ready": bridge.audio_robot.ready,
        "ready": ready_flag,
        "control": bridge.arbiter.to_dict(),
        "control_context": bridge.control_context.to_dict(),
        "control_rights": rights,
        "audio": {
            "worker_alive": audio_status["worker_alive"],
            "worker_error": audio_status["worker_error"],
            "queue_size": audio_status["queue_size"],
        },
    }
    if not ready_flag:
        from bridge.schemas.common import fail

        return fail("not_ready", "robot adapter not ready", payload)
    return ok(payload)


@router.get("/v1/status")
async def status(request: Request) -> dict:
    bridge = _state(request)
    audio_status = await bridge.audio_robot.run(bridge.audio.status)
    return ok(
        {
            "control": bridge.arbiter.to_dict(),
            "control_context": bridge.control_context.to_dict(),
            "control_rights": bridge.control_rights.snapshot(),
            "trajectory": bridge.trajectory.get_status(),
            "audio": audio_status,
            "reader_ready": bridge.reader_robot.ready,
            "control_ready": bridge.control_robot.ready,
            "audio_ready": bridge.audio_robot.ready,
        }
    )
