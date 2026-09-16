import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_linux as pl


class FfmpegCandidatesTests(unittest.TestCase):
    def test_includes_common_linux_install_locations(self):
        candidates = pl.ffmpeg_candidates()
        self.assertIn("/usr/bin/ffmpeg", candidates)
        self.assertIn("/usr/local/bin/ffmpeg", candidates)


class FindObsExecutableTests(unittest.TestCase):
    def test_finds_system_flatpak_export(self):
        def isfile(path):
            return path == "/var/lib/flatpak/exports/bin/com.obsproject.Studio"

        with unittest.mock.patch.object(pl.os.path, "isfile", side_effect=isfile):
            self.assertEqual(pl.find_obs_executable(), "/var/lib/flatpak/exports/bin/com.obsproject.Studio")

    def test_finds_user_flatpak_export(self):
        user_path = os.path.expanduser("~/.local/share/flatpak/exports/bin/com.obsproject.Studio")

        def isfile(path):
            return path == user_path

        with unittest.mock.patch.object(pl.os.path, "isfile", side_effect=isfile):
            self.assertEqual(pl.find_obs_executable(), user_path)

    def test_returns_none_when_no_flatpak_export_exists(self):
        # A native-package OBS (plain `obs` on PATH) is handled by platform_common's own
        # shutil.which check before this function is ever called -- this function only needs to
        # cover the Flatpak case, so "nothing found" here correctly means "no Flatpak install",
        # not "OBS isn't installed at all".
        with unittest.mock.patch.object(pl.os.path, "isfile", return_value=False):
            self.assertIsNone(pl.find_obs_executable())


class FindVlcDirectoryTests(unittest.TestCase):
    def test_returns_directory_of_vlc_on_path(self):
        with unittest.mock.patch.object(pl.shutil, "which", return_value="/usr/bin/vlc"):
            self.assertEqual(pl.find_vlc_directory(), "/usr/bin")

    def test_returns_none_when_vlc_not_on_path(self):
        with unittest.mock.patch.object(pl.shutil, "which", return_value=None):
            self.assertIsNone(pl.find_vlc_directory())


if __name__ == "__main__":
    unittest.main()
