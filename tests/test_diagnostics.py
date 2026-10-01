import inspect
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import diagnostics  # noqa: E402


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FirewallCheckTest(unittest.TestCase):
    def test_run_diagnostics_skips_firewall_probe_when_requested(self):
        with mock.patch("diagnostics._firewall_check") as firewall_check:
            report = diagnostics.run_diagnostics(skip_firewall=True)

        firewall_check.assert_not_called()
        firewall = next(check for check in report.checks if check.name == "firewall")
        self.assertEqual(firewall.status, diagnostics.STATUS_SKIP)

    def test_skips_when_no_firewall_tool(self):
        with mock.patch("diagnostics.shutil.which", return_value=None):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_SKIP)

    def test_ufw_inactive_is_ok(self):
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed("Status: inactive")):
            check = diagnostics._firewall_check()
        self.assertEqual(check.name, "firewall (ufw)")
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_ufw_active_without_port_warns(self):
        status = "Status: active\nTo  Action  From\n22/tcp  ALLOW  Anywhere"
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed(status)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn(f"ufw allow {diagnostics.WFD_RTSP_PORT}/tcp", check.detail)

    def test_ufw_active_with_port_is_ok(self):
        status = f"Status: active\n{diagnostics.WFD_RTSP_PORT}/tcp  ALLOW  Anywhere"
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed(status)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_firewalld_running_without_port_warns(self):
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                mock.patch("diagnostics._firewalld_active", return_value=True), \
                mock.patch("diagnostics._run", return_value=_completed("no", returncode=1)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.name, "firewall (firewalld)")
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn(f"{diagnostics.WFD_RTSP_PORT}/tcp", check.detail)

    def test_firewalld_not_running_is_ok(self):
        # Answered by systemd alone: firewall-cmd never runs, so no Polkit dialog.
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                mock.patch("diagnostics._firewalld_active", return_value=False), \
                mock.patch("diagnostics._run") as run:
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)
        run.assert_not_called()

    def test_firewalld_active_asks_systemd_not_firewall_cmd(self):
        # `firewall-cmd --state` is Polkit-gated on some hosts and times out
        # before the dialog can be answered; `systemctl is-active` is not.
        calls = []

        def fake_run(args, timeout=3.0):
            calls.append(args)
            return _completed("active")

        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                mock.patch("diagnostics._run", side_effect=fake_run):
            self.assertTrue(diagnostics._firewalld_active())
        self.assertEqual(calls, [["systemctl", "is-active", "firewalld"]])

    def test_firewalld_active_is_false_when_unit_inactive(self):
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                mock.patch("diagnostics._run", return_value=_completed("inactive", returncode=3)):
            self.assertIs(diagnostics._firewalld_active(), False)

    def test_firewalld_doctor_reports_each_systemctl_outcome(self):
        # Only a definite answer from systemd may become a definite verdict.
        # "Couldn't ask" (no systemctl, no systemd, hang) must not turn into
        # "not running; port not blocked": firewalld may be up and blocking.
        no_systemd = _completed(
            "", returncode=1,
            stderr="System has not been booted with systemd as init system (PID 1). Can't operate.",
        )
        cases = [
            ("active", _completed("active"), diagnostics.STATUS_OK, "allows port", True),
            ("inactive", _completed("inactive", returncode=3), diagnostics.STATUS_OK, "not running", False),
            ("no systemd", no_systemd, diagnostics.STATUS_WARN, "could not verify", False),
            ("systemctl missing", FileNotFoundError("systemctl"), diagnostics.STATUS_WARN, "could not verify", False),
            ("systemctl hangs", subprocess.TimeoutExpired(cmd="systemctl", timeout=3.0), diagnostics.STATUS_WARN, "could not verify", False),
        ]
        for name, systemctl_reply, status, message, queries_port in cases:
            calls = []

            def fake_run(args, timeout=None):
                calls.append(args)
                if args[0] == "systemctl":
                    if isinstance(systemctl_reply, BaseException):
                        raise systemctl_reply
                    return systemctl_reply
                return _completed("yes")

            with self.subTest(name), \
                    mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                    mock.patch("diagnostics._run", side_effect=fake_run):
                check = diagnostics._firewall_check()
                self.assertEqual(check.status, status)
                self.assertIn(message, check.message)
                self.assertEqual(any(args[0] == "firewall-cmd" for args in calls), queries_port)

    def test_firewalld_doctor_query_keeps_short_budget(self):
        # run_diagnostics() runs at every session start, so the informational
        # query must not sit on a Polkit dialog for the 60 s auth budget; that
        # budget belongs to the session path in wfd/firewall.py only.
        timeouts = []

        def fake_run(args, timeout=None):
            timeouts.append(timeout)
            return _completed("yes")

        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                mock.patch("diagnostics._firewalld_active", return_value=True), \
                mock.patch("diagnostics._run", side_effect=fake_run):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)
        self.assertEqual(timeouts, [3.0])

    def test_firewalld_doctor_query_timeout_is_could_not_verify(self):
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                mock.patch("diagnostics._firewalld_active", return_value=True), \
                mock.patch("diagnostics._run", side_effect=subprocess.TimeoutExpired(cmd="firewall-cmd", timeout=3.0)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("could not verify", check.message)
        self.assertNotIn("closed", check.message)

    def test_firewalld_query_auth_failure_is_not_reported_closed(self):
        # firewalld is running, but the port probe hits an auth error; that is
        # "couldn't verify", not a definitive "port closed".
        err = "Authorization failed."
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/bin/firewall-cmd" if b == "firewall-cmd" else None), \
                mock.patch("diagnostics._firewalld_active", return_value=True), \
                mock.patch("diagnostics._run", return_value=_completed("", returncode=1, stderr=err)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("could not verify", check.message)
        self.assertNotIn("closed", check.message)

    def test_query_timeout_is_handled(self):
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", side_effect=subprocess.TimeoutExpired(cmd="ufw", timeout=3.0)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)

    def test_ufw_port_denied_is_not_ok(self):
        status = f"Status: active\n{diagnostics.WFD_RTSP_PORT}/tcp  DENY  Anywhere"
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed(status)):
            check = diagnostics._firewall_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)

    # `ufw status` refuses to run unprivileged, and --doctor is normally run
    # unprivileged, so this branch decides whether the user hears about port
    # 7236 at all. It used to drop the check entirely, which is how #98
    # happened: nothing in the report, then a session that hangs.
    ROOT_ERR = "ERROR: You need to be root to run this script"

    def _unprivileged_check(self, enabled):
        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: "/usr/sbin/ufw" if b == "ufw" else None), \
                mock.patch("diagnostics._run", return_value=_completed(self.ROOT_ERR)), \
                mock.patch("diagnostics._ufw_enabled", return_value=enabled):
            return diagnostics._ufw_check()

    def test_ufw_without_root_warns_when_enabled(self):
        check = self._unprivileged_check(True)
        self.assertIsNotNone(check)
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn(str(diagnostics.WFD_RTSP_PORT), check.message)
        self.assertIn(f"ufw allow {diagnostics.WFD_RTSP_PORT}/tcp", check.detail)

    def test_ufw_without_root_is_ok_when_disabled(self):
        check = self._unprivileged_check(False)
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_ufw_without_root_warns_when_state_unknown(self):
        check = self._unprivileged_check(None)
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("could not verify", check.message)

    def test_ufw_enabled_reads_the_conf_file(self):
        for text, expected in [
            ("ENABLED=yes\n", True),
            ("ENABLED=no\n", False),
            ("# comment only\n", None),
        ]:
            with mock.patch("builtins.open", mock.mock_open(read_data=text)):
                self.assertIs(diagnostics._ufw_enabled(), expected, text)

    def test_ufw_enabled_is_unknown_when_conf_is_unreadable(self):
        with mock.patch("builtins.open", side_effect=OSError("nope")):
            self.assertIsNone(diagnostics._ufw_enabled())

    def test_returns_worst_case_across_front_ends(self):
        def fake_run(args, timeout=3.0):
            if args[0] == "ufw":
                return _completed("Status: inactive")
            return _completed("no", returncode=1)

        with mock.patch("diagnostics.shutil.which", side_effect=lambda b: f"/usr/bin/{b}" if b in ("ufw", "firewall-cmd") else None), \
                mock.patch("diagnostics._firewalld_active", return_value=True), \
                mock.patch("diagnostics._run", side_effect=fake_run):
            check = diagnostics._firewall_check()
        self.assertEqual(check.name, "firewall (firewalld)")
        self.assertEqual(check.status, diagnostics.STATUS_WARN)


class WpaDbusSetCheckTest(unittest.TestCase):
    def test_permitted_set_is_ok(self):
        with mock.patch("diagnostics.shutil.which", return_value="/usr/bin/gdbus"), \
                mock.patch(
                    "diagnostics._run",
                    return_value=_completed(
                        "Error: GDBus.Error:org.freedesktop.DBus.Error.InvalidArgs: "
                        "No such property",
                        returncode=1,
                    ),
                ):
            check = diagnostics._wpa_dbus_set_check()
        self.assertEqual(check.name, "wpa D-Bus Set")
        self.assertEqual(check.status, diagnostics.STATUS_OK)
        self.assertIn("permitted", check.message)

    def test_access_denied_without_admin_group_warns(self):
        with mock.patch("diagnostics.shutil.which", return_value="/usr/bin/gdbus"), \
                mock.patch("diagnostics._user_admin_groups", return_value=[]), \
                mock.patch(
                    "diagnostics._run",
                    return_value=_completed(
                        "Error: GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: "
                        "Rejected send message",
                        returncode=1,
                    ),
                ):
            check = diagnostics._wpa_dbus_set_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("denied", check.message)
        self.assertIn("wheel", check.detail)
        self.assertIn("sudo", check.detail)

    def test_access_denied_with_admin_group_warns_about_policy(self):
        with mock.patch("diagnostics.shutil.which", return_value="/usr/bin/gdbus"), \
                mock.patch("diagnostics._user_admin_groups", return_value=["wheel"]), \
                mock.patch(
                    "diagnostics._run",
                    return_value=_completed(
                        "Error: GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: "
                        "Rejected send message",
                        returncode=1,
                    ),
                ):
            check = diagnostics._wpa_dbus_set_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("dbus", check.detail.lower())

    def test_missing_gdbus_warns(self):
        with mock.patch("diagnostics.shutil.which", return_value=None):
            check = diagnostics._wpa_dbus_set_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("gdbus", check.message)

    def test_run_diagnostics_includes_set_check(self):
        with mock.patch("diagnostics._wpa_dbus_set_check") as probe, \
                mock.patch("diagnostics._firewall_check"):
            probe.return_value = diagnostics.Check(
                "wpa D-Bus Set", diagnostics.STATUS_OK, "Properties.Set on wpa_supplicant is permitted",
            )
            report = diagnostics.run_diagnostics(skip_firewall=True)
        probe.assert_called_once()
        names = [check.name for check in report.checks]
        self.assertIn("wpa D-Bus Set", names)


class WpaDbusPolicyConfTest(unittest.TestCase):
    def test_shipped_policy_is_scoped_to_admin_groups(self):
        conf_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "meta",
            "zz-dev.fluxcast.wpa-supplicant.conf",
        )
        with open(conf_path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotRegex(text, r'<policy\s+context="default"')
        self.assertIn('group="wheel"', text)
        self.assertIn('group="sudo"', text)
        self.assertIn('send_member="Set"', text)


class SubnetConflictCheckTest(unittest.TestCase):
    def test_skips_when_ip_missing(self):
        with mock.patch("diagnostics.shutil.which", return_value=None):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.name, "P2P subnet")
        self.assertEqual(check.status, diagnostics.STATUS_SKIP)

    def test_no_overlap_is_ok(self):
        payload = json.dumps([
            {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": "wlan0", "addr_info": [{"family": "inet", "local": "192.168.1.20", "prefixlen": 24}]},
        ])
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed(payload)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_overlapping_interface_warns(self):
        payload = json.dumps([
            {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "192.168.49.1", "prefixlen": 24}]},
        ])
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed(payload)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("docker0", check.detail)

    def test_containing_supernet_warns(self):
        # A VPN on 192.168.0.0/16 fully contains the P2P subnet.
        payload = json.dumps([
            {"ifname": "tun0", "addr_info": [{"family": "inet", "local": "192.168.0.5", "prefixlen": 16}]},
        ])
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed(payload)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("tun0", check.detail)

    def test_own_p2p_interface_is_ignored(self):
        # FluxCast's own Wi-Fi Direct interface lives on the subnet by design.
        payload = json.dumps([
            {"ifname": "p2p-dev-wlan0", "addr_info": [{"family": "inet", "local": "192.168.49.1", "prefixlen": 24}]},
        ])
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed(payload)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_OK)

    def test_timeout_is_handled(self):
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", side_effect=subprocess.TimeoutExpired(cmd="ip", timeout=3.0)):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)

    def test_malformed_json_is_handled(self):
        with mock.patch("diagnostics.shutil.which", return_value="/usr/sbin/ip"), \
                mock.patch("diagnostics._run", return_value=_completed("not json")):
            check = diagnostics._subnet_conflict_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)


if __name__ == "__main__":
    unittest.main()


class PortalGstElementsCheckTest(unittest.TestCase):
    """--doctor vouched for 2 of the 12 elements the portal backend enforces, so
    users installed exactly what it named and still could not start a session
    (#129). The list is now shared with the backend preflight."""

    def _check(self, present, no_audio=False, timeout_on=()):
        """Run the check with `present` the set of elements gst-inspect can see."""
        def fake_run(argv, timeout=None):
            element = argv[1]
            if element in timeout_on:
                raise subprocess.TimeoutExpired(argv, timeout)
            return _completed(returncode=0 if element in present else 1)

        with mock.patch("diagnostics.shutil.which", return_value="/usr/bin/gst-inspect-1.0"), \
                mock.patch("diagnostics._run", side_effect=fake_run):
            return diagnostics._portal_gst_elements_check(no_audio=no_audio)

    def _everything(self):
        return (set(diagnostics.PORTAL_GST_VIDEO_ELEMENTS)
                | set(diagnostics.PORTAL_GST_AUDIO_ELEMENTS)
                | {"avenc_aac"})

    def test_reports_ok_and_names_the_chosen_aac_encoder(self):
        check = self._check(self._everything())
        self.assertEqual(check.status, diagnostics.STATUS_OK)
        self.assertIn("13 of 13 present", check.detail)
        self.assertIn("AAC encoder: avenc_aac", check.detail)

    def test_audio_elements_and_the_encoder_drop_out_without_audio(self):
        check = self._check(set(diagnostics.PORTAL_GST_VIDEO_ELEMENTS), no_audio=True)
        self.assertEqual(check.status, diagnostics.STATUS_OK)
        self.assertIn("8 of 8 present", check.detail)
        self.assertNotIn("AAC", check.detail)

    def test_names_every_missing_element_not_just_the_two_it_used_to_know(self):
        present = self._everything() - {"mpegtsmux", "rtpmp2tpay", "videorate"}
        check = self._check(present)
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        for element in ("mpegtsmux", "rtpmp2tpay", "videorate"):
            self.assertIn(element, check.message)
        self.assertIn("missing 3 of 13", check.message)

    def test_install_hint_names_each_package_once(self):
        check = self._check(self._everything() - {"udpsink", "rtpmp2tpay"})
        self.assertIn("rtpmp2tpay, udpsink: install gstreamer1.0-plugins-good",
                      check.detail)
        self.assertEqual(check.detail.count("gstreamer1.0-plugins-good"), 1)

    def test_a_missing_aac_encoder_is_one_requirement_not_four(self):
        check = self._check(self._everything() - {"avenc_aac"})
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("an AAC encoder", check.message)
        self.assertIn("missing 1 of 13", check.message)
        self.assertIn("gstreamer1.0-libav", check.detail)
        for encoder in diagnostics.PORTAL_GST_AAC_ENCODERS:
            self.assertIn(encoder, check.detail)

    def test_the_encoder_reported_is_the_one_the_picker_would_choose(self):
        present = (self._everything() - {"avenc_aac"}) | {"voaacenc", "fdkaacenc"}
        check = self._check(present)
        self.assertIn("AAC encoder: fdkaacenc", check.detail)

    def test_unverifiable_elements_are_not_reported_as_missing(self):
        """A gst-inspect timeout must not tell users to install something they
        may already have."""
        check = self._check(self._everything(), timeout_on={"mpegtsmux"})
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("could not verify", check.message)
        self.assertIn("mpegtsmux", check.message)
        self.assertNotIn("install", check.detail)

    def test_missing_gst_inspect_still_names_the_whole_requirement(self):
        with mock.patch("diagnostics.shutil.which", return_value=None):
            check = diagnostics._portal_gst_elements_check()
        self.assertEqual(check.status, diagnostics.STATUS_WARN)
        self.assertIn("13", check.message)
        self.assertIn("gstreamer1.0-tools", check.detail)

    def test_every_element_has_an_install_hint(self):
        for element in (diagnostics.PORTAL_GST_VIDEO_ELEMENTS
                        + diagnostics.PORTAL_GST_AUDIO_ELEMENTS
                        + diagnostics.PORTAL_GST_AAC_ENCODERS):
            self.assertIn(element, diagnostics._GST_ELEMENT_PACKAGES,
                          f"{element} would report 'unknown package'")

    def test_the_check_is_registered_in_the_report(self):
        with mock.patch("diagnostics._firewall_check"):
            report = diagnostics.run_diagnostics(skip_firewall=True)
        self.assertTrue(any(check.name == "portal gst elements"
                            for check in report.checks))


class PortalPreflightSharesTheDoctorListTest(unittest.TestCase):
    """The report and the backend requirement drifted apart once (#129); this
    fails if either grows its own copy of the list again."""

    def test_portal_preflight_uses_the_shared_constants(self):
        """Scoped to the preflight: the pipeline argv further down the same
        function names elements legitimately."""
        from wfd.media import portal
        source = inspect.getsource(portal.PortalMixin._start_desktop_portal)
        self.assertIn("required = PORTAL_GST_VIDEO_ELEMENTS", source)
        self.assertIn("required += PORTAL_GST_AUDIO_ELEMENTS", source)
        self.assertNotIn("required = (", source,
                         "preflight is building its own element tuple again")

    def test_the_backend_enforces_exactly_what_the_doctor_checks(self):
        """Both sides resolve to the same set, whatever the tuples contain."""
        from wfd.media import portal
        source = inspect.getsource(portal.PortalMixin._start_desktop_portal)
        preflight = source.split("monitor = self.config.monitor")[0]
        for element in (diagnostics.PORTAL_GST_VIDEO_ELEMENTS
                        + diagnostics.PORTAL_GST_AUDIO_ELEMENTS):
            self.assertNotIn(f'"{element}"', preflight,
                             f"{element} is hardcoded in the preflight")

    def test_aac_picker_uses_the_shared_order(self):
        from wfd import gst
        source = inspect.getsource(gst._gst_pick_aac_encoder)
        self.assertIn("PORTAL_GST_AAC_ENCODERS", source)
        self.assertEqual(set(gst._AAC_ENCODER_CAPS),
                         set(diagnostics.PORTAL_GST_AAC_ENCODERS))
