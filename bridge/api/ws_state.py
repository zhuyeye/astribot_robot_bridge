"""WebSocket endpoints: state stream, realtime control, audio PCM."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from bridge.schemas.common import BridgeError

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
    bound_session_id: str | None = None
    client = websocket.client.host if websocket.client else "-"
    logger.info("realtime ws connected client=%s", client)

    def _should_send_ack() -> bool:
        return bridge.motion.realtime_ack_mode() != "none"

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
                logger.info(
                    "realtime ws close client=%s session_id=%s bound_session_id=%s",
                    client,
                    payload.get("session_id", ""),
                    bound_session_id,
                )
                result = bridge.motion.close_realtime_session(
                    session_id=payload.get("session_id", ""),
                    terminal_reason="completed",
                )
                if bound_session_id == result.get("session_id"):
                    bound_session_id = None
                await websocket.send_json({"cmd": "closed", **result})
                break
            if cmd == "open":
                logger.info(
                    "realtime ws open client=%s request_id=%s expected_current_session_id=%s",
                    client,
                    payload.get("request_id", ""),
                    payload.get("expected_current_session_id"),
                )
                result = bridge.motion.open_realtime_session(
                    rate_hz=payload.get("rate_hz"),
                    source_hz=payload.get("source_hz"),
                    control_hz=payload.get("control_hz"),
                    control_way=payload.get("control_way"),
                    space=payload.get("space", "joints"),
                    force=bool(payload.get("force", False)),
                    reacquire_if_needed=bool(payload.get("reacquire_if_needed", False)),
                    request_id=payload.get("request_id", ""),
                    expected_current_session_id=payload.get("expected_current_session_id"),
                    supersedes_session_id=payload.get("supersedes_session_id"),
                    prefer_latest=bool(payload.get("prefer_latest", True)),
                    ack_mode=payload.get("ack_mode"),
                )
                bound_session_id = result.get("session_id")
                await websocket.send_json({"cmd": "opened", **result})
                continue

            try:
                result = await bridge.control_robot.run(
                    bridge.motion.apply_realtime_command,
                    session_id=payload.get("session_id", ""),
                    targets=payload.get("targets"),
                    q=payload.get("q"),
                    layout=payload.get("layout"),
                    names=payload.get("names"),
                    poses=payload.get("poses"),
                    check_step_delta=bool(payload.get("check_step_delta", True)),
                    reacquire_if_needed=bool(payload.get("reacquire_if_needed", False)),
                )
            except BridgeError as exc:
                # Late frames after close/play: soft reject so client drain_async does not see a dead WS.
                logger.warning(
                    "realtime ws command_rejected client=%s session_id=%s seq=%s reason=%s",
                    client,
                    payload.get("session_id", ""),
                    payload.get("seq"),
                    exc.code,
                )
                if not _should_send_ack():
                    continue
                await websocket.send_json(
                    {
                        "cmd": "ack",
                        "seq": payload.get("seq"),
                        "accepted": False,
                        "reason": exc.code,
                        "message": exc.message,
                    }
                )
                continue

            if not _should_send_ack():
                continue
            reply = {"cmd": "ack", "seq": payload.get("seq"), **result}
            if payload.get("return_state"):
                reply["state"] = await bridge.reader_robot.run(bridge.info.sample_state)
            await websocket.send_json(reply)
    except WebSocketDisconnect:
        logger.info("realtime ws disconnected client=%s bound_session_id=%s", client, bound_session_id)
    except Exception as exc:
        logger.exception("realtime ws error client=%s bound_session_id=%s", client, bound_session_id)
        try:
            await websocket.send_json({"cmd": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        if bound_session_id is not None and bridge.motion.realtime_session_id() == bound_session_id:
            logger.info("realtime ws cleanup_close client=%s session_id=%s", client, bound_session_id)
            try:
                bridge.motion.close_realtime_session(session_id=bound_session_id, terminal_reason="stopped")
            except Exception:
                pass
        elif bound_session_id is not None:
            logger.info(
                "realtime ws cleanup_skip client=%s session_id=%s active_session_id=%s",
                client,
                bound_session_id,
                bridge.motion.realtime_session_id(),
            )


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
                            backend=payload.get("backend"),
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
