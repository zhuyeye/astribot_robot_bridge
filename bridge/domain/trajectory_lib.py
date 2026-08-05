"""HDF5 trajectory loading, 250Hz offline interpolation, and cache."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from bridge.robot.joint_model import TRAJECTORY_PARTS, WHOLE_BODY_INDEX

REPLAY_NAMES = TRAJECTORY_PARTS
DEFAULT_CONTROL_HZ = 250.0
DOF_SLICES = [4, 7, 1, 7, 1, 2]
HEAD_SLICE = WHOLE_BODY_INDEX["astribot_head"]
FORWARD_HEAD_POSE = [0.0, 0.0]


def normalize_head_pose(value: Any) -> list[float] | None:
    if value is None:
        return None
    pose = [float(v) for v in value]
    if len(pose) != 2:
        raise ValueError("head_pose must have exactly two values")
    return pose


def head_pose_tag(head_pose: list[float] | None) -> str:
    if head_pose is None:
        return ""

    def fmt(value: float) -> str:
        return f"{value:+.3f}".replace("+", "p").replace("-", "m").replace(".", "d")

    return f"_head{fmt(head_pose[0])}_{fmt(head_pose[1])}"


def apply_head_pose_to_rows(
    joint_rows: list[list[float]],
    head_pose: list[float],
) -> list[list[float]]:
    start, end = HEAD_SLICE
    patched = [list(row) for row in joint_rows]
    for row in patched:
        row[start:end] = head_pose
    return patched


@dataclass
class ActionTrajectory:
    action_id: str
    path: Path
    names: list[str]
    waypoints: list[list[list[float]]]
    control_hz: float
    loop: bool = False
    transition_s: float | None = None
    cache_path: Path | None = None

    @property
    def frames(self) -> int:
        return len(self.waypoints)

    @property
    def duration_s(self) -> float:
        if self.frames <= 1:
            return 0.0
        return (self.frames - 1) / self.control_hz


def load_hdf5_joint_rows(path: Path) -> tuple[list[list[float]], list[float]]:
    with h5py.File(path, "r") as root:
        joints = root["joints_dict/joints_position_command"][()].tolist()
        times = root["time"][()].tolist()
    return joints, times


def trim_joint_rows(
    joint_rows: list[list[float]],
    times: list[float],
    *,
    trim_start: int | None = None,
    trim_end: int | None = None,
) -> tuple[list[list[float]], list[float]]:
    """Slice keyframes by inclusive frame indices."""
    if trim_start is None and trim_end is None:
        return joint_rows, times
    if not joint_rows:
        raise ValueError("empty trajectory")

    start = 0 if trim_start is None else int(trim_start)
    end = len(joint_rows) - 1 if trim_end is None else int(trim_end)
    if start < 0 or end < 0 or start > end:
        raise ValueError(f"invalid trim range: start={start}, end={end}")
    if end >= len(joint_rows):
        raise ValueError(
            f"trim_end={end} out of range for trajectory with {len(joint_rows)} frames"
        )

    return joint_rows[start : end + 1], times[start : end + 1]


def row_to_flat(row: list[float], names: list[str] | None = None) -> list[float]:
    names = names or REPLAY_NAMES
    flat: list[float] = []
    for name in names:
        start, end = WHOLE_BODY_INDEX[name]
        flat.extend(row[start:end])
    return flat


def flat_to_waypoint(flat: list[float] | np.ndarray) -> list[list[float]]:
    values = list(flat)
    waypoint: list[list[float]] = []
    offset = 0
    for size in DOF_SLICES:
        waypoint.append(values[offset : offset + size])
        offset += size
    return waypoint


def _find_segment(times: list[float], t: float) -> tuple[int, float]:
    if t <= times[0]:
        return 0, 0.0
    if t >= times[-1]:
        return len(times) - 2, 1.0

    idx = 0
    while idx < len(times) - 2 and times[idx + 1] < t:
        idx += 1

    t0 = times[idx]
    t1 = times[idx + 1]
    if t1 <= t0:
        return idx, 0.0
    alpha = (t - t0) / (t1 - t0)
    return idx, min(max(alpha, 0.0), 1.0)


def interpolate_rows_to_control_hz(
    joint_rows: list[list[float]],
    times: list[float],
    *,
    names: list[str] | None = None,
    control_hz: float = DEFAULT_CONTROL_HZ,
) -> list[list[list[float]]]:
    """Resample recorded joints to a fixed control rate using linear interpolation."""
    names = names or REPLAY_NAMES
    if not joint_rows:
        raise ValueError("empty trajectory")
    if len(joint_rows) == 1:
        return [flat_to_waypoint(row_to_flat(joint_rows[0], names))]

    rel_times = [float(t - times[0]) for t in times]
    duration = rel_times[-1]
    if duration <= 0.0:
        return [flat_to_waypoint(row_to_flat(joint_rows[0], names))]

    key_flats = [row_to_flat(row, names) for row in joint_rows]
    dt = 1.0 / control_hz
    frames: list[list[list[float]]] = []

    sample_t = 0.0
    while sample_t <= duration + 1e-9:
        seg_idx, alpha = _find_segment(rel_times, sample_t)
        start = key_flats[seg_idx]
        end = key_flats[seg_idx + 1]
        flat = [s + alpha * (e - s) for s, e in zip(start, end)]
        frames.append(flat_to_waypoint(flat))
        sample_t += dt

    return frames


def cache_file_for(
    action_id: str,
    source_path: Path,
    control_hz: float,
    cache_dir: Path,
    *,
    trim_start: int | None = None,
    trim_end: int | None = None,
    head_pose: list[float] | None = None,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    stem = source_path.stem
    trim_tag = ""
    if trim_start is not None or trim_end is not None:
        start = -1 if trim_start is None else trim_start
        end = -1 if trim_end is None else trim_end
        trim_tag = f"_t{start}_{end}"
    return cache_dir / f"{action_id}_{stem}_{int(control_hz)}hz{trim_tag}{head_pose_tag(head_pose)}.npz"


def save_interpolated_cache(
    cache_path: Path,
    *,
    waypoints: list[list[list[float]]],
    names: list[str],
    control_hz: float,
    source_path: Path,
    trim_start: int | None = None,
    trim_end: int | None = None,
    head_pose: list[float] | None = None,
) -> None:
    flat_rows = [[value for group in wp for value in group] for wp in waypoints]
    data = np.asarray(flat_rows, dtype=np.float64)
    if head_pose is None:
        head_pose_values = np.asarray([-1.0, -1.0], dtype=np.float64)
    else:
        head_pose_values = np.asarray(head_pose, dtype=np.float64)

    np.savez_compressed(
        cache_path,
        data=data,
        control_hz=np.asarray([control_hz], dtype=np.float64),
        source_path=np.asarray([str(source_path.resolve())]),
        source_mtime=np.asarray([source_path.stat().st_mtime], dtype=np.float64),
        trim_start=np.asarray([-1 if trim_start is None else trim_start], dtype=np.int64),
        trim_end=np.asarray([-1 if trim_end is None else trim_end], dtype=np.int64),
        head_pose=head_pose_values,
        names=np.asarray(names, dtype=object),
    )


def load_interpolated_cache(cache_path: Path, names: list[str]) -> tuple[list[list[list[float]]], float]:
    with np.load(cache_path, allow_pickle=True) as archive:
        data = archive["data"]
        control_hz = float(archive["control_hz"][0])
        cached_names = archive["names"].tolist()
        if list(cached_names) != names:
            raise ValueError("cached names mismatch")
        waypoints = [flat_to_waypoint(row) for row in data]
    return waypoints, control_hz


def _expected_head_pose_values(head_pose: list[float] | None) -> np.ndarray:
    if head_pose is None:
        return np.asarray([-1.0, -1.0], dtype=np.float64)
    return np.asarray(head_pose, dtype=np.float64)


def _cache_is_valid(
    cache_path: Path,
    source_path: Path,
    *,
    trim_start: int | None = None,
    trim_end: int | None = None,
    head_pose: list[float] | None = None,
) -> bool:
    if not cache_path.is_file():
        return False
    try:
        with np.load(cache_path, allow_pickle=True) as archive:
            cached_path = str(archive["source_path"][0])
            cached_mtime = float(archive["source_mtime"][0])
            cached_trim_start = int(archive["trim_start"][0]) if "trim_start" in archive else -1
            cached_trim_end = int(archive["trim_end"][0]) if "trim_end" in archive else -1
            if "head_pose" in archive:
                cached_head_pose = np.asarray(archive["head_pose"], dtype=np.float64)
            else:
                cached_head_pose = np.asarray([-1.0, -1.0], dtype=np.float64)
        expected_trim_start = -1 if trim_start is None else trim_start
        expected_trim_end = -1 if trim_end is None else trim_end
        return (
            cached_path == str(source_path.resolve())
            and cached_mtime == source_path.stat().st_mtime
            and cached_trim_start == expected_trim_start
            and cached_trim_end == expected_trim_end
            and np.allclose(cached_head_pose, _expected_head_pose_values(head_pose))
        )
    except (KeyError, OSError, ValueError):
        return False


def build_or_load_interpolated_trajectory(
    action_id: str,
    source_path: Path,
    *,
    names: list[str] | None = None,
    control_hz: float = DEFAULT_CONTROL_HZ,
    cache_dir: Path,
    trim_start: int | None = None,
    trim_end: int | None = None,
    head_pose: list[float] | None = None,
    force_rebuild: bool = False,
) -> ActionTrajectory:
    names = names or REPLAY_NAMES
    cache_path = cache_file_for(
        action_id,
        source_path,
        control_hz,
        cache_dir,
        trim_start=trim_start,
        trim_end=trim_end,
        head_pose=head_pose,
    )

    if not force_rebuild and _cache_is_valid(
        cache_path,
        source_path,
        trim_start=trim_start,
        trim_end=trim_end,
        head_pose=head_pose,
    ):
        waypoints, cached_hz = load_interpolated_cache(cache_path, names)
        print(f"[trajectory] loaded cache: {cache_path.name} ({len(waypoints)} frames @ {cached_hz}Hz)")
        return ActionTrajectory(
            action_id=action_id,
            path=source_path,
            names=names,
            waypoints=waypoints,
            control_hz=cached_hz,
            cache_path=cache_path,
        )

    joint_rows, times = load_hdf5_joint_rows(source_path)
    raw_frames = len(joint_rows)
    joint_rows, times = trim_joint_rows(
        joint_rows,
        times,
        trim_start=trim_start,
        trim_end=trim_end,
    )
    if head_pose is not None:
        joint_rows = apply_head_pose_to_rows(joint_rows, head_pose)
    waypoints = interpolate_rows_to_control_hz(
        joint_rows,
        times,
        names=names,
        control_hz=control_hz,
    )
    save_interpolated_cache(
        cache_path,
        waypoints=waypoints,
        names=names,
        control_hz=control_hz,
        source_path=source_path,
        trim_start=trim_start,
        trim_end=trim_end,
        head_pose=head_pose,
    )
    trim_note = ""
    if trim_start is not None or trim_end is not None:
        trim_note = f", trim=[{trim_start}, {trim_end}]"
    if head_pose is not None:
        trim_note += f", head_pose={head_pose}"
    print(
        f"[trajectory] built cache: {cache_path.name} "
        f"({raw_frames} raw -> {len(joint_rows)} keyframes -> {len(waypoints)} frames "
        f"@ {control_hz}Hz{trim_note})"
    )
    return ActionTrajectory(
        action_id=action_id,
        path=source_path,
        names=names,
        waypoints=waypoints,
        control_hz=control_hz,
        cache_path=cache_path,
    )


def load_action_from_hdf5(
    action_id: str,
    path: Path,
    *,
    control_hz: float = DEFAULT_CONTROL_HZ,
    cache_dir: Path,
    loop: bool = False,
    transition_s: float | None = None,
    trim_start: int | None = None,
    trim_end: int | None = None,
    head_pose: list[float] | None = None,
    names: list[str] | None = None,
    force_rebuild: bool = False,
) -> ActionTrajectory:
    action = build_or_load_interpolated_trajectory(
        action_id,
        path.resolve(),
        names=names,
        control_hz=control_hz,
        cache_dir=cache_dir,
        trim_start=trim_start,
        trim_end=trim_end,
        head_pose=head_pose,
        force_rebuild=force_rebuild,
    )
    action.loop = loop
    action.transition_s = transition_s
    return action


def load_action_library(
    manifest_path: Path,
    *,
    force_rebuild: bool = False,
) -> dict[str, ActionTrajectory]:
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest: dict[str, Any] = json.load(f)

    base_dir = manifest_path.parent
    control_hz = float(manifest.get("control_hz", DEFAULT_CONTROL_HZ))
    default_head_pose = normalize_head_pose(manifest.get("head_pose"))
    cache_dir = base_dir / "cache"
    actions: dict[str, ActionTrajectory] = {}

    for action_id, entry in manifest.get("actions", {}).items():
        raw_path = entry["path"]
        path = Path(raw_path) if Path(raw_path).is_absolute() else (base_dir / raw_path)
        action_hz = float(entry.get("control_hz", control_hz))
        trim_start = entry.get("trim_start")
        trim_end = entry.get("trim_end")
        if trim_start is None and trim_end is None and "trim" in entry:
            trim_values = entry["trim"]
            if len(trim_values) != 2:
                raise ValueError(f"action '{action_id}' trim must have exactly two values")
            trim_start, trim_end = trim_values
        if "head_pose" in entry:
            head_pose = normalize_head_pose(entry["head_pose"])
        else:
            head_pose = default_head_pose
        actions[action_id] = load_action_from_hdf5(
            action_id,
            path.resolve(),
            control_hz=action_hz,
            cache_dir=cache_dir,
            loop=bool(entry.get("loop", False)),
            transition_s=(
                float(entry["transition_s"]) if entry.get("transition_s") is not None else None
            ),
            trim_start=int(trim_start) if trim_start is not None else None,
            trim_end=int(trim_end) if trim_end is not None else None,
            head_pose=head_pose,
            force_rebuild=force_rebuild,
        )
    return actions


def load_action_tags(manifest_path: Path) -> dict[str, list[str]]:
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest: dict[str, Any] = json.load(f)

    tags: dict[str, list[str]] = {}
    for action_id, entry in manifest.get("actions", {}).items():
        tag = str(entry.get("tag", action_id)).strip()
        if not tag:
            tag = action_id
        tags.setdefault(tag, []).append(action_id)
    return tags
