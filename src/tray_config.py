"""Validated per-mode configuration for FluxCast tray launches."""

from __future__ import annotations

import configparser
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable


Validator = Callable[[str], str | bool]
WarningHandler = Callable[[str], None]


@dataclass(frozen=True)
class _Option:
    validate: Validator
    is_flag: bool = False


def _choice(*choices: str) -> Validator:
    allowed = set(choices)

    def validate(value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in allowed:
            expected = ", ".join(choices)
            raise ValueError(f"expected one of: {expected}")
        return normalized

    return validate


def _positive_int(value: str) -> str:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError("expected a positive integer") from exc
    if parsed <= 0:
        raise ValueError("expected a positive integer")
    return str(parsed)


def _port(value: str) -> str:
    parsed = int(_positive_int(value))
    if parsed > 65535:
        raise ValueError("expected a port from 1 to 65535")
    return str(parsed)


def _resolution(value: str) -> str:
    match = re.fullmatch(r"\s*(\d+)[xX](\d+)\s*", value)
    if match is None:
        raise ValueError("expected WIDTHxHEIGHT, for example 1920x1080")
    width, height = (int(part) for part in match.groups())
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be greater than zero")
    return f"{width}x{height}"


def _bitrate(value: str) -> str:
    normalized = value.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmMgG]?)", normalized)
    if match is None:
        raise ValueError("expected a positive number with an optional K, M, or G suffix")
    try:
        positive = Decimal(match.group(1)) > 0
    except InvalidOperation as exc:
        raise ValueError("expected a positive bitrate") from exc
    if not positive:
        raise ValueError("expected a positive bitrate")
    return normalized


def _text(value: str) -> str:
    normalized = value.strip()
    if not normalized or normalized.startswith("-"):
        raise ValueError("expected a non-empty value that does not start with '-'")
    return normalized


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "yes", "true", "on"}:
        return True
    if normalized in {"0", "no", "false", "off"}:
        return False
    raise ValueError("expected true or false")


_COMMON_OPTIONS = {
    "output-res": _Option(_resolution),
    "fps": _Option(_positive_int),
    "bitrate": _Option(_bitrate),
}

_DLNA_CAST_OPTIONS = {
    **_COMMON_OPTIONS,
    "host": _Option(_text),
    "port": _Option(_port),
    "discover-timeout": _Option(_positive_int),
    "transport": _Option(_choice("progressive-ts", "hls", "live-ts")),
    "capture-backend": _Option(_choice("auto", "wf-recorder", "x11grab")),
}

_MODE_OPTIONS = {
    "wfd": {
        **_COMMON_OPTIONS,
        "wfd-test-pattern": _Option(_boolean, is_flag=True),
        "wfd-media-pipeline": _Option(_choice("auto", "ffmpeg", "gst")),
        "wfd-capture-backend": _Option(
            _choice("auto", "portal", "wf-recorder", "x11grab", "gst-x11")
        ),
        "wfd-latency-log": _Option(_text),
        "wfd-no-audio": _Option(_boolean, is_flag=True),
        "wfd-audio-device": _Option(_text),
        "wfd-rtsp-port": _Option(_port),
        "wfd-no-firewall": _Option(_boolean, is_flag=True),
        "wfd-rtp-source-port": _Option(_port),
        "wfd-interface": _Option(_text),
        "wfd-timeout": _Option(_positive_int),
    },
    "dlna": _DLNA_CAST_OPTIONS,
    "cast": _DLNA_CAST_OPTIONS,
}


def get_config_path() -> Path:
    """Return the XDG-compliant tray config path."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "fluxcast" / "config"


def _default_warning(message: str) -> None:
    print(f"[FluxCast tray config] WARNING: {message}", file=sys.stderr)


def load_profile(
    mode: str,
    *,
    config_path: str | os.PathLike[str] | None = None,
    warn: WarningHandler | None = None,
) -> list[str]:
    """Load validated extra CLI arguments for a tray launch in ``mode``.

    A missing file, an empty file, or a missing mode section returns an empty
    list so tray launches retain their built-in defaults.
    """
    if mode not in _MODE_OPTIONS:
        raise ValueError(f"unsupported tray mode: {mode}")

    path = Path(config_path) if config_path is not None else get_config_path()
    warning = warn if warn is not None else _default_warning

    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        warning(f"could not read {path}: {exc}; using built-in defaults")
        return []

    if not contents.strip():
        return []

    # Treat [DEFAULT] like an ordinary, unused section: profiles must not
    # inherit options because the config contract is explicitly per-mode.
    parser = configparser.ConfigParser(
        interpolation=None,
        default_section="__fluxcast_defaults_are_not_supported__",
    )
    try:
        parser.read_string(contents, source=str(path))
    except configparser.Error as exc:
        warning(f"could not parse {path}: {exc}; using built-in defaults")
        return []

    if not parser.has_section(mode):
        return []

    args: list[str] = []
    options = _MODE_OPTIONS[mode]
    for name, raw_value in parser.items(mode):
        option = options.get(name)
        if option is None:
            warning(f"unknown option '{name}' in [{mode}]; ignoring it")
            continue

        try:
            value = option.validate(raw_value)
        except ValueError as exc:
            warning(
                f"invalid value for '{name}' in [{mode}]: {raw_value!r} "
                f"({exc}); using the built-in default"
            )
            continue

        if option.is_flag:
            if value is True:
                args.append(f"--{name}")
        else:
            args.extend((f"--{name}", str(value)))

    return args
