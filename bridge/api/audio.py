"""Audio REST API."""

from __future__ import annotations

from fastapi import APIRouter, Request

from bridge.schemas.common import ok
from bridge.schemas.requests import (
    AudioPlayRequest,
    AudioStreamStartRequest,
    AudioSystemPlayRequest,
    AudioSystemVolumeRequest,
)

router = APIRouter(prefix="/v1/audio", tags=["audio"])


def _state(request: Request):
    return request.app.state.bridge


@router.get("/clips")
async def list_clips(request: Request) -> dict:
    return ok(_state(request).audio.list_clips())


@router.get("/status")
async def audio_status(request: Request) -> dict:
    bridge = _state(request)
    return ok(await bridge.audio_robot.run(bridge.audio.status))


@router.get("/system-volume")
async def get_system_volume(request: Request) -> dict:
    bridge = _state(request)
    return ok(await bridge.audio_robot.run(bridge.audio.get_system_volume))


@router.post("/play")
async def play(request: Request, body: AudioPlayRequest) -> dict:
    result = _state(request).audio.play_clip(
            clip_id=body.clip_id,
            path=body.path,
            mode=body.mode,
            force=body.force,
            reacquire_if_needed=body.reacquire_if_needed,
        )
    result.update({"resource": "audio.playback", "execution": "async", "operation_status": "accepted"})
    return ok(result)


@router.post("/system-play")
async def system_play(request: Request, body: AudioSystemPlayRequest) -> dict:
    """Play wav via paplay → Pulse Yundea USB sink, no SDK speaker topic."""
    result = _state(request).audio.play_system_clip(
        clip_id=body.clip_id,
        path=body.path,
        force=body.force,
    )
    result.update(
        {
            "resource": "audio.system_playback",
            "execution": "async",
            "operation_status": "accepted",
        }
    )
    return ok(result)


@router.post("/stop")
async def stop(request: Request) -> dict:
    bridge = _state(request)
    result = await bridge.audio_robot.run(bridge.audio.stop)
    result.update({"resource": "audio.playback", "execution": "sync", "operation_status": "stopped"})
    return ok(result)


@router.post("/stream/start")
async def stream_start(request: Request, body: AudioStreamStartRequest | None = None) -> dict:
    body = body or AudioStreamStartRequest()
    bridge = _state(request)
    result = await bridge.audio_robot.run(
            bridge.audio.start_stream_with_rights,
            force=body.force,
            reacquire_if_needed=body.reacquire_if_needed,
            backend=body.backend,
        )
    result.update({"resource": "audio.stream", "execution": "sync", "operation_status": "opened"})
    return ok(result)


@router.post("/stream/stop")
async def stream_stop(request: Request) -> dict:
    bridge = _state(request)
    result = await bridge.audio_robot.run(bridge.audio.stop)
    result.update({"resource": "audio.stream", "execution": "sync", "operation_status": "closed"})
    return ok(result)


@router.post("/system-volume")
async def set_system_volume(request: Request, body: AudioSystemVolumeRequest) -> dict:
    bridge = _state(request)
    result = await bridge.audio_robot.run(
            bridge.audio.set_system_volume,
            body.volume_percent,
            unmute=body.unmute,
        )
    result.update({"resource": "audio.system_volume", "execution": "sync", "operation_status": "updated"})
    return ok(result)
