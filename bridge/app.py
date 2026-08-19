"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bridge.api import audio, control_rights, health, info, motion, trajectory
from bridge.api import ws_state as ws_routes
from bridge.config import BRIDGE_ROOT, BridgeConfig, get_config
from bridge.domain.arbiter import ControlArbiter
from bridge.domain.audio_service import AudioService
from bridge.domain.control_context import ControlContext
from bridge.domain.control_rights import ControlRightsManager
from bridge.domain.info_service import InfoService
from bridge.domain.motion_service import MotionService
from bridge.domain.trajectory_service import TrajectoryService
from bridge.robot.adapter import RobotAdapter
from bridge.schemas.common import BridgeError, bridge_error_handler, fail, http_error_handler
from bridge.state import AppState

logger = logging.getLogger(__name__)


def _configure_logging(config: BridgeConfig) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if config.logging.file:
        log_path = config.resolve_path(config.logging.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max(1, int(config.logging.rotate_mb)) * 1024 * 1024,
            backupCount=max(1, int(config.logging.backup_count)),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()
    _configure_logging(config)
    logger.info("starting asribot_robot_bridge (root=%s)", BRIDGE_ROOT)

    arbiter = ControlArbiter()
    control_context = ControlContext()
    control_robot = RobotAdapter(
        sdk_root=Path(config.sdk.root),
        freq=config.sdk.control_hz,
        node_name=config.sdk.node_name,
        high_control_rights=True,
    )
    reader_robot = RobotAdapter(
        sdk_root=Path(config.sdk.root),
        freq=config.sdk.control_hz,
        node_name=f"{config.sdk.node_name}_reader",
        high_control_rights=False,
    )
    audio_robot = RobotAdapter(
        sdk_root=Path(config.sdk.root),
        freq=config.sdk.control_hz,
        node_name=f"{config.sdk.node_name}_audio",
        high_control_rights=False,
    )
    rights = ControlRightsManager(
        control_robot,
        arbiter,
        config=config.control_rights,
    )
    info_svc = InfoService(reader_robot, arbiter, rights)
    trajectory_svc = TrajectoryService(
        control_robot,
        arbiter,
        rights,
        control_context,
        manifest_path=config.manifest_path,
    )
    motion_svc = MotionService(
        control_robot,
        arbiter,
        rights,
        control_context,
        config,
        on_estop_hooks=[trajectory_svc.interrupt_for_estop],
    )
    audio_svc = AudioService(audio_robot, config)
    audio_svc.apply_system_audio_defaults()
    rights.register_loss_callback(trajectory_svc.on_control_rights_lost)
    rights.register_loss_callback(motion_svc.on_control_rights_lost)
    rights.start()

    app.state.bridge = AppState(
        config=config,
        control_robot=control_robot,
        reader_robot=reader_robot,
        audio_robot=audio_robot,
        arbiter=arbiter,
        control_context=control_context,
        control_rights=rights,
        info=info_svc,
        motion=motion_svc,
        trajectory=trajectory_svc,
        audio=audio_svc,
    )
    logger.info("bridge ready; manifest=%s", config.manifest_path)
    try:
        yield
    finally:
        logger.info("shutting down bridge")
        rights.close()
        trajectory_svc.close()
        audio_svc.close()
        audio_robot.close()
        reader_robot.close()
        control_robot.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Astribot Robot Bridge",
        version="0.1.0",
        description="HTTP/WebSocket bridge for Astribot info, motion, and audio",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(BridgeError, bridge_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)

    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        config = get_config()
        expected = config.http.api_key
        if expected:
            provided = request.headers.get("X-API-Key")
            if provided != expected:
                return JSONResponse(status_code=401, content=fail("unauthorized", "invalid API key"))
        return await call_next(request)

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next):
        started_at = time.perf_counter()
        client = request.client.host if request.client else "-"
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = request_id
        logger.info(
            "request start request_id=%s method=%s path=%s query=%s client=%s",
            request_id,
            request.method,
            request.url.path,
            request.url.query,
            client,
        )
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            logger.exception(
                "request error request_id=%s method=%s path=%s client=%s duration_ms=%.1f",
                request_id,
                request.method,
                request.url.path,
                client,
                elapsed_ms,
            )
            raise
        response.headers["X-Request-Id"] = request_id
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        logger.info(
            "request end request_id=%s method=%s path=%s status=%s client=%s duration_ms=%.1f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            client,
            elapsed_ms,
        )
        return response

    app.include_router(health.router)
    app.include_router(control_rights.router)
    app.include_router(info.router)
    app.include_router(trajectory.router)
    app.include_router(motion.router)
    app.include_router(audio.router)
    app.include_router(ws_routes.router)
    return app


app = create_app()
