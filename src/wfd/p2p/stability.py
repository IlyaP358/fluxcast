"""On-demand Wi-Fi chip stability workarounds for WFD / P2P GO.

Selected from live sysfs (driver, bus, module parameters), not interface
names or sink models. Call ``apply_radio_stability(iface)`` when a
dedicated P2P radio is brought up, or ``python3 -m wfd.p2p.stability``.

Does not unload drivers or ``rmmod -f``. Skip with
``FLUXCAST_WFD_NO_RADIO_STABILITY=1``.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..privileged import run_privileged
from .safety import safe_iface, safe_sysfs_write_path

_SYS_CLASS_NET = Path("/sys/class/net")
_SYS_MODULE = Path("/sys/module")


@dataclass(frozen=True)
class RadioFacts:
    """What this phy actually is, from sysfs."""

    iface: str
    driver: str
    bus: str  # usb | pci | unknown
    usb_id: str = ""
    usb_power_path: str = ""
    usb_power_control: str = ""
    module_params: dict[str, str] = field(default_factory=dict)
    module_param_paths: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedWorkaround:
    id: str
    summary: str
    sysfs_path: str
    value: str
    already: bool = False


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _driver_name(dev: Path) -> str:
    link = dev / "driver"
    try:
        return Path(os.path.realpath(link)).name if link.exists() else ""
    except OSError:
        return _read_text(dev / "uevent").split("DRIVER=")[-1].split("\n")[0].strip()


def _usb_device_dir(net_device: Path) -> Optional[Path]:
    """Walk from the net device to the USB device that has idVendor."""
    try:
        p = (net_device / "device").resolve()
    except OSError:
        return None
    for _ in range(8):
        if (p / "idVendor").is_file() and (p / "idProduct").is_file():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _pci_device_dir(net_device: Path) -> Optional[Path]:
    try:
        p = (net_device / "device").resolve()
    except OSError:
        return None
    for _ in range(8):
        if (p / "vendor").is_file() and (p / "device").is_file() and not (p / "idVendor").is_file():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _module_params(
    sys_module: Path, names: tuple[str, ...]
) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    paths: dict[str, str] = {}
    for mod in names:
        pdir = sys_module / mod / "parameters"
        if not pdir.is_dir():
            continue
        try:
            files = list(pdir.iterdir())
        except OSError:
            continue
        for param in files:
            if param.is_file():
                key = f"{mod}.{param.name}"
                values[key] = _read_text(param)
                paths[key] = str(param)
    return values, paths


def inspect_radio(
    iface: str,
    *,
    sys_class_net: Path = _SYS_CLASS_NET,
    sys_module: Path = _SYS_MODULE,
) -> Optional[RadioFacts]:
    """Describe a live iface from sysfs. None if the iface is missing."""
    iface = safe_iface(iface)
    if not iface:
        return None
    net = sys_class_net / iface
    if not net.is_dir():
        return None
    usb = _usb_device_dir(net)
    pci = None if usb else _pci_device_dir(net)
    if usb:
        params, paths = _module_params(
            sys_module,
            ("mt76_usb", "mt76x0u", "mt76x02_usb", "rtw88_usb", "rtl8xxxu"),
        )
        vid = _read_text(usb / "idVendor")
        pid = _read_text(usb / "idProduct")
        pwr = usb / "power" / "control"
        return RadioFacts(
            iface=iface,
            driver=_driver_name(net) or _driver_name(usb / f"{usb.name}:1.0"),
            bus="usb",
            usb_id=f"{vid}:{pid}".lower() if vid and pid else "",
            usb_power_path=str(pwr) if pwr.is_file() else "",
            usb_power_control=_read_text(pwr).lower(),
            module_params=params,
            module_param_paths=paths,
        )
    if pci:
        params, paths = _module_params(sys_module, ("iwlwifi", "mt76"))
        return RadioFacts(
            iface=iface,
            driver=_driver_name(net),
            bus="pci",
            module_params=params,
            module_param_paths=paths,
        )
    return RadioFacts(
        iface=iface,
        driver=_driver_name(net),
        bus="unknown",
    )


def _plan_usb_runtime_pm(facts: RadioFacts) -> Optional[PlannedWorkaround]:
    """USB Wi-Fi: keep the device from runtime-suspending under GO TX."""
    if facts.bus != "usb" or not facts.usb_power_path:
        return None
    already = facts.usb_power_control == "on"
    return PlannedWorkaround(
        id="usb_runtime_pm_on",
        summary="USB Wi-Fi runtime PM off (power/control=on)",
        sysfs_path=facts.usb_power_path,
        value="on",
        already=already,
    )


def _plan_mt76_usb_disable_sg(facts: RadioFacts) -> Optional[PlannedWorkaround]:
    """mt76 USB scatter-gather: known URB/EPROTO stalls in AP/GO TX."""
    if facts.bus != "usb":
        return None
    key = "mt76_usb.disable_usb_sg"
    if key not in facts.module_params:
        return None
    val = facts.module_params[key].strip().upper()
    already = val in ("Y", "1")
    path = facts.module_param_paths.get(key, "")
    if not path:
        return None
    return PlannedWorkaround(
        id="mt76_usb_disable_sg",
        summary="mt76 USB disable scatter-gather (disable_usb_sg=1)",
        sysfs_path=path,
        value="1",
        already=already,
    )


_PLANNERS: tuple[Callable[[RadioFacts], Optional[PlannedWorkaround]], ...] = (
    _plan_usb_runtime_pm,
    _plan_mt76_usb_disable_sg,
)


def plan_radio_stability(facts: RadioFacts) -> list[PlannedWorkaround]:
    """Workarounds that match this radio. Empty if none apply."""
    out: list[PlannedWorkaround] = []
    for planner in _PLANNERS:
        item = planner(facts)
        if item is not None:
            out.append(item)
    return out


def _sysfs_write(path: str, value: str, run) -> None:
    path = safe_sysfs_write_path(path)
    if not path:
        raise ValueError("refusing non-sysfs path")
    if value not in ("on", "auto", "1", "0", "Y", "N"):
        raise ValueError("refusing unexpected sysfs value")
    result = run(
        ["sh", "-c", 'printf "%s\\n" "$1" > "$2"', "fluxcast-stability", value, path],
        timeout=5.0,
    )
    if getattr(result, "returncode", 0) not in (0, None):
        err = (getattr(result, "stderr", None) or getattr(result, "stdout", None) or "")
        raise RuntimeError(str(err).strip() or f"exit {result.returncode}")


def apply_radio_stability(
    iface: str,
    *,
    run=None,
    sys_class_net: Path = _SYS_CLASS_NET,
    sys_module: Path = _SYS_MODULE,
) -> list[str]:
    """Apply matching workarounds. Returns human log lines (possibly empty)."""
    skip = os.environ.get("FLUXCAST_WFD_NO_RADIO_STABILITY", "").strip().lower()
    if skip in ("1", "true", "yes", "on"):
        return ["radio stability skipped (FLUXCAST_WFD_NO_RADIO_STABILITY)"]
    facts = inspect_radio(
        iface, sys_class_net=sys_class_net, sys_module=sys_module
    )
    if facts is None:
        return []
    run_fn = run or run_privileged
    lines: list[str] = []
    ident = facts.driver or "wifi"
    if facts.usb_id:
        ident = f"{ident} {facts.usb_id}"
    ident = f"{ident} {facts.bus}"
    planned = plan_radio_stability(facts)
    if not planned:
        return [f"radio stability: no workarounds for {iface} ({ident})"]
    for item in planned:
        if item.already:
            lines.append(f"radio stability: {item.id} already applied ({item.summary})")
            continue
        try:
            _sysfs_write(item.sysfs_path, item.value, run_fn)
            lines.append(f"radio stability: applied {item.id} ({item.summary})")
        except Exception as exc:
            lines.append(f"radio stability: {item.id} failed ({exc})")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan or apply Wi-Fi chip stability workarounds for an iface."
    )
    parser.add_argument("iface", help="wifi interface (resolved live name)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply matching workarounds (default: print the plan)",
    )
    args = parser.parse_args(argv)
    facts = inspect_radio(args.iface)
    if facts is None:
        print(f"no such iface {args.iface}", file=sys.stderr)
        return 1
    print(
        f"iface={facts.iface} driver={facts.driver or '?'} "
        f"bus={facts.bus} usb_id={facts.usb_id or '-'}"
    )
    if args.apply:
        for line in apply_radio_stability(args.iface):
            print(line)
        return 0
    planned = plan_radio_stability(facts)
    if not planned:
        print("no workarounds")
        return 0
    for item in planned:
        state = "already" if item.already else "needed"
        print(f"{item.id}\t{state}\t{item.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
