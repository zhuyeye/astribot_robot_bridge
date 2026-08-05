"""Joint command safety checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

SDK_LIMITS: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {
    "astribot_torso": (
        (-0.04, -2.30, -0.40, -1.20),
        (1.30, 0.06, 2.30, 1.20),
    ),
    "astribot_arm_left": (
        (-3.10, -1.53, -3.10, -0.06, -2.56, -0.76, -1.53),
        (3.10, 0.46, 3.10, 2.61, 2.56, 0.76, 1.53),
    ),
    "astribot_arm_right": (
        (-3.10, -1.53, -3.10, -0.06, -2.56, -0.76, -1.53),
        (3.10, 0.46, 3.10, 2.61, 2.56, 0.76, 1.53),
    ),
    "astribot_head": ((-1.57, -1.22), (1.57, 1.22)),
}


@dataclass
class CommandViolation:
    part: str
    index: int
    value: float
    reason: str
    limit_lower: float | None = None
    limit_upper: float | None = None
    max_delta: float | None = None
    delta: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def check_joint_targets(
    names: list[str],
    values: list[list[float]],
    *,
    prev: dict[str, list[float]] | None = None,
    check_step_delta: bool = True,
    max_step_delta_rad: float = 0.35,
    max_abs_rad: float = 3.5,
    gripper_min: float = 0.0,
    gripper_max: float = 100.0,
) -> list[CommandViolation]:
    violations: list[CommandViolation] = []
    current = {name: group for name, group in zip(names, values)}

    for name, group in current.items():
        if name.startswith("astribot_gripper"):
            for i, value in enumerate(group):
                if value < gripper_min or value > gripper_max:
                    violations.append(
                        CommandViolation(
                            part=name,
                            index=i,
                            value=float(value),
                            reason="gripper_out_of_range",
                            limit_lower=gripper_min,
                            limit_upper=gripper_max,
                        )
                    )
            continue

        if name == "astribot_chassis":
            continue

        limits = SDK_LIMITS.get(name)
        if limits is None:
            for i, value in enumerate(group):
                if abs(value) > max_abs_rad:
                    violations.append(
                        CommandViolation(
                            part=name,
                            index=i,
                            value=float(value),
                            reason="exceeds_max_abs",
                            limit_lower=-max_abs_rad,
                            limit_upper=max_abs_rad,
                        )
                    )
            continue

        lower, upper = limits
        for i, value in enumerate(group):
            if i < len(lower) and value < lower[i]:
                violations.append(
                    CommandViolation(
                        part=name,
                        index=i,
                        value=float(value),
                        reason="below_lower_limit",
                        limit_lower=float(lower[i]),
                        limit_upper=float(upper[i]) if i < len(upper) else None,
                    )
                )
            if i < len(upper) and value > upper[i]:
                violations.append(
                    CommandViolation(
                        part=name,
                        index=i,
                        value=float(value),
                        reason="above_upper_limit",
                        limit_lower=float(lower[i]) if i < len(lower) else None,
                        limit_upper=float(upper[i]),
                    )
                )
            if abs(value) > max_abs_rad:
                violations.append(
                    CommandViolation(
                        part=name,
                        index=i,
                        value=float(value),
                        reason="exceeds_max_abs",
                        limit_lower=-max_abs_rad,
                        limit_upper=max_abs_rad,
                    )
                )
            if prev is not None and check_step_delta and name in prev and i < len(prev[name]):
                delta = float(value) - float(prev[name][i])
                if abs(delta) > max_step_delta_rad:
                    violations.append(
                        CommandViolation(
                            part=name,
                            index=i,
                            value=float(value),
                            reason="exceeds_max_step_delta",
                            max_delta=max_step_delta_rad,
                            delta=delta,
                        )
                    )

    return violations
