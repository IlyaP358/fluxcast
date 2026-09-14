"""Discover OS power profiles and map them to stable ``power_plan_N`` ids.

FluxCast does not invent profile *names* — those come from whatever power
stack is active on the machine. Ids are always ``power_plan_0`` …
``power_plan_{n-1}`` in discovery order for the winning backend.

Backends (first that yields at least one named profile wins):

1. power-profiles-daemon D-Bus (``org.freedesktop.UPower.PowerProfiles`` or
   legacy ``net.hadess.PowerProfiles``) — also covers TLP 1.9+ ``tlp-pd``
2. ``powerprofilesctl`` CLI (same daemon, when D-Bus parse fails)
3. ACPI ``/sys/firmware/acpi/platform_profile`` (+ ``_choices``)
4. ``system76-power profile`` (Pop!_OS / System76: performance|balanced|battery)
5. ``tuned-adm`` (RHEL/Fedora tuned profiles)
6. Synthetic ``power_plan_0`` name ``default`` when nothing is available

Encode throttling (former ``efficient`` knobs) is derived from the active
plan's OS name heuristics and/or system battery, not from FluxCast brand
labels. Overrides:

- ``FLUXCAST_WFD_POWER_PLAN=power_plan_N`` or an OS profile name
- ``FLUXCAST_WFD_ENCODE_BIAS=full|efficient`` (legacy alias)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PowerPlan:
    """One discovered (or synthetic) power profile."""

    id: str
    name: str
    source: str
    index: int

    def label(self) -> str:
        return f"{self.id} ({self.name})"


# OS profile names that lean power-saving across common stacks.
_THROTTLE_NAMES = frozenset(
    {
        "power-saver",
        "power_saver",
        "powersave",
        "power-save",
        "power_save",
        "battery",  # system76-power
        "low-power",
        "low_power",
        "lowpower",
        "cool",  # some ACPI platform_profile sets
        "quiet",
        "saver",
        "eco",
        "economy",
    }
)

_GDBUS_PPD = (
    (
        "org.freedesktop.UPower.PowerProfiles",
        "/org/freedesktop/UPower/PowerProfiles",
        "org.freedesktop.UPower.PowerProfiles",
    ),
    (
        "net.hadess.PowerProfiles",
        "/net/hadess/PowerProfiles",
        "net.hadess.PowerProfiles",
    ),
)


def _sysfs_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _is_system_supply(base: str) -> bool:
    """Ignore Device-scoped supplies (HID UPS, mouse, etc.)."""
    scope = _sysfs_text(os.path.join(base, "scope"))
    if scope is None:
        return True
    return scope.lower() == "system"


def on_mains_power() -> bool:
    """True when system AC is online, or no system battery is present."""
    supply = "/sys/class/power_supply"
    try:
        names = os.listdir(supply)
    except OSError:
        return True
    saw_system_battery = False
    for name in names:
        base = os.path.join(supply, name)
        kind = (_sysfs_text(os.path.join(base, "type")) or "").lower()
        if not kind or not _is_system_supply(base):
            continue
        if kind == "mains":
            if _sysfs_text(os.path.join(base, "online")) == "1":
                return True
        elif kind == "battery":
            saw_system_battery = True
            status = (_sysfs_text(os.path.join(base, "status")) or "").lower()
            if status in ("charging", "full"):
                return True
    return not saw_system_battery


def _run(cmd: list[str], timeout: float = 2.0) -> Optional[str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 and not (result.stdout or "").strip():
        return None
    return result.stdout or ""


def _plans_from_names(names: list[str], source: str, active: str) -> tuple[list[PowerPlan], str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    if not cleaned:
        return [], active
    plans = [
        PowerPlan(id=f"power_plan_{i}", name=name, source=source, index=i)
        for i, name in enumerate(cleaned)
    ]
    active_l = (active or "").strip()
    if not active_l:
        active_l = cleaned[0]
    # Preserve discovered spelling when case differs.
    for p in plans:
        if p.name.lower() == active_l.lower():
            active_l = p.name
            break
    return plans, active_l


def _parse_gdbus_profiles(blob: str) -> list[str]:
    # gdbus prints: {'Profile': <'balanced'>, ...}
    return re.findall(r"'Profile':\s*<'([^']+)'>", blob)


def _ppd_dbus() -> Optional[tuple[list[PowerPlan], str]]:
    gdbus = shutil.which("gdbus")
    if not gdbus:
        return None
    for dest, path, iface in _GDBUS_PPD:
        prof = _run(
            [
                gdbus,
                "call",
                "--system",
                "--dest",
                dest,
                "--object-path",
                path,
                "--method",
                "org.freedesktop.DBus.Properties.Get",
                iface,
                "Profiles",
            ]
        )
        if not prof:
            continue
        names = _parse_gdbus_profiles(prof)
        if not names:
            continue
        active_blob = _run(
            [
                gdbus,
                "call",
                "--system",
                "--dest",
                dest,
                "--object-path",
                path,
                "--method",
                "org.freedesktop.DBus.Properties.Get",
                iface,
                "ActiveProfile",
            ]
        ) or ""
        m = re.search(r"<'([^']+)'>", active_blob)
        active = m.group(1) if m else names[0]
        plans, active = _plans_from_names(names, "power-profiles-daemon", active)
        if plans:
            return plans, active
    return None


def _ppd_cli() -> Optional[tuple[list[PowerPlan], str]]:
    ctl = shutil.which("powerprofilesctl")
    if not ctl:
        return None
    listed = _run([ctl, "list"])
    if listed is None:
        return None
    names: list[str] = []
    active = ""
    for line in listed.splitlines():
        m = re.match(r"^(\*?)\s*([^\s:]+)\s*:\s*$", line)
        if not m:
            continue
        name = m.group(2)
        names.append(name)
        if m.group(1) == "*":
            active = name
    if not names:
        got = _run([ctl, "get"])
        if got is None:
            return None
        active = got.strip()
        if not active:
            return None
        names = [active]
    plans, active = _plans_from_names(names, "powerprofilesctl", active)
    return plans, active


def _platform_profile() -> Optional[tuple[list[PowerPlan], str]]:
    choices = _sysfs_text("/sys/firmware/acpi/platform_profile_choices")
    active = _sysfs_text("/sys/firmware/acpi/platform_profile")
    if not choices:
        return None
    names = choices.split()
    plans, active_name = _plans_from_names(names, "platform_profile", active or "")
    if not plans:
        return None
    return plans, active_name


def _system76_power() -> Optional[tuple[list[PowerPlan], str]]:
    bin_path = shutil.which("system76-power")
    if not bin_path:
        return None
    # Fixed catalog for this tool; active from `profile` with no args.
    catalog = ["performance", "balanced", "battery"]
    got = _run([bin_path, "profile"])
    if got is None:
        return None
    active = (got.strip().splitlines() or [""])[0].strip().lower()
    if active not in catalog:
        # Older builds may print more prose; take last token.
        tok = active.split()[-1] if active else ""
        active = tok if tok in catalog else "balanced"
    plans, active_name = _plans_from_names(catalog, "system76-power", active)
    return plans, active_name


def _tuned_adm() -> Optional[tuple[list[PowerPlan], str]]:
    bin_path = shutil.which("tuned-adm")
    if not bin_path:
        return None
    listed = _run([bin_path, "list"], timeout=3.0)
    if listed is None:
        return None
    names: list[str] = []
    active = ""
    for line in listed.splitlines():
        line = line.strip()
        m = re.match(r"^-\s+(\S+)", line)
        if m:
            names.append(m.group(1))
            continue
        m = re.match(r"^Current active profile:\s*(\S+)", line, re.I)
        if m:
            active = m.group(1)
    if not names:
        got = _run([bin_path, "active"])
        if not got:
            return None
        # "Current active profile: balanced"
        m = re.search(r"profile:\s*(\S+)", got, re.I)
        active = m.group(1) if m else got.strip().split()[-1]
        if not active:
            return None
        names = [active]
    plans, active_name = _plans_from_names(names, "tuned", active)
    return plans, active_name


def _synthetic_default() -> tuple[list[PowerPlan], str]:
    plans, active = _plans_from_names(["default"], "synthetic", "default")
    return plans, active


def discover_power_plans() -> tuple[list[PowerPlan], str]:
    """Return (plans, active_name) from the first working backend."""
    for probe in (
        _ppd_dbus,
        _ppd_cli,
        _platform_profile,
        _system76_power,
        _tuned_adm,
    ):
        found = probe()
        if found and found[0]:
            return found
    return _synthetic_default()


def list_power_plans() -> list[PowerPlan]:
    plans, _active = discover_power_plans()
    return list(plans)


def _throttle_name(name: str) -> bool:
    key = (name or "").strip().lower()
    if key in _THROTTLE_NAMES:
        return True
    # Prefix / suffix matches for vendor variants (e.g. power-saver-quiet).
    for token in ("power-saver", "powersave", "low-power", "battery"):
        if token in key:
            return True
    return False


def _requested_encoder() -> str:
    return os.environ.get("FLUXCAST_WFD_ENCODER", "libx264").strip().lower() or "libx264"


def _gpu_encode_opted_in() -> bool:
    return _requested_encoder() not in ("libx264", "x264", "software", "sw")


def _legacy_bias_override() -> Optional[str]:
    raw = os.environ.get("FLUXCAST_WFD_ENCODE_BIAS", "").strip().lower()
    if raw in ("full", "efficient"):
        return raw
    return None


def _plan_override(plans: list[PowerPlan]) -> Optional[PowerPlan]:
    raw = (os.environ.get("FLUXCAST_WFD_POWER_PLAN", "") or "").strip()
    if not raw:
        return None
    key = raw.lower()
    for p in plans:
        if p.id.lower() == key or p.name.lower() == key:
            return p
    # Allow power_planN without underscore.
    m = re.fullmatch(r"power[_-]?plan[_-]?(\d+)", key)
    if m:
        idx = int(m.group(1))
        for p in plans:
            if p.index == idx:
                return p
    return None


def active_power_plan() -> PowerPlan:
    """Plan selected for encode decisions (env override or OS active)."""
    plans, active_name = discover_power_plans()
    override = _plan_override(plans)
    if override is not None:
        return override
    for p in plans:
        if p.name.lower() == active_name.lower():
            return p
    return plans[0]


def encode_throttled(plan: Optional[PowerPlan] = None) -> bool:
    """True when encode should use the power-saving knob set.

    Automatic battery / saver-profile detection only engages when GPU encode
    was opted in (``FLUXCAST_WFD_ENCODER=vaapi|qsv|auto``) or an explicit
    power-plan / legacy bias override is set. Default libx264 sessions keep
    historical bitrate and presets.
    """
    legacy = _legacy_bias_override()
    if legacy == "efficient":
        return True
    if legacy == "full":
        return False

    plan_override = (os.environ.get("FLUXCAST_WFD_POWER_PLAN", "") or "").strip()
    if not plan_override and not _gpu_encode_opted_in():
        return False

    p = plan or active_power_plan()
    if not on_mains_power():
        return True
    return _throttle_name(p.name)


def power_plan_id(plan: Optional[PowerPlan] = None) -> str:
    """Stable ``power_plan_N`` id for the active (or given) plan."""
    return (plan or active_power_plan()).id
