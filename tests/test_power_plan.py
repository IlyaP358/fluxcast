import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd import power_plan as pp  # noqa: E402


class PowerPlanDiscoveryTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_POWER_PLAN", None)
        os.environ.pop("FLUXCAST_WFD_ENCODE_BIAS", None)
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)

    def test_ppd_dbus_assigns_incrementing_ids(self):
        profiles = (
            "(<[{'Profile': <'power-saver'>}, {'Profile': <'balanced'>}, "
            "{'Profile': <'performance'>}]>,)"
        )
        active = "(<'balanced'>,)"

        def fake_run(cmd, **_kwargs):
            joined = " ".join(cmd)
            if "Profiles" in joined and "ActiveProfile" not in joined:
                return profiles
            if "ActiveProfile" in joined:
                return active
            return None

        with mock.patch.object(pp, "_run", side_effect=fake_run):
            with mock.patch.object(pp.shutil, "which", return_value="/usr/bin/gdbus"):
                with mock.patch.object(pp, "_ppd_cli", return_value=None):
                    with mock.patch.object(pp, "_platform_profile", return_value=None):
                        with mock.patch.object(pp, "_system76_power", return_value=None):
                            with mock.patch.object(pp, "_tuned_adm", return_value=None):
                                # Force dbus path: _ppd_dbus uses _run + which
                                plans, active_name = pp._ppd_dbus()
        self.assertEqual(active_name, "balanced")
        self.assertEqual([p.id for p in plans], ["power_plan_0", "power_plan_1", "power_plan_2"])
        self.assertEqual([p.name for p in plans], ["power-saver", "balanced", "performance"])
        self.assertEqual(plans[0].source, "power-profiles-daemon")

    def test_platform_profile_choices(self):
        def fake_sysfs(path):
            if path.endswith("choices"):
                return "cool quiet balanced performance"
            if path.endswith("platform_profile"):
                return "quiet"
            return None

        with mock.patch.object(pp, "_sysfs_text", side_effect=fake_sysfs):
            plans, active = pp._platform_profile()
        self.assertEqual(active, "quiet")
        self.assertEqual(plans[1].id, "power_plan_1")
        self.assertEqual(plans[1].name, "quiet")
        self.assertTrue(pp._throttle_name("quiet"))

    def test_system76_catalog(self):
        with mock.patch.object(pp.shutil, "which", return_value="/usr/bin/system76-power"):
            with mock.patch.object(pp, "_run", return_value="battery\n"):
                plans, active = pp._system76_power()
        self.assertEqual(active, "battery")
        self.assertEqual([p.name for p in plans], ["performance", "balanced", "battery"])
        self.assertTrue(pp._throttle_name("battery"))

    def test_tuned_adm_parses_list(self):
        blob = (
            "Available profiles:\n"
            "- balanced\n"
            "- desktop\n"
            "- powersave\n"
            "Current active profile: desktop\n"
        )
        with mock.patch.object(pp.shutil, "which", return_value="/usr/bin/tuned-adm"):
            with mock.patch.object(pp, "_run", return_value=blob):
                plans, active = pp._tuned_adm()
        self.assertEqual(active, "desktop")
        self.assertEqual([p.name for p in plans], ["balanced", "desktop", "powersave"])
        self.assertEqual(plans[2].id, "power_plan_2")

    def test_synthetic_fallback(self):
        with mock.patch.object(pp, "_ppd_dbus", return_value=None):
            with mock.patch.object(pp, "_ppd_cli", return_value=None):
                with mock.patch.object(pp, "_platform_profile", return_value=None):
                    with mock.patch.object(pp, "_system76_power", return_value=None):
                        with mock.patch.object(pp, "_tuned_adm", return_value=None):
                            plans, active = pp.discover_power_plans()
        self.assertEqual(active, "default")
        self.assertEqual(plans[0].id, "power_plan_0")
        self.assertEqual(plans[0].source, "synthetic")

    def test_label_format(self):
        plan = pp.PowerPlan(id="power_plan_1", name="balanced", source="test", index=1)
        self.assertEqual(plan.label(), "power_plan_1 (balanced)")

    def test_powerprofilesctl_cli_parses_star_active(self):
        blob = (
            "  power-saver:\n"
            "    CpuDriver:intel_pstate\n"
            "\n"
            "* balanced:\n"
            "    CpuDriver:intel_pstate\n"
            "\n"
            "  performance:\n"
            "    CpuDriver:intel_pstate\n"
        )
        with mock.patch.object(pp.shutil, "which", return_value="/usr/bin/powerprofilesctl"):
            with mock.patch.object(pp, "_run", return_value=blob):
                plans, active = pp._ppd_cli()
        self.assertEqual(active, "balanced")
        self.assertEqual([p.name for p in plans], ["power-saver", "balanced", "performance"])
        self.assertEqual(plans[1].source, "powerprofilesctl")

    def test_throttle_name_heuristics(self):
        for name in ("power-saver", "battery", "cool", "quiet", "low-power", "powersave"):
            self.assertTrue(pp._throttle_name(name), name)
        for name in ("performance", "balanced", "desktop", "default"):
            self.assertFalse(pp._throttle_name(name), name)

    def test_unknown_power_plan_override_falls_back_to_active(self):
        plans = [
            pp.PowerPlan(id="power_plan_0", name="performance", source="test", index=0),
            pp.PowerPlan(id="power_plan_1", name="balanced", source="test", index=1),
        ]
        os.environ["FLUXCAST_WFD_POWER_PLAN"] = "not-a-real-plan"
        with mock.patch.object(pp, "discover_power_plans", return_value=(plans, "balanced")):
            active = pp.active_power_plan()
        self.assertEqual(active.id, "power_plan_1")
        self.assertEqual(active.name, "balanced")

    def test_legacy_encode_bias_forces_throttle_without_gpu(self):
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "efficient"
        plans = [
            pp.PowerPlan(id="power_plan_0", name="performance", source="test", index=0),
        ]
        with mock.patch.object(pp, "discover_power_plans", return_value=(plans, "performance")):
            with mock.patch.object(pp, "on_mains_power", return_value=True):
                self.assertTrue(pp.encode_throttled())
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        with mock.patch.object(pp, "on_mains_power", return_value=False):
            self.assertFalse(pp.encode_throttled())

    def test_system76_battery_plan_throttles_on_ac_when_selected(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "auto"
        os.environ["FLUXCAST_WFD_POWER_PLAN"] = "battery"
        plans = [
            pp.PowerPlan(id="power_plan_0", name="performance", source="system76-power", index=0),
            pp.PowerPlan(id="power_plan_1", name="balanced", source="system76-power", index=1),
            pp.PowerPlan(id="power_plan_2", name="battery", source="system76-power", index=2),
        ]
        with mock.patch.object(pp, "discover_power_plans", return_value=(plans, "performance")):
            with mock.patch.object(pp, "on_mains_power", return_value=True):
                self.assertEqual(pp.active_power_plan().name, "battery")
                self.assertTrue(pp.encode_throttled())
