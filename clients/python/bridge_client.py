"""Optional thin Python client for asribot_robot_bridge."""

from __future__ import annotations

import json
from typing import Any

import httpx


class BridgeClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8080", *, api_key: str | None = None, timeout: float = 30.0):
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BridgeClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _unwrap(self, response: httpx.Response) -> Any:
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok", True):
            err = payload.get("error") or {}
            raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
        return payload.get("data")

    def health(self) -> Any:
        return self._unwrap(self._client.get("/health"))

    def status(self) -> Any:
        return self._unwrap(self._client.get("/v1/status"))

    def control_rights(self) -> Any:
        return self._unwrap(self._client.get("/v1/control-rights"))

    def reacquire_control_rights(self) -> Any:
        return self._unwrap(self._client.post("/v1/control-rights/reacquire"))

    def robot(self) -> Any:
        return self._unwrap(self._client.get("/v1/robot"))

    def joints(self, *, names: str | None = None, which: str = "current", fields: str = "pos,vel") -> Any:
        params = {"which": which, "fields": fields}
        if names:
            params["names"] = names
        return self._unwrap(self._client.get("/v1/joints", params=params))

    def list_actions(self) -> Any:
        return self._unwrap(self._client.get("/v1/actions"))

    def play(
        self,
        action_id: str,
        *,
        request_id: str = "",
        force: bool = False,
        reacquire_if_needed: bool | None = None,
        expected_current_session_id: str | None = None,
        supersedes_session_id: str | None = None,
    ) -> Any:
        return self._unwrap(
            self._client.post(
                "/v1/actions/play",
                json={
                    "action_id": action_id,
                    "request_id": request_id,
                    "force": force,
                    "reacquire_if_needed": reacquire_if_needed,
                    "expected_current_session_id": expected_current_session_id,
                    "supersedes_session_id": supersedes_session_id,
                },
            )
        )

    def stop_action(self, session_id: str, *, request_id: str = "") -> Any:
        return self._unwrap(
            self._client.post("/v1/actions/stop", json={"session_id": session_id, "request_id": request_id})
        )

    def move_to_home(
        self,
        *,
        wait: bool = False,
        force: bool = False,
        reacquire_if_needed: bool | None = None,
    ) -> Any:
        return self._unwrap(
            self._client.post(
                "/v1/motion/move_to/home",
                json={
                    "wait": wait,
                    "force": force,
                    "reacquire_if_needed": reacquire_if_needed,
                },
            )
        )

    def move_to_joints(
        self,
        targets: dict[str, list[float]],
        *,
        duration: float = 3.0,
        wait: bool = False,
        force: bool = False,
        reacquire_if_needed: bool | None = None,
        request_id: str = "",
        expected_current_session_id: str | None = None,
    ) -> Any:
        return self._unwrap(
            self._client.post(
                "/v1/motion/move_to/joints",
                json={
                    "targets": targets,
                    "duration": duration,
                    "wait": wait,
                    "force": force,
                    "reacquire_if_needed": reacquire_if_needed,
                    "request_id": request_id,
                    "expected_current_session_id": expected_current_session_id,
                },
            )
        )

    def open_realtime(self, **kwargs: Any) -> Any:
        return self._unwrap(self._client.post("/v1/motion/realtime/session", json=kwargs))

    def realtime_command(self, **kwargs: Any) -> Any:
        return self._unwrap(self._client.post("/v1/motion/realtime/command", json=kwargs))

    def close_realtime(self, session_id: str, *, request_id: str = "") -> Any:
        return self._unwrap(
            self._client.post("/v1/motion/realtime/close", json={"session_id": session_id, "request_id": request_id})
        )

    def estop(self) -> Any:
        return self._unwrap(self._client.post("/v1/motion/estop"))

    def play_audio(
        self,
        *,
        clip_id: str | None = None,
        path: str | None = None,
        mode: str | None = None,
        reacquire_if_needed: bool | None = None,
    ) -> Any:
        return self._unwrap(
            self._client.post(
                "/v1/audio/play",
                json={
                    "clip_id": clip_id,
                    "path": path,
                    "mode": mode,
                    "reacquire_if_needed": reacquire_if_needed,
                },
            )
        )

    def play_system_audio(
        self,
        *,
        clip_id: str | None = None,
        path: str | None = None,
        force: bool = False,
    ) -> Any:
        return self._unwrap(
            self._client.post(
                "/v1/audio/system-play",
                json={"clip_id": clip_id, "path": path, "force": force},
            )
        )

    def stop_audio(self) -> Any:
        return self._unwrap(self._client.post("/v1/audio/stop"))

    def get_system_volume(self) -> Any:
        return self._unwrap(self._client.get("/v1/audio/system-volume"))

    def set_system_volume(self, volume_percent: int, *, unmute: bool = True) -> Any:
        return self._unwrap(
            self._client.post(
                "/v1/audio/system-volume",
                json={"volume_percent": volume_percent, "unmute": unmute},
            )
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Astribot Robot Bridge CLI")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    sub.add_parser("status")
    sub.add_parser("control-rights")
    sub.add_parser("reacquire-control-rights")
    sub.add_parser("robot")
    p_joints = sub.add_parser("joints")
    p_joints.add_argument("--fields", default="pos,vel")
    sub.add_parser("list-actions")
    p_play = sub.add_parser("play")
    p_play.add_argument("action_id")
    p_play.add_argument("--force", action="store_true")
    p_stop = sub.add_parser("stop-action")
    p_stop.add_argument("session_id")
    p_home = sub.add_parser("home")
    p_home.add_argument("--wait", action="store_true")
    sub.add_parser("estop")
    p_audio = sub.add_parser("audio-play")
    p_audio.add_argument("--clip-id", default=None)
    p_audio.add_argument("--path", default=None)
    sub.add_parser("audio-volume")
    p_set_audio_volume = sub.add_parser("set-audio-volume")
    p_set_audio_volume.add_argument("volume_percent", type=int)
    p_set_audio_volume.add_argument("--keep-muted", action="store_true")

    args = parser.parse_args()
    with BridgeClient(args.base_url, api_key=args.api_key) as client:
        if args.cmd == "health":
            data = client.health()
        elif args.cmd == "status":
            data = client.status()
        elif args.cmd == "control-rights":
            data = client.control_rights()
        elif args.cmd == "reacquire-control-rights":
            data = client.reacquire_control_rights()
        elif args.cmd == "robot":
            data = client.robot()
        elif args.cmd == "joints":
            data = client.joints(fields=args.fields)
        elif args.cmd == "list-actions":
            data = client.list_actions()
        elif args.cmd == "play":
            data = client.play(args.action_id, force=args.force)
        elif args.cmd == "stop-action":
            data = client.stop_action(args.session_id)
        elif args.cmd == "home":
            data = client.move_to_home(wait=args.wait)
        elif args.cmd == "estop":
            data = client.estop()
        elif args.cmd == "audio-play":
            data = client.play_audio(clip_id=args.clip_id, path=args.path)
        elif args.cmd == "audio-volume":
            data = client.get_system_volume()
        elif args.cmd == "set-audio-volume":
            data = client.set_system_volume(
                args.volume_percent,
                unmute=not args.keep_muted,
            )
        else:
            raise SystemExit(f"unknown cmd: {args.cmd}")
        print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
