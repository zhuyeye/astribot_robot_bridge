"""Audio clip playback and PCM streaming."""

from __future__ import annotations

import json
import logging
import queue
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path
import re
from typing import Any

import numpy as np

from bridge.config import BridgeConfig
from bridge.robot.adapter import RobotAdapter
from bridge.schemas.common import BridgeError

logger = logging.getLogger(__name__)

CHUNK_SECONDS_DEFAULT = 0.1
_PERCENT_RE = re.compile(r"(\d{1,3})%")


class AudioService:
    def __init__(
        self,
        robot: RobotAdapter,
        config: BridgeConfig,
    ) -> None:
        self.robot = robot
        self.config = config
        self.dataset_dir = Path(config.audio.dataset_dir)
        self._lock = threading.Lock()
        self._playing = False
        self._stream_active = False
        self._stream_seq = 0
        self._pub = None
        self._stop = threading.Event()
        self._frame_queue: queue.Queue[tuple[int, int, int, list[float]]] = queue.Queue(maxsize=64)
        self._stream_worker: threading.Thread | None = None
        self._worker_error: str | None = None
        self._last_frame_ts: float | None = None
        self._last_publish_ts: float | None = None
        self._dropped_frames = 0
        self._received_frames = 0
        self._received_bytes = 0
        self._published_frames = 0
        self._stats_log_interval_s = 5.0
        self._last_stats_log_ts: float | None = None

    def list_clips(self) -> list[dict[str, Any]]:
        if not self.dataset_dir.is_dir():
            return []
        clips = []
        for path in sorted(self.dataset_dir.glob("*.wav")):
            clips.append({"clip_id": path.stem, "path": str(path), "name": path.name})
        for path in sorted(self.dataset_dir.glob("*.WAV")):
            clips.append({"clip_id": path.stem, "path": str(path), "name": path.name})
        return clips

    def status(self) -> dict[str, Any]:
        with self._lock:
            system_volume = self._get_system_volume_status()
            return {
                "playing": self._playing,
                "stream_active": self._stream_active,
                "audio_activated": self.robot.audio_activated,
                "dataset_dir": str(self.dataset_dir),
                "system_volume": system_volume,
                "queue_size": self._frame_queue.qsize(),
                "queue_capacity": self._frame_queue.maxsize,
                "worker_alive": bool(self._stream_worker and self._stream_worker.is_alive()),
                "worker_error": self._worker_error,
                "last_frame_ts": self._last_frame_ts,
                "last_publish_ts": self._last_publish_ts,
                "dropped_frames": self._dropped_frames,
                "received_frames": self._received_frames,
                "received_bytes": self._received_bytes,
                "published_frames": self._published_frames,
            }

    def get_system_volume(self) -> dict[str, Any]:
        return self._get_system_volume_status()

    def set_system_volume(self, volume_percent: int, *, unmute: bool = True) -> dict[str, Any]:
        volume = int(volume_percent)
        if volume < 0 or volume > 150:
            raise BridgeError(
                "invalid_request",
                "volume_percent must be between 0 and 150",
            )

        errors: list[str] = []

        pulse_result = self._set_pulse_volume(volume, unmute=unmute)
        if pulse_result is not None:
            return pulse_result
        errors.append("pactl unavailable or no active user audio session")

        alsa_result = self._set_alsa_volume(volume, unmute=unmute)
        if alsa_result is not None:
            return alsa_result
        errors.append("amixer could not find a usable mixer control")

        raise BridgeError(
            "system_volume_unavailable",
            "failed to control system volume via pactl or amixer",
            status_code=503,
            details={"attempts": errors},
        )

    def _resolve_wav(self, clip_id: str | None, path: str | None) -> Path:
        if path:
            candidate = Path(path)
            if candidate.is_file():
                return candidate
            raise BridgeError("not_found", f"wav not found: {path}", status_code=404)
        if not clip_id:
            raise BridgeError("invalid_request", "clip_id or path required")
        for ext in (".wav", ".WAV", ""):
            candidate = self.dataset_dir / f"{clip_id}{ext}" if ext else self.dataset_dir / clip_id
            if candidate.is_file():
                return candidate
        raise BridgeError("not_found", f"clip not found: {clip_id}", status_code=404)

    def _ensure_activated(self) -> None:
        if not self.robot.audio_activated:
            logger.info("audio activate begin")
            if self.robot.activate_audio() is False:
                raise BridgeError("audio_activate_failed", "activate_audio failed", status_code=500)
            logger.info("audio activate done")
            time.sleep(0.5)

    def play_clip(
        self,
        *,
        clip_id: str | None = None,
        path: str | None = None,
        mode: str | None = None,
        force: bool = False,
        reacquire_if_needed: bool | None = None,
    ) -> dict[str, Any]:
        mode = mode or self.config.audio.default_mode
        wav_path = self._resolve_wav(clip_id, path)
        logger.info(
            "audio play requested mode=%s clip=%s path=%s force=%s",
            mode,
            clip_id,
            path,
            force,
        )

        with self._lock:
            if (self._playing or self._stream_active) and not force:
                raise BridgeError("audio_busy", "audio session busy", status_code=409, details=self.status())
            self._stop.set()
            self._playing = True
            self._stream_active = False
            self._stop = threading.Event()

        def _runner() -> None:
            try:
                self._ensure_activated()
                if mode == "stream":
                    duration = self._play_via_stream(wav_path)
                else:
                    duration = self._play_via_service(wav_path, clip_id or wav_path.stem)
                logger.info("audio play done: %s (%.2fs)", wav_path.name, duration)
            except Exception:
                logger.exception("audio play failed")
            finally:
                with self._lock:
                    self._playing = False

        threading.Thread(target=_runner, name="audio-play", daemon=True).start()
        return {"accepted": True, "clip": wav_path.name, "mode": mode, "path": str(wav_path)}

    def stop(self) -> dict[str, Any]:
        logger.info("audio stop requested")
        with self._lock:
            self._stop.set()
            self._playing = False
            self._stream_active = False
            self._drain_frame_queue()
        try:
            logger.info("audio deactivate begin")
            self.robot.deactivate_audio()
            logger.info("audio deactivate done")
        except Exception:
            logger.exception("deactivate_audio failed")
        return self.status()

    def close(self) -> None:
        self.stop()
        if self._stream_worker is not None:
            self._stream_worker.join(timeout=1.0)

    def start_stream(self, *, force: bool = False) -> dict[str, Any]:
        return self.start_stream_with_rights(force=force, reacquire_if_needed=False)

    def start_stream_with_rights(
        self,
        *,
        force: bool = False,
        reacquire_if_needed: bool | None = None,
    ) -> dict[str, Any]:
        logger.info("audio stream start requested force=%s", force)
        with self._lock:
            if (self._playing or self._stream_active) and not force:
                raise BridgeError("audio_busy", "audio session busy", status_code=409, details=self.status())
            self._stop.set()
            self._playing = False
            self._stream_active = True
            self._stream_seq = 0
            self._stop = threading.Event()
            self._worker_error = None
            self._last_frame_ts = None
            self._last_publish_ts = None
            self._received_frames = 0
            self._received_bytes = 0
            self._published_frames = 0
            self._last_stats_log_ts = time.time()
            self._drain_frame_queue()
        self._ensure_activated()
        self._ensure_publisher()
        self._ensure_stream_worker()
        logger.info("audio stream start ready")
        return {"accepted": True, "stream_active": True}

    def push_pcm_frame(self, frame: bytes) -> dict[str, Any]:
        if len(frame) < 12:
            raise BridgeError("invalid_frame", "audio frame too short")
        seq, sample_rate, channels, fmt = struct.unpack_from("!IIHH", frame, 0)
        payload = frame[12:]
        if fmt != 0:
            raise BridgeError("invalid_frame", f"unsupported format code: {fmt} (expect 0=float32)")
        if len(payload) % 4 != 0:
            raise BridgeError("invalid_frame", "payload must be float32 aligned")
        samples = list(struct.unpack(f"<{len(payload) // 4}f", payload))

        with self._lock:
            if not self._stream_active:
                raise BridgeError("stream_inactive", "audio stream not started", status_code=409)
            self._stream_seq = seq
            self._last_frame_ts = time.time()
            self._received_frames += 1
            self._received_bytes += len(payload)

        if seq == 0:
            logger.info("audio stream first frame sample_rate=%s channels=%s", sample_rate, channels)
        self._maybe_log_stream_stats()
        try:
            self._frame_queue.put_nowait((seq, sample_rate, channels, samples))
        except queue.Full as exc:
            with self._lock:
                self._dropped_frames += 1
            raise BridgeError(
                "audio_backpressure",
                "audio frame queue is full",
                status_code=429,
            ) from exc
        return {"accepted": True, "seq": seq}

    def on_control_rights_lost(self) -> None:
        with self._lock:
            self._stop.set()
            self._playing = False
            self._stream_active = False
            self._drain_frame_queue()

    def _ensure_stream_worker(self) -> None:
        if self._stream_worker is not None and self._stream_worker.is_alive():
            return
        self._stream_worker = threading.Thread(
            target=self._stream_worker_loop,
            name="audio-stream-worker",
            daemon=True,
        )
        self._stream_worker.start()

    def _stream_worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                _, sample_rate, channels, samples = self._frame_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._publish_chunk(samples, channels=channels, samplerate=sample_rate)
                with self._lock:
                    self._last_publish_ts = time.time()
                    self._published_frames += 1
            except Exception as exc:
                with self._lock:
                    self._worker_error = str(exc)
                    self._stream_active = False
                    self._stop.set()
                logger.exception("audio stream worker failed")
                return

    def _drain_frame_queue(self) -> None:
        while True:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                return

    def _maybe_log_stream_stats(self) -> None:
        with self._lock:
            now = time.time()
            if self._last_stats_log_ts is not None and (now - self._last_stats_log_ts) < self._stats_log_interval_s:
                return
            self._last_stats_log_ts = now
            logger.info(
                "audio stream stats received_frames=%s published_frames=%s received_bytes=%s queue_size=%s dropped_frames=%s last_seq=%s",
                self._received_frames,
                self._published_frames,
                self._received_bytes,
                self._frame_queue.qsize(),
                self._dropped_frames,
                self._stream_seq,
            )

    def _ensure_publisher(self) -> None:
        if self._pub is not None:
            return
        from std_msgs.msg import Float64MultiArray

        node = self.robot.raw_interface().astribot_interface.node
        self._pub = node.create_publisher(Float64MultiArray, "/astribot_audio/speaker/stream", 10)

    def _publish_chunk(self, samples: list[float], *, channels: int, samplerate: int) -> None:
        from std_msgs.msg import Float64MultiArray, MultiArrayDimension

        msg = Float64MultiArray()
        msg.data = samples
        dim = MultiArrayDimension()
        dim.label = json.dumps({"channels": channels, "samplerate": samplerate})
        dim.size = len(msg.data)
        dim.stride = 1
        msg.layout.dim = [dim]
        self._pub.publish(msg)

    def _play_via_service(self, wav_path: Path, request_name: str) -> float:
        from astribot_msgs.srv import RawRequest

        node = self.robot.raw_interface().astribot_interface.node
        client = node.create_client(RawRequest, "/astribot_audio/wav_play_service")
        if not client.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("service /astribot_audio/wav_play_service not available")

        if wav_path.parent.resolve() == self.dataset_dir.resolve():
            req_name = wav_path.stem
        else:
            req_name = str(wav_path)

        req = RawRequest.Request()
        req.request = req_name
        future = client.call_async(req)
        start = time.time()
        while not future.done() and (time.time() - start) < 30.0:
            if self._stop.is_set():
                return 0.0
            time.sleep(0.01)
        if not future.done():
            raise TimeoutError("wav_play_service timeout")
        resp = future.result()
        if not str(resp.response).startswith("success"):
            raise RuntimeError(f"wav_play_service failed: {resp.response}")

        audio, samplerate = self._load_wav(wav_path)
        duration = len(audio) / samplerate
        time.sleep(duration + 0.3)
        return duration

    def _play_via_stream(self, wav_path: Path) -> float:
        audio, samplerate = self._load_wav(wav_path)
        channels = audio.shape[1]
        chunk_seconds = self.config.audio.chunk_seconds
        chunk_samples = max(1, int(chunk_seconds * samplerate))
        duration = len(audio) / samplerate
        self._ensure_publisher()
        position = 0
        while position < len(audio):
            if self._stop.is_set():
                break
            end = min(position + chunk_samples, len(audio))
            chunk = audio[position:end]
            position = end
            self._publish_chunk(chunk.flatten().tolist(), channels=channels, samplerate=samplerate)
            time.sleep(chunk_seconds)
        return duration

    @staticmethod
    def _load_wav(path: Path) -> tuple[np.ndarray, int]:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            samplerate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)

        if sample_width == 2:
            samples = np.array(struct.unpack(f"<{n_frames * channels}h", raw), dtype=np.float32)
            samples /= 32768.0
        elif sample_width == 4:
            samples = np.array(struct.unpack(f"<{n_frames * channels}i", raw), dtype=np.float32)
            samples /= 2147483648.0
        else:
            raise ValueError(f"unsupported sample width: {sample_width}")

        if channels == 1:
            return samples.reshape(-1, 1), samplerate
        return samples.reshape(-1, channels), samplerate

    def _run_command(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
        )

    def _get_system_volume_status(self) -> dict[str, Any]:
        pulse = self._get_pulse_volume()
        if pulse is not None:
            return pulse

        alsa = self._get_alsa_volume()
        if alsa is not None:
            return alsa

        return {
            "available": False,
            "backend": None,
            "volume_percent": None,
            "muted": None,
            "message": "no supported system audio volume backend detected",
        }

    def _get_pulse_volume(self) -> dict[str, Any] | None:
        volume_proc = self._run_command(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
        mute_proc = self._run_command(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
        if volume_proc.returncode != 0 or mute_proc.returncode != 0:
            return None

        volume = self._extract_percent(volume_proc.stdout)
        muted = "yes" in mute_proc.stdout.lower()
        return {
            "available": True,
            "backend": "pactl",
            "sink": "@DEFAULT_SINK@",
            "volume_percent": volume,
            "muted": muted,
        }

    def _set_pulse_volume(self, volume: int, *, unmute: bool) -> dict[str, Any] | None:
        set_proc = self._run_command(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{volume}%"])
        if set_proc.returncode != 0:
            return None
        if unmute:
            self._run_command(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
        return self._get_pulse_volume()

    def _get_alsa_volume(self) -> dict[str, Any] | None:
        controls = self._list_alsa_controls()
        for card, control in controls:
            proc = self._run_command(["amixer", "-c", str(card), "sget", control])
            if proc.returncode != 0:
                continue
            volume = self._extract_percent(proc.stdout)
            if volume is None:
                continue
            muted = "[off]" in proc.stdout.lower()
            return {
                "available": True,
                "backend": "amixer",
                "card": card,
                "control": control,
                "volume_percent": volume,
                "muted": muted,
            }
        return None

    def _set_alsa_volume(self, volume: int, *, unmute: bool) -> dict[str, Any] | None:
        controls = self._list_alsa_controls()
        for card, control in controls:
            args = ["amixer", "-c", str(card), "sset", control, f"{volume}%"]
            if unmute:
                args.append("unmute")
            proc = self._run_command(args)
            if proc.returncode == 0:
                return self._get_alsa_volume()
        return None

    def _list_alsa_controls(self) -> list[tuple[int, str]]:
        controls: list[tuple[int, str]] = []
        preferred = ["Master", "Speaker", "PCM", "Headphone"]
        for card in range(8):
            proc = self._run_command(["amixer", "-c", str(card), "scontrols"])
            if proc.returncode != 0:
                continue
            for line in proc.stdout.splitlines():
                if "'" not in line:
                    continue
                control = line.split("'", 2)[1]
                controls.append((card, control))

        controls.sort(key=lambda item: (preferred.index(item[1]) if item[1] in preferred else 999, item[0], item[1]))
        return controls

    def _extract_percent(self, text: str) -> int | None:
        match = _PERCENT_RE.search(text)
        if match is None:
            return None
        return int(match.group(1))


def pack_audio_frame(
    seq: int,
    samples: list[float] | np.ndarray,
    *,
    sample_rate: int = 32000,
    channels: int = 1,
) -> bytes:
    payload = np.asarray(samples, dtype=np.float32).tobytes()
    header = struct.pack("!IIHH", seq, sample_rate, channels, 0)
    return header + payload
