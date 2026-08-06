"""Public surface of the capture package; the code lives in capture.py."""

from .capture import (  # noqa: F401
    CaptureResult, CaptureStartError, FFMPEG_PATH, Monitor, SessionInfo,
    choose_capture_backend, describe_capture_selection, detect_session,
    gather_monitors, prompt_monitor, start_capture, stop_capture
)
