import os
import threading
import time
from typing import Optional


def _process_written_bytes(pid: int) -> Optional[int]:
    """Bytes a process has written, pipes included (Linux /proc/<pid>/io)."""
    try:
        with open(f"/proc/{pid}/io", "r") as handle:
            for line in handle:
                if line.startswith("wchar:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None

_ts_dump_reported: set = set()

def report_ts_dump(dump_ts_path: Optional[str]) -> None:
    # Runs both from a timer and from teardown, whichever comes first, so a
    # short Ctrl+C run still prints the self-check (#84).
    if not dump_ts_path or dump_ts_path in _ts_dump_reported:
        return
    _ts_dump_reported.add(dump_ts_path)
    try:
        if not os.path.exists(dump_ts_path) or os.path.getsize(dump_ts_path) < 188 * 20:
            print(
                f"[FluxCast WFD TS] {dump_ts_path} is empty or too small to analyse; "
                "the media pipeline produced almost nothing."
            )
            return
        from . import ts_probe

        print(ts_probe.format_report(ts_probe.analyze(dump_ts_path)))
    except Exception as exc:  # diagnostics must never break a session
        print(f"[FluxCast WFD TS] Self-check failed: {exc}")

def schedule_ts_dump_report(dump_ts_path: Optional[str], delay: float = 8.0) -> None:
    if not dump_ts_path:
        return

    def _run() -> None:
        time.sleep(delay)
        report_ts_dump(dump_ts_path)

    threading.Thread(target=_run, daemon=True, name="wfd-ts-probe").start()
