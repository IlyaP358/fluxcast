"""Persist sink-advertised Miracast modes for the Omarchy Display panel."""

from __future__ import annotations

import json
import os
from typing import Optional

from .config import WFDCEAMode, WFDVideoFormat
from .modes import WFD_CEA_MODES, WFD_VESA_MODES, _max_wfd_level, _wfd_level_for_mode


def _mode_state_path() -> str:
    return os.environ.get("FLUXCAST_WFD_MODE_STATE", "").strip()


def supported_modes(sink_format: Optional[WFDVideoFormat]) -> list[dict]:
    """Return FluxCast-known modes the sink's CEA/VESA masks allow."""
    if sink_format is None:
        return []
    max_level = _max_wfd_level(sink_format.level)
    out: list[dict] = []
    for bit, mode in {**WFD_CEA_MODES, **WFD_VESA_MODES}.items():
        if mode.table == "vesa":
            if not (sink_format.vesa_mask & bit):
                continue
        else:
            if not (sink_format.cea_mask & bit):
                continue
        if max_level is not None and _wfd_level_for_mode(mode) > max_level:
            continue
        out.append(_mode_dict(mode))
    # Stable UI order: resolution asc, then fps asc.
    out.sort(key=lambda m: (m["height"], m["width"], m["fps"]))
    return out


def _mode_dict(mode: WFDCEAMode) -> dict:
    return {
        "id": mode.name,
        "label": f"{mode.width}x{mode.height}@{mode.fps}",
        "width": mode.width,
        "height": mode.height,
        "fps": mode.fps,
        "resolution": f"{mode.width}x{mode.height}",
    }


def write_mode_state(
    *,
    sink_format: Optional[WFDVideoFormat],
    current: Optional[WFDCEAMode],
    peer: str = "",
    peer_name: str = "",
) -> None:
    path = _mode_state_path()
    if not path:
        return
    payload = {
        "peer": peer,
        "peerName": peer_name,
        "current": current.name if current else "",
        "supported": supported_modes(sink_format),
        "ceaMask": f"0x{sink_format.cea_mask:08x}" if sink_format else "",
        "vesaMask": f"0x{sink_format.vesa_mask:08x}" if sink_format else "",
    }
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
            fh.write("\n")
    except OSError as exc:
        print(f"[FluxCast WFD] Could not write mode state: {exc}")
