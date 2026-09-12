import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd import hw_encode  # noqa: E402


def _supply_tree(entries: dict[str, dict[str, str]]) -> tempfile.TemporaryDirectory:
    """Build a fake /sys/class/power_supply tree.

    entries maps supply name -> {type, online?, status?}
    """
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    for name, attrs in entries.items():
        base = root / name
        base.mkdir()
        for key, value in attrs.items():
            (base / key).write_text(f"{value}\n", encoding="utf-8")
    return tmp


class OnMainsPowerTest(unittest.TestCase):
    def _on_mains(self, root) -> bool:
        supply = str(root)
        real_listdir = os.listdir
        real_join = os.path.join

        def fake_listdir(path):
            if path == "/sys/class/power_supply":
                return real_listdir(supply)
            return real_listdir(path)

        def fake_join(a, *rest):
            if a == "/sys/class/power_supply":
                return real_join(supply, *rest)
            return real_join(a, *rest)

        with mock.patch("os.listdir", side_effect=fake_listdir):
            with mock.patch("os.path.join", side_effect=fake_join):
                return hw_encode._on_mains_power()

    def test_mains_online_is_full_power(self):
        with _supply_tree({
            "AC": {"type": "Mains", "online": "1"},
            "BAT0": {"type": "Battery", "status": "Discharging"},
        }) as root:
            self.assertTrue(self._on_mains(root))

    def test_discharging_battery_without_mains_is_not_mains(self):
        with _supply_tree({
            "BAT0": {"type": "Battery", "status": "Discharging"},
        }) as root:
            self.assertFalse(self._on_mains(root))

    def test_charging_battery_counts_as_mains(self):
        with _supply_tree({
            "BAT0": {"type": "Battery", "status": "Charging"},
        }) as root:
            self.assertTrue(self._on_mains(root))

    def test_full_battery_counts_as_mains(self):
        with _supply_tree({
            "BAT0": {"type": "Battery", "status": "Full"},
        }) as root:
            self.assertTrue(self._on_mains(root))

    def test_desktop_with_only_mains_is_mains(self):
        with _supply_tree({
            "AC": {"type": "Mains", "online": "1"},
        }) as root:
            self.assertTrue(self._on_mains(root))

    def test_empty_supply_dir_is_mains(self):
        with _supply_tree({}) as root:
            self.assertTrue(self._on_mains(root))

    def test_missing_supply_dir_is_mains(self):
        missing = Path(tempfile.mkdtemp()) / "missing"
        self.assertTrue(self._on_mains(str(missing)))

    def test_mains_offline_with_discharging_battery_is_not_mains(self):
        with _supply_tree({
            "AC": {"type": "Mains", "online": "0"},
            "BAT0": {"type": "Battery", "status": "Discharging"},
        }) as root:
            self.assertFalse(self._on_mains(root))

    def test_device_scoped_battery_is_ignored(self):
        # HID UPS / peripheral packs must not flip a desktop into "efficient".
        with _supply_tree({
            "hidpp_battery_0": {
                "type": "Battery",
                "scope": "Device",
                "status": "Discharging",
            },
        }) as root:
            self.assertTrue(self._on_mains(root))

    def test_system_scoped_discharging_battery_is_not_mains(self):
        with _supply_tree({
            "BAT0": {
                "type": "Battery",
                "scope": "System",
                "status": "Discharging",
            },
        }) as root:
            self.assertFalse(self._on_mains(root))

    def test_device_battery_does_not_mask_system_ac(self):
        with _supply_tree({
            "AC": {"type": "Mains", "scope": "System", "online": "1"},
            "hidpp_battery_0": {
                "type": "Battery",
                "scope": "Device",
                "status": "Discharging",
            },
        }) as root:
            self.assertTrue(self._on_mains(root))


class PowerBiasTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_ENCODE_BIAS", None)

    def test_env_override_wins(self):
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "efficient"
        with mock.patch.object(hw_encode, "_on_mains_power", return_value=True):
            with mock.patch.object(hw_encode, "_power_profile", return_value="performance"):
                self.assertEqual(hw_encode.power_bias(), "efficient")

        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        with mock.patch.object(hw_encode, "_on_mains_power", return_value=False):
            self.assertEqual(hw_encode.power_bias(), "full")

    def test_default_libx264_ignores_battery(self):
        os.environ.pop("FLUXCAST_WFD_ENCODE_BIAS", None)
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)
        with mock.patch.object(hw_encode, "_on_mains_power", return_value=False):
            with mock.patch.object(hw_encode, "_power_profile", return_value="power-saver"):
                self.assertEqual(hw_encode.power_bias(), "full")

    def test_battery_forces_efficient_when_gpu_opted_in(self):
        os.environ.pop("FLUXCAST_WFD_ENCODE_BIAS", None)
        os.environ["FLUXCAST_WFD_ENCODER"] = "auto"
        with mock.patch.object(hw_encode, "_on_mains_power", return_value=False):
            with mock.patch.object(hw_encode, "_power_profile", return_value="performance"):
                self.assertEqual(hw_encode.power_bias(), "efficient")

    def test_power_saver_profile_on_ac_is_efficient_when_gpu_opted_in(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "vaapi"
        with mock.patch.object(hw_encode, "_on_mains_power", return_value=True):
            with mock.patch.object(hw_encode, "_power_profile", return_value="power-saver"):
                self.assertEqual(hw_encode.power_bias(), "efficient")

    def test_balanced_or_unknown_profile_on_ac_is_full(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "auto"
        with mock.patch.object(hw_encode, "_on_mains_power", return_value=True):
            for profile in ("balanced", "performance", "unknown"):
                with mock.patch.object(hw_encode, "_power_profile", return_value=profile):
                    self.assertEqual(hw_encode.power_bias(), "full", profile)

    def test_missing_powerprofilesctl_is_unknown_not_efficient(self):
        with mock.patch.object(hw_encode.shutil, "which", return_value=None):
            self.assertEqual(hw_encode._power_profile(), "unknown")


class BitrateBiasTest(unittest.TestCase):
    def test_full_bias_unchanged(self):
        self.assertEqual(hw_encode.apply_bitrate_bias("4M", "full"), "4M")

    def test_efficient_trims_megabit(self):
        self.assertEqual(hw_encode.apply_bitrate_bias("4M", "efficient"), "3.6M")

    def test_efficient_trims_kilobit_with_floor(self):
        self.assertEqual(hw_encode.apply_bitrate_bias("600k", "efficient"), "540k")


class ProbeEncoderTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)

    def test_default_request_is_libx264(self):
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)
        self.assertEqual(hw_encode._requested_encoder(), "libx264")

    def test_default_stays_software_even_when_vaapi_exists(self):
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)

        def has(name):
            return name == "h264_vaapi"

        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", side_effect=has):
            with mock.patch("os.path.exists", return_value=True):
                self.assertEqual(hw_encode.probe_encoder(hw_encode._requested_encoder()), "libx264")

    def test_software_request_skips_gpu(self):
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=True):
            self.assertEqual(hw_encode.probe_encoder("libx264"), "libx264")

    def test_auto_falls_back_when_no_gpu_encoder(self):
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=False):
            self.assertEqual(hw_encode.probe_encoder("auto"), "libx264")

    def test_explicit_auto_prefers_vaapi_when_device_exists(self):
        def has(name):
            return name == "h264_vaapi"

        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", side_effect=has):
            with mock.patch.object(hw_encode, "_vaapi_device", return_value="/dev/dri/renderD128"):
                with mock.patch("os.path.exists", return_value=True):
                    self.assertEqual(hw_encode.probe_encoder("auto"), "vaapi")

    def test_explicit_vaapi_falls_back_without_render_node(self):
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=True):
            with mock.patch.object(hw_encode, "_vaapi_device", return_value="/dev/dri/missing"):
                with mock.patch("os.path.exists", return_value=False):
                    self.assertEqual(hw_encode.probe_encoder("vaapi"), "libx264")


class BuildEncodePlanTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)
        os.environ.pop("FLUXCAST_WFD_ENCODE_BIAS", None)

    def test_default_plan_is_historical_libx264(self):
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=True):
            with mock.patch("os.path.exists", return_value=True):
                plan = hw_encode.build_encode_plan(
                    h264_profile="baseline",
                    level="3.1",
                    fps=30,
                    gop=30,
                    bitrate="4M",
                    bufsize="8M",
                    vf_scale=None,
                    output_height=1080,
                )
        self.assertEqual(plan.name, "libx264")
        self.assertEqual(plan.pre_input, [])
        self.assertIn("libx264", plan.video_args)
        self.assertIn("zerolatency", plan.video_args)
        self.assertIn("repeat-headers=1:aud=1", plan.video_args)
        self.assertIn("veryfast", plan.video_args)
        self.assertEqual(plan.vf, ["-vf", "format=yuv420p"])
        self.assertNotIn("no usable GPU encoder", plan.note)

    def test_full_bias_uses_ultrafast_above_1080p(self):
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        plan = hw_encode.build_encode_plan(
            h264_profile="baseline",
            level="5.1",
            fps=30,
            gop=30,
            bitrate="8M",
            bufsize="16M",
            vf_scale=None,
            output_height=1440,
        )
        self.assertIn("ultrafast", plan.video_args)

    def test_efficient_software_uses_ultrafast(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "libx264"
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "efficient"
        plan = hw_encode.build_encode_plan(
            h264_profile="baseline",
            level="3.1",
            fps=30,
            gop=30,
            bitrate="3M",
            bufsize="6M",
            vf_scale=None,
            output_height=720,
        )
        self.assertIn("ultrafast", plan.video_args)

    def test_gpu_fallback_note_mentions_missing_encoder(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "vaapi"
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=False):
            plan = hw_encode.build_encode_plan(
                h264_profile="baseline",
                level="3.1",
                fps=30,
                gop=30,
                bitrate="4M",
                bufsize="8M",
                vf_scale=None,
                output_height=1080,
            )
        self.assertEqual(plan.name, "libx264")
        self.assertIn("no usable GPU encoder", plan.note)


class LevelToIdcTest(unittest.TestCase):
    def test_empty_defaults_to_level_31(self):
        self.assertEqual(hw_encode._level_to_idc(""), "31")
        self.assertEqual(hw_encode._level_to_idc("   "), "31")
        self.assertEqual(hw_encode._level_to_idc(None), "31")  # type: ignore[arg-type]

    def test_dotted_levels_convert(self):
        self.assertEqual(hw_encode._level_to_idc("3.1"), "31")
        self.assertEqual(hw_encode._level_to_idc("4.0"), "40")
        self.assertEqual(hw_encode._level_to_idc("5.1"), "51")

    def test_digits_pass_through(self):
        self.assertEqual(hw_encode._level_to_idc("31"), "31")
        self.assertEqual(hw_encode._level_to_idc("42"), "42")

    def test_malformed_raises(self):
        with self.assertRaises(ValueError):
            hw_encode._level_to_idc("nope")
        with self.assertRaises(ValueError):
            hw_encode._level_to_idc("3.")
        with self.assertRaises(ValueError):
            hw_encode._level_to_idc("3.10")


class VaapiQsvPlanShapeTest(unittest.TestCase):
    """GPU plan argv shape — only reached when encoder is explicitly opted in."""

    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)
        os.environ.pop("FLUXCAST_WFD_ENCODE_BIAS", None)
        os.environ.pop("FLUXCAST_WFD_VAAPI_DEVICE", None)
        os.environ.pop("FLUXCAST_WFD_CAPTURE_ENCODE", None)

    def test_vaapi_plan_uses_device_and_h264_vaapi(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "vaapi"
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        os.environ["FLUXCAST_WFD_VAAPI_DEVICE"] = "/dev/dri/renderD128"
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=True):
            with mock.patch("os.path.exists", return_value=True):
                plan = hw_encode.build_encode_plan(
                    h264_profile="baseline",
                    level="3.1",
                    fps=30,
                    gop=30,
                    bitrate="4M",
                    bufsize="8M",
                    vf_scale=None,
                )
        self.assertEqual(plan.name, "vaapi")
        self.assertEqual(plan.pre_input, ["-vaapi_device", "/dev/dri/renderD128"])
        self.assertIn("h264_vaapi", plan.video_args)
        self.assertIn("hwupload", plan.vf[1])
        self.assertIn("-quality", plan.video_args)
        self.assertEqual(plan.video_args[plan.video_args.index("-quality") + 1], "4")

    def test_vaapi_nv12_skips_format_convert(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "vaapi"
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "efficient"
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=True):
            with mock.patch("os.path.exists", return_value=True):
                plan = hw_encode.build_encode_plan(
                    h264_profile="baseline",
                    level="3.1",
                    fps=30,
                    gop=30,
                    bitrate="3M",
                    bufsize="6M",
                    vf_scale=None,
                    input_pix_fmt="nv12",
                )
        self.assertEqual(plan.vf, ["-vf", "hwupload"])
        self.assertNotIn("format=nv12", plan.vf[1])

    def test_vaapi_efficient_uses_faster_quality_cbr_no_low_power(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "vaapi"
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "efficient"
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=True):
            with mock.patch("os.path.exists", return_value=True):
                plan = hw_encode.build_encode_plan(
                    h264_profile="baseline",
                    level="3.1",
                    fps=30,
                    gop=30,
                    bitrate="3M",
                    bufsize="6M",
                    vf_scale=None,
                )
        self.assertEqual(plan.video_args[plan.video_args.index("-quality") + 1], "5")
        self.assertEqual(plan.video_args[plan.video_args.index("-async_depth") + 1], "2")
        self.assertEqual(plan.video_args[plan.video_args.index("-b:v") + 1], "3M")
        self.assertNotIn("-low_power", plan.video_args)
        self.assertNotIn("CQP", plan.video_args)
        self.assertIn("efficient power bias", plan.note)
        self.assertNotIn("low_power", plan.note)

    def test_qsv_plan_uses_init_hw_device(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "qsv"
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=True):
            plan = hw_encode.build_encode_plan(
                h264_profile="baseline",
                level="3.1",
                fps=30,
                gop=30,
                bitrate="4M",
                bufsize="8M",
                vf_scale=None,
            )
        self.assertEqual(plan.name, "qsv")
        self.assertIn("-init_hw_device", plan.pre_input)
        self.assertIn("h264_qsv", plan.video_args)
        self.assertIn("balanced", plan.video_args)
        self.assertIn("-level", plan.video_args)
        self.assertEqual(plan.video_args[plan.video_args.index("-level") + 1], "31")
        self.assertEqual(
            plan.video_args[plan.video_args.index("-profile:v") + 1], "baseline"
        )

    def test_qsv_maps_constrained_baseline_profile(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "qsv"
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=True):
            plan = hw_encode.build_encode_plan(
                h264_profile="constrained_baseline",
                level="4.0",
                fps=30,
                gop=30,
                bitrate="4M",
                bufsize="8M",
                vf_scale=None,
            )
        self.assertEqual(
            plan.video_args[plan.video_args.index("-profile:v") + 1], "baseline"
        )
        self.assertEqual(plan.video_args[plan.video_args.index("-level") + 1], "40")

    def test_vaapi_falls_back_to_libx264_when_encoder_missing(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "vaapi"
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=False):
            plan = hw_encode.build_encode_plan(
                h264_profile="baseline",
                level="3.1",
                fps=30,
                gop=30,
                bitrate="4M",
                bufsize="8M",
                vf_scale=None,
            )
        self.assertEqual(plan.name, "libx264")

    def test_vaapi_plan_keeps_letterbox_before_hwupload(self):
        os.environ["FLUXCAST_WFD_ENCODER"] = "vaapi"
        os.environ["FLUXCAST_WFD_ENCODE_BIAS"] = "full"
        letterbox = (
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2"
        )
        with mock.patch.object(hw_encode, "_ffmpeg_has_encoder", return_value=True):
            with mock.patch("os.path.exists", return_value=True):
                plan = hw_encode.build_encode_plan(
                    h264_profile="baseline",
                    level="3.1",
                    fps=30,
                    gop=30,
                    bitrate="4M",
                    bufsize="8M",
                    vf_scale=letterbox,
                )
        self.assertEqual(plan.name, "vaapi")
        self.assertEqual(plan.vf[0], "-vf")
        self.assertTrue(plan.vf[1].startswith(letterbox))
        self.assertIn(",format=nv12,hwupload", plan.vf[1])



class CaptureEncodeModeTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FLUXCAST_WFD_ENCODER", None)
        os.environ.pop("FLUXCAST_WFD_CAPTURE_ENCODE", None)
        os.environ.pop("FLUXCAST_WFD_ENCODE_BIAS", None)

    def test_default_capture_encode_is_pipe(self):
        os.environ.pop("FLUXCAST_WFD_CAPTURE_ENCODE", None)
        self.assertEqual(hw_encode.capture_encode_mode(), "pipe")
        self.assertFalse(hw_encode.prefer_wf_recorder_vaapi_dmabuf())

    def test_auto_prefers_dmabuf_when_vaapi_usable_and_gpu_requested(self):
        os.environ["FLUXCAST_WFD_CAPTURE_ENCODE"] = "auto"
        os.environ["FLUXCAST_WFD_ENCODER"] = "auto"
        mon = mock.Mock(spec=["name"])
        mon.name = "HEADLESS-1"
        with mock.patch.object(hw_encode, "_vaapi_usable", return_value=True):
            with mock.patch.object(hw_encode, "_requested_gpu_encode", return_value=True):
                with mock.patch.object(hw_encode, "hypr_monitor_scale", return_value=1.0):
                    self.assertTrue(hw_encode.prefer_wf_recorder_vaapi_dmabuf(mon))

    def test_auto_stays_pipe_without_gpu_request(self):
        os.environ["FLUXCAST_WFD_CAPTURE_ENCODE"] = "auto"
        os.environ["FLUXCAST_WFD_ENCODER"] = "libx264"
        with mock.patch.object(hw_encode, "_vaapi_usable", return_value=True):
            self.assertFalse(hw_encode.prefer_wf_recorder_vaapi_dmabuf())

    def test_vaapi_mode_requires_usable_device(self):
        os.environ["FLUXCAST_WFD_CAPTURE_ENCODE"] = "vaapi"
        with mock.patch.object(hw_encode, "_vaapi_usable", return_value=False):
            self.assertFalse(hw_encode.prefer_wf_recorder_vaapi_dmabuf())
        with mock.patch.object(hw_encode, "_vaapi_usable", return_value=True):
            self.assertTrue(hw_encode.prefer_wf_recorder_vaapi_dmabuf())

    def test_vaapi_quality_bias(self):
        self.assertEqual(hw_encode.vaapi_quality_for_bias("efficient"), "5")
        self.assertEqual(hw_encode.vaapi_quality_for_bias("full"), "4")

    def test_scaled_monitor_skips_dmabuf(self):
        os.environ["FLUXCAST_WFD_CAPTURE_ENCODE"] = "auto"
        os.environ["FLUXCAST_WFD_ENCODER"] = "auto"
        # Monitor NamedTuple has no scale — lookup via hypr_monitor_scale.
        mon = mock.Mock(spec=["name"])
        mon.name = "hotyeah-TV"
        with mock.patch.object(hw_encode, "_vaapi_usable", return_value=True):
            with mock.patch.object(hw_encode, "_requested_gpu_encode", return_value=True):
                with mock.patch.object(hw_encode, "hypr_monitor_scale", return_value=2.0):
                    self.assertFalse(hw_encode.prefer_wf_recorder_vaapi_dmabuf(mon))
                with mock.patch.object(hw_encode, "hypr_monitor_scale", return_value=1.0):
                    self.assertTrue(hw_encode.prefer_wf_recorder_vaapi_dmabuf(mon))

if __name__ == "__main__":
    unittest.main()
