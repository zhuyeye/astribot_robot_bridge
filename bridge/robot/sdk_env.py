"""Bind Astribot SDK import paths for Orin services."""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_SDK_ROOT = Path("/home/astribot/Downloads/astribot_sdk_aarch64")


def configure_sdk_paths(sdk_root: Path | None = None) -> Path:
    root = (sdk_root or Path(os.environ.get("ASTRIBOT_SDK_ROOT", str(DEFAULT_SDK_ROOT)))).resolve()
    if not root.is_dir():
        raise RuntimeError(
            f"Astribot SDK not found: {root}\n"
            f"Set ASTRIBOT_SDK_ROOT or install SDK under {DEFAULT_SDK_ROOT}"
        )

    candidates = [
        root,
        root / "third_party" / "astribot_ros_middleware_py",
    ]
    for path in candidates:
        if path.is_dir():
            entry = str(path)
            if entry not in sys.path:
                sys.path.insert(0, entry)

    os.environ.setdefault("ASTRIBOT_SDK_ROOT", str(root))
    return root


def import_astribot_sdk(sdk_root: Path | None = None):
    configure_sdk_paths(sdk_root)
    try:
        import astribot_ros_middleware as ros_mw
        from astribot_sdk.core.astribot_api.astribot_client import Astribot
    except ImportError as exc:
        raise RuntimeError(
            "Failed to import astribot_sdk. Start with scripts/start_bridge.sh "
            "so env.sh configures ROS and native libraries."
        ) from exc
    return ros_mw, Astribot
