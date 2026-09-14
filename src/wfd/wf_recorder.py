"""Discovery and validation for the wlroots screen recorder."""

import os
import shutil
import subprocess
from typing import Optional

# AppImage wrappers exit 127 when the host wf-recorder binary is missing.
_WRAPPER_MISSING_BINARY = 127

# Cache ICC capability keyed by resolved path (help parse is a few ms).
_icc_cache: dict[str, bool] = {}


def _usable_recorder(path: str) -> bool:
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=3.0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Reject only the AppImage "command not found" exit; other non-zero codes
    # (e.g. older builds without --version) must not disable a working binary.
    return result.returncode != _WRAPPER_MISSING_BINARY


def wf_recorder_supports_icc(path: str) -> bool:
    """True when ``path`` looks like an ext-image-copy-capture (PR #347) build.

    Detection: ``--help`` advertises ``--toplevel`` (ICC/toplevel capture), or
    ``--version`` mentions ``ext-copy-capture``. Stock 0.6.0 has neither.
    """
    if not path:
        return False
    cached = _icc_cache.get(path)
    if cached is not None:
        return cached
    blob = ""
    try:
        ver = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=3.0
        )
        blob += (ver.stdout or "") + (ver.stderr or "")
        help_ = subprocess.run(
            [path, "--help"], capture_output=True, text=True, timeout=3.0
        )
        blob += (help_.stdout or "") + (help_.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        _icc_cache[path] = False
        return False
    lower = blob.lower()
    ok = ("--toplevel" in lower) or ("ext-copy-capture" in lower)
    _icc_cache[path] = ok
    return ok


def find_wf_recorder() -> Optional[str]:
    """Return a usable wf-recorder path, or None if a wrapper cannot run it.

    Selection (first match wins):

    1. ``FLUXCAST_WFD_WF_RECORDER_BIN`` if set and usable
    2. ``PATH`` ``wf-recorder`` if usable

    When ``FLUXCAST_WFD_WF_RECORDER_PROTO=icc``, the chosen binary must support
    ext-image-copy-capture (see ``wf_recorder_supports_icc``); otherwise return
    None so the caller can fail closed. ``wlr`` / unset / ``auto`` accept any
    usable binary (default stays stock PATH — ICC is opt-in via BIN or PROTO).
    """
    proto = (os.environ.get("FLUXCAST_WFD_WF_RECORDER_PROTO") or "").strip().lower()
    override = (os.environ.get("FLUXCAST_WFD_WF_RECORDER_BIN") or "").strip()
    candidates: list[str] = []
    if override:
        candidates.append(override)
    which = shutil.which("wf-recorder")
    if which and which not in candidates:
        candidates.append(which)

    chosen: Optional[str] = None
    for path in candidates:
        if _usable_recorder(path):
            chosen = path
            break
    if chosen is None:
        return None

    if proto == "icc" and not wf_recorder_supports_icc(chosen):
        return None
    return chosen
