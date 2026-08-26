"""Discovery and validation for the wlroots screen recorder."""

import shutil
import subprocess
from typing import Optional

# AppImage wrappers exit 127 when the host wf-recorder binary is missing.
_WRAPPER_MISSING_BINARY = 127


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
    # Reject only the AppImage "command not found" exit; other non-zero codes
    # (e.g. older builds without --version) must not disable a working binary.
    if result.returncode == _WRAPPER_MISSING_BINARY:
        return None
    return path
