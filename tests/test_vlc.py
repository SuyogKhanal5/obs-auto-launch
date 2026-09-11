import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a


class FindVlcTests(unittest.TestCase):
    def test_finds_via_hklm_registry(self):
        mock_key = MagicMock()
        with patch.object(a.winreg, "OpenKey") as mock_open_key:
            mock_open_key.return_value.__enter__.return_value = mock_key
            with patch.object(a.winreg, "QueryValueEx", return_value=(r"C:\VLC", 1)):
                with patch("os.path.isfile", return_value=True):
                    self.assertEqual(a.find_vlc(), r"C:\VLC")

    def test_falls_back_to_hkcu_when_hklm_fails(self):
        def open_key(hive, subkey):
            if hive == a.winreg.HKEY_LOCAL_MACHINE:
                raise OSError("not found")
            return MagicMock().__enter__()

        with patch.object(a.winreg, "OpenKey", side_effect=open_key):
            with patch.object(a.winreg, "QueryValueEx", return_value=(r"C:\VLC (user)", 1)):
                with patch("os.path.isfile", return_value=True):
                    self.assertEqual(a.find_vlc(), r"C:\VLC (user)")

    def test_stale_registry_entry_without_libvlc_dll_is_rejected(self):
        # The registry key can outlive an uninstall/move -- don't trust it blindly, verify
        # libvlc.dll actually still exists there before reporting it as found.
        mock_key = MagicMock()
        with patch.object(a.winreg, "OpenKey") as mock_open_key:
            mock_open_key.return_value.__enter__.return_value = mock_key
            with patch.object(a.winreg, "QueryValueEx", return_value=(r"C:\Stale\VLC", 1)):
                with patch("os.path.isfile", return_value=False):
                    with patch.dict(os.environ, {}, clear=True):
                        with patch("shutil.which", return_value=None):
                            self.assertIsNone(a.find_vlc())

    def test_falls_back_to_program_files(self):
        program_files = r"C:\Program Files"

        def isfile(path):
            return path == os.path.join(program_files, "VideoLAN", "VLC", "libvlc.dll")

        with patch.object(a.winreg, "OpenKey", side_effect=OSError("not found")):
            with patch.dict(os.environ, {"ProgramFiles": program_files}, clear=True):
                with patch("os.path.isfile", side_effect=isfile):
                    self.assertEqual(a.find_vlc(), os.path.join(program_files, "VideoLAN", "VLC"))

    def test_falls_back_to_path_lookup(self):
        vlc_dir = r"D:\Apps\VLC"

        def isfile(path):
            return path == os.path.join(vlc_dir, "libvlc.dll")

        with patch.object(a.winreg, "OpenKey", side_effect=OSError("not found")):
            with patch.dict(os.environ, {}, clear=True):
                with patch("os.path.isfile", side_effect=isfile):
                    with patch("shutil.which", return_value=os.path.join(vlc_dir, "vlc.exe")):
                        self.assertEqual(a.find_vlc(), vlc_dir)

    def test_returns_none_when_nothing_found(self):
        with patch.object(a.winreg, "OpenKey", side_effect=OSError("not found")):
            with patch.dict(os.environ, {}, clear=True):
                with patch("os.path.isfile", return_value=False):
                    with patch("shutil.which", return_value=None):
                        self.assertIsNone(a.find_vlc())


class WingetInstallVlcTests(unittest.TestCase):
    def test_success_exit_code_reports_success(self):
        with patch.object(a.subprocess, "run") as mock_run:
            mock_run.return_value = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            success, reason = a.winget_install_vlc()
        self.assertTrue(success)
        self.assertIsNone(reason)

    def test_nonzero_exit_code_reports_failure_with_reason(self):
        with patch.object(a.subprocess, "run") as mock_run:
            mock_run.return_value = type(
                "Result", (), {"returncode": 1, "stdout": "", "stderr": "no package found"}
            )()
            success, reason = a.winget_install_vlc()
        self.assertFalse(success)
        self.assertIn("no package found", reason)

    def test_missing_winget_binary_reports_failure_not_raise(self):
        with patch.object(a.subprocess, "run", side_effect=OSError("not found")):
            success, reason = a.winget_install_vlc()  # must not raise
        self.assertFalse(success)
        self.assertIsNotNone(reason)


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
