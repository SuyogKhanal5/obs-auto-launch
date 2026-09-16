import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_windows as pw

# platform_windows.py only imports the real winreg module when sys.platform == "win32" (see its
# own module docstring for why) -- patch.object(..., create=True) lets these tests inject a fake
# `winreg` attribute onto the module even when the real import never happened, so the actual
# registry-lookup LOGIC in find_obs_executable/find_vlc_directory can be exercised and asserted
# on from any OS running this suite, not just real Windows. This is the concrete mechanism
# CROSS_PLATFORM_PLAN.md §3.2 describes.


class FfmpegCandidatesTests(unittest.TestCase):
    def test_includes_common_windows_install_locations(self):
        candidates = pw.ffmpeg_candidates()
        self.assertIn(r"C:\ffmpeg\bin\ffmpeg.exe", candidates)
        self.assertIn(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe", candidates)

    def test_glob_failure_does_not_raise(self):
        with unittest.mock.patch.object(pw.glob, "glob", side_effect=OSError("boom")):
            candidates = pw.ffmpeg_candidates()  # must not raise
        self.assertTrue(len(candidates) >= 4)


class FindObsExecutableTests(unittest.TestCase):
    def test_finds_via_default_path(self):
        def isfile(path):
            return path == pw.DEFAULT_OBS_PATHS[0]

        with unittest.mock.patch.object(pw.os.path, "isfile", side_effect=isfile):
            self.assertEqual(pw.find_obs_executable(), pw.DEFAULT_OBS_PATHS[0])

    def test_falls_back_to_registry_when_default_paths_missing(self):
        fake_winreg = unittest.mock.MagicMock()
        fake_winreg.HKEY_LOCAL_MACHINE = "HKLM"
        fake_key = unittest.mock.MagicMock()
        fake_winreg.OpenKey.return_value.__enter__.return_value = fake_key
        fake_winreg.QueryValueEx.return_value = (r"C:\OBS Install", 1)

        def isfile(path):
            return path == r"C:\OBS Install\bin\64bit\obs64.exe"

        with unittest.mock.patch.object(pw, "winreg", fake_winreg, create=True):
            with unittest.mock.patch.object(pw.os.path, "isfile", side_effect=isfile):
                self.assertEqual(pw.find_obs_executable(), r"C:\OBS Install\bin\64bit\obs64.exe")

    def test_stale_registry_entry_is_rejected(self):
        fake_winreg = unittest.mock.MagicMock()
        fake_key = unittest.mock.MagicMock()
        fake_winreg.OpenKey.return_value.__enter__.return_value = fake_key
        fake_winreg.QueryValueEx.return_value = (r"C:\Stale\OBS", 1)

        with unittest.mock.patch.object(pw, "winreg", fake_winreg, create=True):
            with unittest.mock.patch.object(pw.os.path, "isfile", return_value=False):
                self.assertIsNone(pw.find_obs_executable())

    def test_returns_none_when_registry_lookup_fails_entirely(self):
        fake_winreg = unittest.mock.MagicMock()
        fake_winreg.OpenKey.side_effect = OSError("not found")

        with unittest.mock.patch.object(pw, "winreg", fake_winreg, create=True):
            with unittest.mock.patch.object(pw.os.path, "isfile", return_value=False):
                self.assertIsNone(pw.find_obs_executable())


class FindVlcDirectoryTests(unittest.TestCase):
    def test_finds_via_registry(self):
        fake_winreg = unittest.mock.MagicMock()
        fake_winreg.HKEY_LOCAL_MACHINE = "HKLM"
        fake_winreg.HKEY_CURRENT_USER = "HKCU"
        fake_key = unittest.mock.MagicMock()
        fake_winreg.OpenKey.return_value.__enter__.return_value = fake_key
        fake_winreg.QueryValueEx.return_value = (r"C:\VLC", 1)

        with unittest.mock.patch.object(pw, "winreg", fake_winreg, create=True):
            with unittest.mock.patch.object(pw.os.path, "isfile", return_value=True):
                self.assertEqual(pw.find_vlc_directory(), r"C:\VLC")

    def test_falls_back_to_path_lookup_when_registry_empty(self):
        fake_winreg = unittest.mock.MagicMock()
        fake_winreg.OpenKey.side_effect = OSError("not found")
        vlc_dir = r"D:\Apps\VLC"

        def isfile(path):
            return path == os.path.join(vlc_dir, "libvlc.dll")

        with unittest.mock.patch.object(pw, "winreg", fake_winreg, create=True):
            with unittest.mock.patch.dict(pw.os.environ, {}, clear=True):
                with unittest.mock.patch.object(pw.os.path, "isfile", side_effect=isfile):
                    with unittest.mock.patch.object(pw.shutil, "which", return_value=os.path.join(vlc_dir, "vlc.exe")):
                        self.assertEqual(pw.find_vlc_directory(), vlc_dir)

    def test_returns_none_when_nothing_found(self):
        fake_winreg = unittest.mock.MagicMock()
        fake_winreg.OpenKey.side_effect = OSError("not found")

        with unittest.mock.patch.object(pw, "winreg", fake_winreg, create=True):
            with unittest.mock.patch.dict(pw.os.environ, {}, clear=True):
                with unittest.mock.patch.object(pw.os.path, "isfile", return_value=False):
                    with unittest.mock.patch.object(pw.shutil, "which", return_value=None):
                        self.assertIsNone(pw.find_vlc_directory())


if __name__ == "__main__":
    unittest.main()
