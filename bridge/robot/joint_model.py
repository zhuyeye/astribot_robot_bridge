"""Canonical joint / part layout for Astribot S1."""

from __future__ import annotations

from typing import Iterable

# Full 25-DOF body index in HDF5 / whole-body flat vectors
WHOLE_BODY_INDEX: dict[str, tuple[int, int]] = {
    "astribot_chassis": (0, 3),
    "astribot_torso": (3, 7),
    "astribot_arm_left": (7, 14),
    "astribot_gripper_left": (14, 15),
    "astribot_arm_right": (15, 22),
    "astribot_gripper_right": (22, 23),
    "astribot_head": (23, 25),
}

PART_DOFS: dict[str, int] = {
    name: end - start for name, (start, end) in WHOLE_BODY_INDEX.items()
}

# Readable / controllable without chassis for social trajectory replay
TRAJECTORY_PARTS: list[str] = [
    "astribot_torso",
    "astribot_arm_left",
    "astribot_gripper_left",
    "astribot_arm_right",
    "astribot_gripper_right",
    "astribot_head",
]

# Default API joint set (25 DOF readable)
READABLE_PARTS: list[str] = [
    "astribot_chassis",
    "astribot_torso",
    "astribot_arm_left",
    "astribot_gripper_left",
    "astribot_arm_right",
    "astribot_gripper_right",
    "astribot_head",
]

TRAJECTORY_DOF = sum(PART_DOFS[p] for p in TRAJECTORY_PARTS)  # 22
READABLE_DOF = sum(PART_DOFS[p] for p in READABLE_PARTS)  # 25


def expand_names(names: Iterable[str] | None) -> list[str]:
    if names is None:
        return list(READABLE_PARTS)
    out = [str(n) for n in names]
    for name in out:
        if name not in WHOLE_BODY_INDEX:
            raise ValueError(f"unknown part name: {name}")
    return out


def flatten_groups(names: list[str], groups: list[list[float]]) -> list[float]:
    if len(names) != len(groups):
        raise ValueError("names/groups length mismatch")
    flat: list[float] = []
    for name, group in zip(names, groups):
        expected = PART_DOFS[name]
        if len(group) != expected:
            raise ValueError(f"{name} expects {expected} values, got {len(group)}")
        flat.extend(float(v) for v in group)
    return flat


def groups_to_dict(names: list[str], groups: list[list[float]]) -> dict[str, list[float]]:
    return {name: [float(v) for v in group] for name, group in zip(names, groups)}


def dict_to_groups(targets: dict[str, list[float]], names: list[str] | None = None) -> tuple[list[str], list[list[float]]]:
    ordered = names or [n for n in READABLE_PARTS if n in targets]
    missing = [n for n in ordered if n not in targets]
    if missing:
        raise ValueError(f"missing target parts: {missing}")
    values: list[list[float]] = []
    for name in ordered:
        group = [float(v) for v in targets[name]]
        expected = PART_DOFS[name]
        if len(group) != expected:
            raise ValueError(f"{name} expects {expected} values, got {len(group)}")
        values.append(group)
    return ordered, values


def flat_to_groups(flat: list[float], names: list[str]) -> list[list[float]]:
    expected = sum(PART_DOFS[n] for n in names)
    if len(flat) != expected:
        raise ValueError(f"flat length {len(flat)} != expected {expected} for {names}")
    groups: list[list[float]] = []
    offset = 0
    for name in names:
        size = PART_DOFS[name]
        groups.append([float(v) for v in flat[offset : offset + size]])
        offset += size
    return groups


def joint_names_expanded(parts: list[str]) -> list[str]:
    """Expand part names into per-DOF labels for API clarity."""
    labels: list[str] = []
    for part in parts:
        dof = PART_DOFS[part]
        if dof == 1:
            labels.append(part)
        else:
            labels.extend(f"{part}_{i}" for i in range(dof))
    return labels
