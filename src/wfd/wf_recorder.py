"""Discovery and validation for the wlroots screen recorder."""

import shutil
import subprocess
from typing import Optional


def find_wf_recorder() -> Optional[str]:
    """Return a usable wf-recorder path, or None if a wrapper cannot run it."""
    path = shutil.which("wf-recorder")
    if not path:
        return None
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=3.0
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if result.returncode == 0 else None
