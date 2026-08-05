"""WebSocket endpoints: state stream, realtime control, audio PCM."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


def _bridge(websocket: WebSocket):
    return websocket.app.state.bridge


@router.websocket("/v1/ws/state")
async def ws_state(
    websocket: WebSocket,
    hz: float = Query(50.0),
    fields: str = Query("joints.pos,joints.vel"),
) -> None:
    await websocket.accept()
    bridge = _bridge(websocket)
    cfg = bridge.config.state
    rate = max(1.0, min(cfg.max_hz, float(hz or cfg.default_hz)))
    field_list = [f.strip() for f in fields.split(",") if f.strip()] or list(cfg.default_fields)
    period = 1.0 / rate
    seq = 0
    try:
        while True:
            t0 = time.perf_counter()
            sample = await bridge.reader_robot.run(bridge.info.sample_state, field_list)
            seq += 1
            sample["seq"] = seq
            await websocket.send_json(sample)
            elapsed = time.perf_counter() - t0
            await asyncio.sleep(max(0.0, period - elapsed))
    except WebSocketDisconnect:
        logger.info("state ws disconnected")
    except Exception:
        logger.exception("state ws error")
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/v1/ws/realtime")
async def ws_realtime(websocket: WebSocket) -> None:
    await websocket.accept()
    bridge = _bridge(websocket)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                continue
            payload = json.loads(text)
            cmd = payload.get("cmd", "command")
            if cmd == "ping":
                await websocket.send_json({"cmd": "pong", "t": time.time()})
                continue
            if cmd == "close":
                result = bridge.motion.close_realtime_session()
                await websocket.send_json({"cmd": "closed", **result})
                break
            if cmd == "open":
                result = bridge.motion.open_realtime_session(
                    rate_hz=payload.get("rate_hz"),
                    control_way=payload.get("control_way"),
                    space=payload.get("space", "joints"),
                    force=bool(payload.get("force", False)),
                    reacquire_if_needed=bool(payload.get("reacquire_if_needed", False)),
                )
                await websocket.send_json({"cmd": "opened", **result})
                continue

            result = await bridge.control_robot.run(
                bridge.motion.apply_realtime_command,
                targets=payload.get("targets"),
                q=payload.get("q"),
                layout=payload.get("layout"),
                names=payload.get("names"),
                poses=payload.get("poses"),
                check_step_delta=bool(payload.get("check_step_delta", True)),
                reacquire_if_needed=bool(payload.get("reacquire_if_needed", False)),
            )
            reply = {"cmd": "ack", "seq": payload.get("seq"), **result}
            if payload.get("return_state"):
                reply["state"] = await bridge.reader_robot.run(bridge.info.sample_state)
            await websocket.send_json(reply)
    except WebSocketDisconnect:
        logger.info("realtime ws disconnected")
    except Exception as exc:
        logger.exception("realtime ws error")
        try:
            await websocket.send_json({"cmd": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        try:
            bridge.motion.close_realtime_session()
        except Exception:
            pass


@router.websocket("/v1/ws/audio")
async def ws_audio(websocket: WebSocket) -> None:
    await websocket.accept()
    bridge = _bridge(websocket)
    client = websocket.client.host if websocket.client else "-"
    logger.info("audio ws connected client=%s", client)
    try:
        # Expect binary PCM frames after /v1/audio/stream/start
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is None:
                text = message.get("text")
                if text:
                    payload = json.loads(text)
                    if payload.get("cmd") == "start":
                        logger.info("audio ws command=start client=%s", client)
                        result = await bridge.audio_robot.run(
                            bridge.audio.start_stream_with_rights,
                            force=bool(payload.get("force", False)),
                            reacquire_if_needed=payload.get("reacquire_if_needed"),
                        )
                        await websocket.send_json({"cmd": "started", **result})
                    elif payload.get("cmd") == "stop":
                        logger.info("audio ws command=stop client=%s", client)
                        result = await bridge.audio_robot.run(bridge.audio.stop)
                        await websocket.send_json({"cmd": "stopped", **result})
                        break
                continue
            result = await bridge.audio_robot.run(bridge.audio.push_pcm_frame, data)
            await websocket.send_json({"cmd": "ack", **result})
    except WebSocketDisconnect:
        logger.info("audio ws disconnected client=%s", client)
    except Exception as exc:
        logger.exception("audio ws error client=%s", client)
        try:
            await websocket.send_json({"cmd": "error", "message": str(exc)})
        except Exception:
            pass
