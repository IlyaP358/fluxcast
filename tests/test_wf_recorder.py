import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wfd.wf_recorder import find_wf_recorder  # noqa: E402


class FindWfRecorderTest(unittest.TestCase):
    @mock.patch("wfd.wf_recorder.shutil.which", return_value=None)
    def test_missing_recorder(self, _which):
        self.assertIsNone(find_wf_recorder())

    @mock.patch("wfd.wf_recorder.subprocess.run")
    @mock.patch("wfd.wf_recorder.shutil.which", return_value="/app/usr/bin/wf-recorder")
    def test_rejects_wrapper_when_system_recorder_is_missing(self, _which, run):
        run.return_value = subprocess.CompletedProcess([], 127, "", "not found")
        self.assertIsNone(find_wf_recorder())

    @mock.patch("wfd.wf_recorder.subprocess.run")
    @mock.patch("wfd.wf_recorder.shutil.which", return_value="/usr/bin/wf-recorder")
    def test_accepts_working_recorder(self, _which, run):
        run.return_value = subprocess.CompletedProcess([], 0, "wf-recorder 0.6.0", "")
        self.assertEqual(find_wf_recorder(), "/usr/bin/wf-recorder")

    @mock.patch("wfd.wf_recorder.subprocess.run")
    @mock.patch("wfd.wf_recorder.shutil.which", return_value="/usr/bin/wf-recorder")
    def test_accepts_recorder_without_version_flag(self, _which, run):
        # Older builds may not support --version; only exit 127 is a hard reject.
        run.return_value = subprocess.CompletedProcess([], 1, "", "unknown option")
        self.assertEqual(find_wf_recorder(), "/usr/bin/wf-recorder")


if __name__ == "__main__":
    unittest.main()
