"""Compositor-agnostic Wayland output probes.

Prefers ``wlr-randr --json`` (wlr-output-management) so Hyprland, Sway, and
other wlroots compositors share one path. Falls back to ``hyprctl -j monitors``
when wlr-randr is missing or fails.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Optional


def _check_json(cmd: list[str]) -> Optional[Any]:
    try:
        raw = subprocess.check_output(
            cmd,
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
        return json.loads(raw)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _wlr_randr_monitors() -> Optional[list]:
    data = _check_json(["wlr-randr", "--json"])
    return data if isinstance(data, list) else None


def _hyprctl_monitors() -> Optional[list]:
    data = _check_json(["hyprctl", "-j", "monitors"])
    return data if isinstance(data, list) else None


def _named_monitor(monitors: list, monitor_name: str) -> Optional[dict]:
    for mon in monitors:
        if str(mon.get("name") or "") == monitor_name:
            return mon
    return None


def _format_monitor_fingerprint(
    name: str,
    width: int,
    height: int,
    refresh,
    scale,
    x: int,
    y: int,
) -> str:
    return "|".join(
        [
            name,
            str(int(width or 0)),
            str(int(height or 0)),
            str(refresh if refresh is not None else 0),
            str(scale if scale is not None else 0),
            str(int(x or 0)),
            str(int(y or 0)),
        ]
    )


def _fingerprint_from_wlr(mon: dict, monitor_name: str) -> str:
    modes = mon.get("modes") or []
    mode = next((m for m in modes if m.get("current")), None)
    if mode is None:
        mode = next((m for m in modes if m.get("preferred")), None)
    if mode is None and modes:
        mode = modes[0]
    mode = mode or {}
    pos = mon.get("position") or {}
    return _format_monitor_fingerprint(
        monitor_name,
        mode.get("width") or 0,
        mode.get("height") or 0,
        mode.get("refresh") or 0,
        mon.get("scale") or 0,
        pos.get("x") or 0,
        pos.get("y") or 0,
    )


def _fingerprint_from_hypr(mon: dict, monitor_name: str) -> str:
    return _format_monitor_fingerprint(
        monitor_name,
        mon.get("width") or 0,
        mon.get("height") or 0,
        mon.get("refreshRate") or 0,
        mon.get("scale") or 0,
        mon.get("x") or 0,
        mon.get("y") or 0,
    )


def _scale_from_mon(mon: dict) -> float:
    try:
        return float(mon.get("scale") or 1) or 1.0
    except (TypeError, ValueError):
        return 1.0


def monitor_fingerprint(monitor_name: str) -> Optional[str]:
    """Return name|w|h|refresh|scale|x|y for a compositor output, or None."""
    if not monitor_name:
        return None
    monitors = _wlr_randr_monitors()
    if monitors is not None:
        mon = _named_monitor(monitors, monitor_name)
        if mon is not None:
            return _fingerprint_from_wlr(mon, monitor_name)
    monitors = _hyprctl_monitors()
    if monitors is not None:
        mon = _named_monitor(monitors, monitor_name)
        if mon is not None:
            return _fingerprint_from_hypr(mon, monitor_name)
    return None


def monitor_scale(monitor_name: str) -> float:
    """Output scale for *monitor_name*, or 1.0 if unknown."""
    if not monitor_name:
        return 1.0
    monitors = _wlr_randr_monitors()
    if monitors is not None:
        mon = _named_monitor(monitors, monitor_name)
        if mon is not None:
            return _scale_from_mon(mon)
    monitors = _hyprctl_monitors()
    if monitors is not None:
        mon = _named_monitor(monitors, monitor_name)
        if mon is not None:
            return _scale_from_mon(mon)
    return 1.0
