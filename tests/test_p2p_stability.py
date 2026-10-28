#!/usr/bin/env python3
"""Chip-family P2P/WFD stability workarounds (sysfs facts, not iface names)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.p2p.safety import mac_file_key, safe_iface, safe_ipv4, safe_mac  # noqa: E402
from wfd.p2p.stability import (  # noqa: E402
    RadioFacts,
    apply_radio_stability,
    inspect_radio,
    plan_radio_stability,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SafetyTest(unittest.TestCase):
    def test_iface_rejects_sysctl_all_and_path(self):
        self.assertEqual(safe_iface("p2p-0"), "p2p-0")
        self.assertEqual(safe_iface("wlp0s20f0u3"), "wlp0s20f0u3")
        self.assertEqual(safe_iface("all"), "")
        self.assertEqual(safe_iface("default"), "")
        self.assertEqual(safe_iface("../eth0"), "")
        self.assertEqual(safe_iface("p2p-0;reboot"), "")

    def test_mac_key_rejects_traversal(self):
        self.assertEqual(mac_file_key("AA:BB:CC:DD:EE:FF"), "aa-bb-cc-dd-ee-ff")
        self.assertEqual(mac_file_key("../../etc/passwd"), "")
        self.assertEqual(safe_mac("aa-bb-cc-dd-ee-ff"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(safe_ipv4("192.168.49.1"), "192.168.49.1")
        self.assertEqual(safe_ipv4("999.1.1.1"), "")


class InspectAndPlanTest(unittest.TestCase):
    def test_usb_mt76_plans_runtime_pm_and_sg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            net = root / "class" / "net" / "wlan_usb"
            usb_dev = root / "usb" / "3-3"
            usb_if = usb_dev / "3-3:1.0"
            drv = root / "drivers" / "mt76x0u"
            drv.mkdir(parents=True)
            usb_if.mkdir(parents=True)
            _write(usb_dev / "idVendor", "0e8d")
            _write(usb_dev / "idProduct", "7610")
            _write(usb_dev / "power" / "control", "auto")
            os.symlink(drv, usb_if / "driver")
            net.mkdir(parents=True)
            os.symlink(usb_if, net / "device")
            _write(root / "module" / "mt76_usb" / "parameters" / "disable_usb_sg", "N")

            facts = inspect_radio(
                "wlan_usb", sys_class_net=root / "class" / "net", sys_module=root / "module"
            )
            self.assertIsNotNone(facts)
            assert facts is not None
            self.assertEqual(facts.bus, "usb")
            self.assertEqual(facts.driver, "mt76x0u")
            self.assertEqual(facts.usb_id, "0e8d:7610")
            ids = [p.id for p in plan_radio_stability(facts)]
            self.assertEqual(ids, ["usb_runtime_pm_on", "mt76_usb_disable_sg"])
            self.assertFalse(any(p.already for p in plan_radio_stability(facts)))

    def test_pci_sta_radio_has_no_usb_workarounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            net = root / "class" / "net" / "wlan_pci"
            pci = root / "pci" / "0000:00:14.3"
            drv = root / "drivers" / "iwlwifi"
            drv.mkdir(parents=True)
            pci.mkdir(parents=True)
            _write(pci / "vendor", "0x8086")
            _write(pci / "device", "0x43f0")
            net.mkdir(parents=True)
            os.symlink(pci, net / "device")
            os.symlink(drv, net / "driver")
            facts = inspect_radio(
                "wlan_pci", sys_class_net=root / "class" / "net", sys_module=root / "module"
            )
            self.assertIsNotNone(facts)
            assert facts is not None
            self.assertEqual(facts.bus, "pci")
            self.assertEqual(plan_radio_stability(facts), [])

    def test_missing_iface_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                inspect_radio("nope", sys_class_net=Path(tmp), sys_module=Path(tmp))
            )


class ApplyTest(unittest.TestCase):
    def test_apply_writes_needed_sysfs(self):
        facts = RadioFacts(
            iface="wlan_usb",
            driver="mt76x0u",
            bus="usb",
            usb_id="0e8d:7610",
            usb_power_path="/sys/bus/usb/devices/3-3/power/control",
            usb_power_control="auto",
            module_params={"mt76_usb.disable_usb_sg": "N"},
            module_param_paths={
                "mt76_usb.disable_usb_sg": "/sys/module/mt76_usb/parameters/disable_usb_sg"
            },
        )
        writes: list[tuple[str, str]] = []

        def run(argv, timeout=5.0):  # noqa: ARG001
            # sh -c 'printf...' name value path
            writes.append((argv[-1], argv[-2]))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch(
            "wfd.p2p.stability.inspect_radio", return_value=facts
        ):
            lines = apply_radio_stability("wlan_usb", run=run)
        self.assertEqual([w[1] for w in writes], ["on", "1"])
        self.assertTrue(any(p.endswith("power/control") for p, _ in writes))
        self.assertTrue(any(p.endswith("disable_usb_sg") for p, _ in writes))
        self.assertTrue(all(p.startswith("/sys/") for p, _ in writes))
        self.assertTrue(any("applied usb_runtime_pm_on" in ln for ln in lines))
        self.assertTrue(any("applied mt76_usb_disable_sg" in ln for ln in lines))

    def test_already_applied_is_noop(self):
        facts = RadioFacts(
            iface="wlan_usb",
            driver="mt76x0u",
            bus="usb",
            usb_power_path="/sys/bus/usb/devices/3-3/power/control",
            usb_power_control="on",
            module_params={"mt76_usb.disable_usb_sg": "Y"},
            module_param_paths={
                "mt76_usb.disable_usb_sg": "/sys/module/mt76_usb/parameters/disable_usb_sg"
            },
        )
        run = mock.Mock()
        with mock.patch(
            "wfd.p2p.stability.inspect_radio", return_value=facts
        ):
            lines = apply_radio_stability("wlan_usb", run=run)
        run.assert_not_called()
        self.assertTrue(all("already applied" in ln for ln in lines))

    def test_env_skip(self):
        with mock.patch.dict(os.environ, {"FLUXCAST_WFD_NO_RADIO_STABILITY": "1"}):
            lines = apply_radio_stability("wlan_usb")
        self.assertEqual(len(lines), 1)
        self.assertIn("skipped", lines[0])


if __name__ == "__main__":
    unittest.main()
