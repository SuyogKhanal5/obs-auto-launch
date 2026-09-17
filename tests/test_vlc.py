import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
import platform_common

# a.find_vlc() is now a thin wrapper around platform_common.find_vlc_directory() (see
# CROSS_PLATFORM_PLAN.md Phase 1) -- the real per-OS registry/PATH-lookup logic these tests used
# to exercise directly via a.winreg now lives in platform_windows.py, covered by
# test_platform_windows.py::FindVlcDirectoryTests (which can run on any OS via patch.object(...,
# create=True), unlike a direct a.winreg patch here that only works for real on Windows).


class ResolveVlcPathTests(unittest.TestCase):
    def test_existing_configured_dir_is_used_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, platform_common.libvlc_filename()), "w").close()
            with patch.object(a, "find_vlc") as mock_find_vlc:
                self.assertEqual(a.resolve_vlc_path(tmp), tmp)
            mock_find_vlc.assert_not_called()

    def test_configured_dir_without_libvlc_falls_back_to_find_vlc(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(a, "find_vlc", return_value=r"C:\Fallback\VLC") as mock_find_vlc:
                self.assertEqual(a.resolve_vlc_path(tmp), r"C:\Fallback\VLC")
            mock_find_vlc.assert_called_once()

    def test_blank_configured_path_falls_back_to_find_vlc(self):
        with patch.object(a, "find_vlc", return_value=r"C:\Auto\VLC"):
            self.assertEqual(a.resolve_vlc_path(""), r"C:\Auto\VLC")

    def test_returns_none_when_nothing_resolves(self):
        with patch.object(a, "find_vlc", return_value=None):
            self.assertIsNone(a.resolve_vlc_path(""))


class WingetInstallVlcTests(unittest.TestCase):
    # winget_install_vlc() now dispatches to platform_common.install_optional_dependency("vlc")
    # -- see test_platform_windows.py/test_platform_macos.py for the real winget/Homebrew logic.
    def test_success_exit_code_reports_success(self):
        with patch.object(a.platform_common, "install_optional_dependency", return_value=(True, None)) as mock_install:
            success, reason = a.winget_install_vlc()
        self.assertTrue(success)
        self.assertIsNone(reason)
        mock_install.assert_called_once_with("vlc", timeout=600)

    def test_failure_reports_failure_with_reason(self):
        with patch.object(a.platform_common, "install_optional_dependency", return_value=(False, "no package found")):
            success, reason = a.winget_install_vlc()
        self.assertFalse(success)
        self.assertIn("no package found", reason)


class ImportVlcModuleTests(unittest.TestCase):
    def test_returns_module_on_success(self):
        fake_vlc = MagicMock()
        with patch.dict(sys.modules, {"vlc": fake_vlc}):
            self.assertIs(a.import_vlc_module(), fake_vlc)

    def test_returns_none_and_logs_on_import_failure(self):
        # Simulates python-vlc's real failure mode: an uncaught OSError from its own module-level
        # libvlc discovery when VLC isn't installed at all (not a clean ImportError).
        with patch.dict(sys.modules, {"vlc": None}):
            with self.assertLogs(level="WARNING"):
                result = a.import_vlc_module()  # must not raise
        self.assertIsNone(result)


class CreateVlcInstanceWithLoggingTests(unittest.TestCase):
    def make_fake_vlc_module(self):
        fake_vlc = MagicMock()
        fake_vlc.LogLevel.DEBUG = 0
        fake_vlc.LogLevel.NOTICE = 2
        fake_vlc.LogLevel.WARNING = 3
        fake_vlc.LogLevel.ERROR = 4
        # CallbackDecorators.LogCb needs to behave like a decorator that just returns the
        # function it's applied to, so on_vlc_log stays a plain callable in these tests.
        fake_vlc.CallbackDecorators.LogCb = lambda fn: fn
        return fake_vlc

    def test_appends_quiet_flag_when_missing(self):
        fake_vlc = self.make_fake_vlc_module()
        a.create_vlc_instance_with_logging(fake_vlc)
        args = fake_vlc.Instance.call_args.args
        self.assertIn("--quiet", args)

    def test_does_not_duplicate_quiet_flag(self):
        fake_vlc = self.make_fake_vlc_module()
        a.create_vlc_instance_with_logging(fake_vlc, args=["--quiet"])
        args = fake_vlc.Instance.call_args.args
        self.assertEqual(args.count("--quiet"), 1)

    def test_attaches_log_callback_to_instance(self):
        fake_vlc = self.make_fake_vlc_module()
        instance = a.create_vlc_instance_with_logging(fake_vlc)
        instance.log_set.assert_called_once()

    def test_log_callback_attach_failure_does_not_raise(self):
        fake_vlc = self.make_fake_vlc_module()
        fake_vlc.Instance.return_value.log_set.side_effect = RuntimeError("boom")
        with self.assertLogs(level="WARNING"):
            a.create_vlc_instance_with_logging(fake_vlc)  # must not raise


if __name__ == "__main__":
    unittest.main()
