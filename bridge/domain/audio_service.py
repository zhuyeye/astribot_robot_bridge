"""Audio clip playback and PCM streaming.

Two backends:

- ``sdk``: ROS ``/astribot_audio/speaker/stream`` (robot speaker driver).
- ``system``: Orin PulseAudio **Yundea USB** sink — same path as a manual
  ``paplay`` / ``pacat`` test, **not** the Jetson built-in APE codec.

Verified audible path on this robot (Yundea USB speaker)::

    paplay / pacat --device=<Yundea sink>
      → Unix socket /run/user/1000/pulse/native
      → pulseaudio (user astribot)
      → sink: alsa_output.usb-...Yundea_8MICA...analog-stereo
      → PulseAudio module-alsa-card.c
      → ALSA card 2 / snd_usb_audio  (Y8MICA)
      → USB Yundea 8MICA (usb-3610000.usb-1.4)
      → speaker

Bridge ``system`` pins this sink (config ``system_sink_match: Yundea``), sets
Pulse default sink + startup volume (default 75%), and volume APIs
(``GET/POST /v1/audio/system-volume``) operate on that same Yundea sink.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path
import re
from typing import Any

import numpy as np

from bridge.config import BRIDGE_ROOT, BridgeConfig
from bridge.robot.adapter import RobotAdapter
from bridge.schemas.common import BridgeError

logger = logging.getLogger(__name__)

CHUNK_SECONDS_DEFAULT = 0.1
_PERCENT_RE = re.compile(r"(\d{1,3})%")


def match_pulse_sink(sinks_short: str, match: str) -> str | None:
    needle = (match or "").strip().lower()
    if not needle:
        return None
    for line in sinks_short.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if needle in name.lower():
            return name
    return None


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
        self._dump_wav: wave.Wave_write | None = None
        self._dump_path: Path | None = None
        self._dump_sample_rate: int | None = None
        self._dump_channels: int | None = None
        self._dump_frames_written = 0
        self._play_backend: str | None = None
        self._system_play_proc: subprocess.Popen | None = None
        self._system_stream_rate: int | None = None
        self._system_stream_channels: int | None = None
        self._system_sink: str | None = None

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
                "play_backend": self._play_backend,
                "audio_activated": self.robot.audio_activated,
                "dataset_dir": str(self.dataset_dir),
                "system_volume": system_volume,
                "system_sink": self._system_sink,
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
                "dump_received_wav": bool(self.config.audio.dump_received_wav),
                "dump_path": str(self._dump_path) if self._dump_path is not None else None,
                "dump_frames_written": self._dump_frames_written,
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
            self._kill_system_play_unlocked()
            self._playing = True
            self._stream_active = False
            self._play_backend = "sdk"
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
                    if self._play_backend == "sdk":
                        self._play_backend = None

        threading.Thread(target=_runner, name="audio-play", daemon=True).start()
        return {"accepted": True, "clip": wav_path.name, "mode": mode, "path": str(wav_path), "backend": "sdk"}

    def play_system_clip(
        self,
        *,
        clip_id: str | None = None,
        path: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Play wav via Pulse Yundea USB sink (paplay --device)."""
        wav_path = self._resolve_wav(clip_id, path)
        logger.info(
            "audio system-play requested clip=%s path=%s force=%s",
            clip_id,
            path,
            force,
        )

        with self._lock:
            if (self._playing or self._stream_active) and not force:
                raise BridgeError("audio_busy", "audio session busy", status_code=409, details=self.status())
            self._stop.set()
            self._kill_system_play_unlocked()
            self._playing = True
            self._stream_active = False
            self._play_backend = "system"
            self._stop = threading.Event()
            self._drain_frame_queue()
            self._close_dump_wav_unlocked()

        def _runner() -> None:
            try:
                player, duration = self._play_via_system(wav_path)
                logger.info(
                    "audio system-play done: %s via %s (%.2fs)",
                    wav_path.name,
                    player,
                    duration,
                )
            except Exception:
                logger.exception("audio system-play failed")
            finally:
                with self._lock:
                    self._playing = False
                    if self._play_backend == "system":
                        self._play_backend = None
                    self._system_play_proc = None

        threading.Thread(target=_runner, name="audio-system-play", daemon=True).start()
        return {
            "accepted": True,
            "clip": wav_path.name,
            "path": str(wav_path),
            "backend": "system",
            "sink": self._resolve_system_sink() or "@DEFAULT_SINK@",
        }

    def stop(self) -> dict[str, Any]:
        logger.info("audio stop requested")
        with self._lock:
            self._stop.set()
            self._playing = False
            self._stream_active = False
            self._play_backend = None
            self._kill_system_play_unlocked()
            self._drain_frame_queue()
            dump_path = self._close_dump_wav_unlocked()
        try:
            logger.info("audio deactivate begin")
            self.robot.deactivate_audio()
            logger.info("audio deactivate done")
        except Exception:
            logger.exception("deactivate_audio failed")
        status = self.status()
        if dump_path is not None:
            status["dump_path"] = str(dump_path)
        return status
    def close(self) -> None:
        self.stop()
        if self._stream_worker is not None:
            self._stream_worker.join(timeout=1.0)

    def start_stream(self, *, force: bool = False, backend: str | None = None) -> dict[str, Any]:
        return self.start_stream_with_rights(force=force, reacquire_if_needed=False, backend=backend)

    def _resolve_stream_backend(self, requested: str | None) -> str:
        forced = self.config.audio.force_stream_backend
        if forced is not None:
            out = forced.strip().lower()
            if requested is not None and requested.strip().lower() != out:
                logger.warning(
                    "audio stream backend=%s requested but force_stream_backend=%s; using forced",
                    requested,
                    out,
                )
            return out
        if requested is not None and str(requested).strip():
            return str(requested).strip().lower()
        return self.config.audio.default_stream_backend.strip().lower()

    def start_stream_with_rights(
        self,
        *,
        force: bool = False,
        reacquire_if_needed: bool | None = None,
        backend: str | None = None,
    ) -> dict[str, Any]:
        backend_norm = self._resolve_stream_backend(backend)
        if backend_norm not in ("sdk", "system"):
            raise BridgeError("invalid_request", "backend must be 'sdk' or 'system'")
        logger.info("audio stream start requested force=%s backend=%s", force, backend_norm)
        with self._lock:
            if (self._playing or self._stream_active) and not force:
                raise BridgeError("audio_busy", "audio session busy", status_code=409, details=self.status())
            self._stop.set()
            self._kill_system_play_unlocked()
            self._playing = False
            self._stream_active = True
            self._play_backend = backend_norm
            self._stream_seq = 0
            self._stop = threading.Event()
            self._worker_error = None
            self._last_frame_ts = None
            self._last_publish_ts = None
            self._received_frames = 0
            self._received_bytes = 0
            self._published_frames = 0
            self._dropped_frames = 0
            self._last_stats_log_ts = time.time()
            self._system_stream_rate = None
            self._system_stream_channels = None
            self._drain_frame_queue()
            self._close_dump_wav_unlocked()
            self._prepare_dump_session_unlocked()
        if backend_norm == "sdk":
            self._ensure_activated()
            self._ensure_publisher()
        elif shutil.which("pacat") is None and shutil.which("paplay") is None:
            with self._lock:
                self._stream_active = False
                self._play_backend = None
            raise BridgeError(
                "system_audio_unavailable",
                "pacat/paplay not found; cannot stream to system sink",
                status_code=503,
            )
        self._ensure_stream_worker()
        logger.info(
            "audio stream start ready backend=%s dump_wav=%s dump_path=%s",
            backend_norm,
            self.config.audio.dump_received_wav,
            self._dump_path,
        )
        return {
            "accepted": True,
            "stream_active": True,
            "backend": backend_norm,
            "sink": self._resolve_system_sink() if backend_norm == "system" else None,
            "dump_path": str(self._dump_path) if self._dump_path is not None else None,
        }
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
            self._append_dump_wav_unlocked(samples, sample_rate=sample_rate, channels=channels)

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
            self._play_backend = None
            self._kill_system_play_unlocked()
            self._drain_frame_queue()
            self._close_dump_wav_unlocked()

    def _dump_dir(self) -> Path:
        raw = Path(self.config.audio.dump_dir)
        if raw.is_absolute():
            return raw
        return (BRIDGE_ROOT / raw).resolve()

    def _prepare_dump_session_unlocked(self) -> None:
        self._dump_wav = None
        self._dump_path = None
        self._dump_sample_rate = None
        self._dump_channels = None
        self._dump_frames_written = 0
        if not self.config.audio.dump_received_wav:
            return
        dump_dir = self._dump_dir()
        dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self._dump_path = dump_dir / f"received_{stamp}.wav"
        logger.info("audio dump armed path=%s", self._dump_path)

    def _append_dump_wav_unlocked(
        self,
        samples: list[float],
        *,
        sample_rate: int,
        channels: int,
    ) -> None:
        if not self.config.audio.dump_received_wav or self._dump_path is None:
            return
        try:
            if self._dump_wav is None:
                channels = max(1, int(channels))
                self._dump_sample_rate = int(sample_rate)
                self._dump_channels = channels
                wf = wave.open(str(self._dump_path), "wb")
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(self._dump_sample_rate)
                self._dump_wav = wf
                logger.info(
                    "audio dump opened path=%s sample_rate=%s channels=%s",
                    self._dump_path,
                    self._dump_sample_rate,
                    channels,
                )
            elif self._dump_sample_rate != int(sample_rate) or self._dump_channels != int(channels):
                logger.warning(
                    "audio dump format changed mid-stream (sr %s->%s ch %s->%s); keeping first format",
                    self._dump_sample_rate,
                    sample_rate,
                    self._dump_channels,
                    channels,
                )
            arr = np.asarray(samples, dtype=np.float32)
            if arr.size == 0:
                return
            arr = np.clip(arr, -1.0, 1.0)
            pcm = (arr * 32767.0).astype("<i2")
            assert self._dump_wav is not None
            self._dump_wav.writeframes(pcm.tobytes())
            # nframes counts frames, not samples; for multi-channel, wave tracks frames.
            self._dump_frames_written += int(arr.size // max(1, self._dump_channels or 1))
        except Exception:
            logger.exception("audio dump write failed path=%s", self._dump_path)

    def _close_dump_wav_unlocked(self) -> Path | None:
        path = self._dump_path
        wf = self._dump_wav
        frames = self._dump_frames_written
        self._dump_wav = None
        self._dump_sample_rate = None
        self._dump_channels = None
        if wf is not None:
            try:
                wf.close()
            except Exception:
                logger.exception("audio dump close failed path=%s", path)
            logger.info("audio dump closed path=%s frames=%s", path, frames)
        # Keep last path visible in status until next stream.
        return path if frames > 0 else path

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
                with self._lock:
                    backend = self._play_backend
                if backend == "system":
                    self._write_system_stream_chunk(samples, channels=channels, samplerate=sample_rate)
                else:
                    self._publish_chunk(samples, channels=channels, samplerate=sample_rate)
                with self._lock:
                    self._last_publish_ts = time.time()
                    self._published_frames += 1
            except Exception as exc:
                with self._lock:
                    self._worker_error = str(exc)
                    self._stream_active = False
                    self._stop.set()
                    self._kill_system_play_unlocked()
                logger.exception("audio stream worker failed")
                return

    def _ensure_system_stream(self, *, sample_rate: int, channels: int) -> None:
        """Start pacat → Pulse Yundea USB sink (same path as paplay; no SDK)."""
        channels = max(1, int(channels))
        sample_rate = int(sample_rate)
        with self._lock:
            proc = self._system_play_proc
            same = (
                proc is not None
                and proc.poll() is None
                and self._system_stream_rate == sample_rate
                and self._system_stream_channels == channels
            )
            if same:
                return
            self._kill_system_play_unlocked()

        cmd_name = "pacat" if shutil.which("pacat") else None
        if cmd_name is None:
            raise RuntimeError("pacat not found for system PCM stream")
        cmd = [
            "pacat",
            "--playback",
            "--raw",
            "--format=float32le",
            f"--rate={sample_rate}",
            f"--channels={channels}",
            "--latency-msec=50",
        ]
        sink = self._resolve_system_sink()
        if sink:
            cmd[1:1] = ["--device", sink]
        logger.info("audio system-stream launching: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=self._pulse_env(),
        )
        with self._lock:
            self._system_play_proc = proc
            self._system_stream_rate = sample_rate
            self._system_stream_channels = channels

    def _write_system_stream_chunk(
        self,
        samples: list[float],
        *,
        channels: int,
        samplerate: int,
    ) -> None:
        self._ensure_system_stream(sample_rate=samplerate, channels=channels)
        with self._lock:
            proc = self._system_play_proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("system stream process not ready")
        if proc.poll() is not None:
            err = ""
            if proc.stderr is not None:
                try:
                    err = proc.stderr.read().decode("utf-8", errors="replace").strip()
                except Exception:
                    err = ""
            raise RuntimeError(f"system stream exited early: {err or proc.returncode}")
        pcm = np.asarray(samples, dtype="<f4").tobytes()
        try:
            proc.stdin.write(pcm)
            proc.stdin.flush()
        except BrokenPipeError as exc:
            raise RuntimeError("system stream pipe broken") from exc

    def _kill_system_play_unlocked(self) -> None:
        proc = self._system_play_proc
        self._system_play_proc = None
        self._system_stream_rate = None
        self._system_stream_channels = None
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        self._terminate_process(proc)

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

    def _play_via_system(self, wav_path: Path) -> tuple[str, float]:
        """Play a wav through Pulse Yundea sink: paplay --device → pulse native socket."""
        audio, samplerate = self._load_wav(wav_path)
        duration = float(len(audio) / max(1, samplerate))
        sink = self._resolve_system_sink()
        paplay_cmd = ["paplay", str(wav_path)]
        if sink:
            paplay_cmd = ["paplay", "--device", sink, str(wav_path)]
        candidates: list[list[str]] = [
            paplay_cmd,
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(wav_path)],
            ["aplay", str(wav_path)],
        ]
        errors: list[str] = []
        for cmd in candidates:
            if shutil.which(cmd[0]) is None:
                errors.append(f"{cmd[0]} not found")
                continue
            logger.info("audio system-play launching: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=self._pulse_env(),
            )
            with self._lock:
                self._system_play_proc = proc
            while proc.poll() is None:
                if self._stop.is_set():
                    self._terminate_process(proc)
                    return cmd[0], 0.0
                time.sleep(0.05)
            stderr = ""
            if proc.stderr is not None:
                try:
                    stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
                except Exception:
                    stderr = ""
            if proc.returncode == 0:
                return cmd[0], duration
            errors.append(f"{cmd[0]} exit={proc.returncode} {stderr}".strip())
        raise RuntimeError("system audio playback failed: " + "; ".join(errors))

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

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

    def apply_system_audio_defaults(self) -> None:
        """Pin Pulse default sink to Yundea and set startup volume (default 75%)."""
        sink = self._resolve_system_sink()
        if sink is None:
            logger.warning(
                "audio system sink matching %r not found; skip Pulse defaults",
                self.config.audio.system_sink_match,
            )
            return
        set_default = self._run_command(["pactl", "set-default-sink", sink])
        if set_default.returncode != 0:
            logger.warning(
                "audio failed to set Pulse default sink=%s: %s",
                sink,
                (set_default.stderr or set_default.stdout).strip(),
            )
        volume = int(self.config.audio.default_system_volume_percent)
        result = self._set_pulse_volume(volume, unmute=True)
        if result is None:
            logger.warning("audio failed to set Yundea volume to %s%%", volume)
            return
        logger.info(
            "audio system defaults applied sink=%s volume=%s%% muted=%s",
            result.get("sink"),
            result.get("volume_percent"),
            result.get("muted"),
        )

    @staticmethod
    def _pulse_env() -> dict[str, str]:
        env = os.environ.copy()
        if not env.get("XDG_RUNTIME_DIR"):
            runtime = Path(f"/run/user/{os.getuid()}")
            if runtime.is_dir():
                env["XDG_RUNTIME_DIR"] = str(runtime)
        return env

    def _resolve_system_sink(self) -> str | None:
        explicit = (self.config.audio.system_sink or "").strip()
        if explicit:
            self._system_sink = explicit
            return explicit
        if self._system_sink:
            return self._system_sink
        proc = self._run_command(["pactl", "list", "short", "sinks"])
        if proc.returncode != 0:
            logger.warning("audio pactl list sinks failed: %s", (proc.stderr or proc.stdout).strip())
            return None
        sink = match_pulse_sink(proc.stdout, self.config.audio.system_sink_match)
        if sink is None:
            return None
        self._system_sink = sink
        return sink

    def _pulse_sink_target(self) -> str:
        return self._resolve_system_sink() or "@DEFAULT_SINK@"

    def _run_command(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=self._pulse_env(),
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
            "sink": self._system_sink,
            "volume_percent": None,
            "muted": None,
            "message": "no supported system audio volume backend detected",
        }

    def _get_pulse_volume(self) -> dict[str, Any] | None:
        sink = self._pulse_sink_target()
        volume_proc = self._run_command(["pactl", "get-sink-volume", sink])
        mute_proc = self._run_command(["pactl", "get-sink-mute", sink])
        if volume_proc.returncode != 0 or mute_proc.returncode != 0:
            return None

        volume = self._extract_percent(volume_proc.stdout)
        muted = "yes" in mute_proc.stdout.lower()
        return {
            "available": True,
            "backend": "pactl",
            "sink": sink,
            "volume_percent": volume,
            "muted": muted,
        }

    def _set_pulse_volume(self, volume: int, *, unmute: bool) -> dict[str, Any] | None:
        sink = self._pulse_sink_target()
        set_proc = self._run_command(["pactl", "set-sink-volume", sink, f"{volume}%"])
        if set_proc.returncode != 0:
            return None
        if unmute:
            self._run_command(["pactl", "set-sink-mute", sink, "0"])
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
