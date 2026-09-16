import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_macos as pmac


class FfmpegCandidatesTests(unittest.TestCase):
    def test_includes_both_homebrew_roots_and_macports(self):
        candidates = pmac.ffmpeg_candidates()
        self.assertIn("/opt/homebrew/bin/ffmpeg", candidates)  # Apple Silicon
        self.assertIn("/usr/local/bin/ffmpeg", candidates)  # Intel
        self.assertIn("/opt/local/bin/ffmpeg", candidates)  # MacPorts


class FindObsExecutableTests(unittest.TestCase):
    def test_finds_in_applications(self):
        def isfile(path):
            return path == "/Applications/OBS.app/Contents/MacOS/OBS"

        with unittest.mock.patch.object(pmac.os.path, "isfile", side_effect=isfile):
            self.assertEqual(pmac.find_obs_executable(), "/Applications/OBS.app/Contents/MacOS/OBS")

    def test_finds_in_user_applications(self):
        user_path = os.path.expanduser("~/Applications/OBS.app/Contents/MacOS/OBS")

        def isfile(path):
            return path == user_path

        with unittest.mock.patch.object(pmac.os.path, "isfile", side_effect=isfile):
            self.assertEqual(pmac.find_obs_executable(), user_path)

    def test_returns_none_when_not_installed(self):
        with unittest.mock.patch.object(pmac.os.path, "isfile", return_value=False):
            self.assertIsNone(pmac.find_obs_executable())


class FindVlcDirectoryTests(unittest.TestCase):
    # These two build a multi-segment path (app_dir/Contents/MacOS[/lib]) the same way the real
    # function does -- os.path.join is ambient (ntpath on Windows, posixpath elsewhere), so
    # joining more than one component produces genuinely different separators depending on which
    # real OS is running the test, not just which OS this code is describing. Real verification
    # happens for real on the macOS CI leg (CROSS_PLATFORM_PLAN.md's test.yml matrix); skipped
    # elsewhere rather than asserting a value that's only correct on macOS/Linux.
    @unittest.skipUnless(sys.platform == "darwin", "exercises real macOS multi-segment path joining")
    def test_finds_bundled_lib_subfolder(self):
        expected = "/Applications/VLC.app/Contents/MacOS/lib"

        def isfile(path):
            return path == os.path.join(expected, "libvlc.dylib")

        with unittest.mock.patch.object(pmac.os.path, "isfile", side_effect=isfile):
            self.assertEqual(pmac.find_vlc_directory(), expected)

    @unittest.skipUnless(sys.platform == "darwin", "exercises real macOS multi-segment path joining")
    def test_falls_back_to_dylib_directly_under_macos(self):
        expected = "/Applications/VLC.app/Contents/MacOS"

        def isfile(path):
            return path == os.path.join(expected, "libvlc.dylib")

        with unittest.mock.patch.object(pmac.os.path, "isfile", side_effect=isfile):
            self.assertEqual(pmac.find_vlc_directory(), expected)

    def test_falls_back_to_path_lookup(self):
        with unittest.mock.patch.object(pmac.os.path, "isfile", return_value=False):
            with unittest.mock.patch.object(pmac.shutil, "which", return_value="/usr/local/bin/vlc"):
                self.assertEqual(pmac.find_vlc_directory(), "/usr/local/bin")

    def test_returns_none_when_nothing_found(self):
        with unittest.mock.patch.object(pmac.os.path, "isfile", return_value=False):
            with unittest.mock.patch.object(pmac.shutil, "which", return_value=None):
                with unittest.mock.patch.object(pmac.glob, "glob", return_value=[]):
                    self.assertIsNone(pmac.find_vlc_directory())


class GetWindowTitlesTests(unittest.TestCase):
    def test_missing_pyobjc_returns_empty_not_raises(self):
        # A None entry in sys.modules makes `import Quartz` raise ImportError immediately,
        # regardless of whether pyobjc is actually installed in the environment running this test
        # -- needed because it genuinely IS installed on real macOS CI (a requirements.txt
        # dependency for sys_platform == "darwin"), so this can't rely on a real absence to
        # exercise the ImportError path the way it could when the dependency wasn't installed yet.
        with unittest.mock.patch.dict(sys.modules, {"Quartz": None}):
            with self.assertLogs(level="WARNING"):
                self.assertEqual(pmac.get_window_titles(), {})

    def test_parses_window_list_into_pid_keyed_titles(self):
        # Injects a fake Quartz module into sys.modules -- lets this test exercise
        # get_window_titles' real CGWindowListCopyWindowInfo-parsing logic end to end without
        # pyobjc actually being installed, the same cross-OS-testability spirit as
        # CROSS_PLATFORM_PLAN.md §3.2.
        fake_quartz = unittest.mock.MagicMock()
        fake_quartz.kCGWindowListOptionOnScreenOnly = 1
        fake_quartz.kCGWindowListExcludeDesktopElements = 2
        fake_quartz.kCGNullWindowID = 0
        fake_quartz.CGWindowListCopyWindowInfo.return_value = [
            {"kCGWindowName": "Balatro", "kCGWindowOwnerPID": 4321},
            {"kCGWindowName": "Discord", "kCGWindowOwnerPID": 5555},
            {"kCGWindowOwnerPID": 9999},  # no title -- should be skipped, not raise
            {"kCGWindowName": "Dock"},  # no PID -- should be skipped, not raise
        ]

        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            result = pmac.get_window_titles()

        self.assertEqual(result, {4321: ["Balatro"], 5555: ["Discord"]})

    def test_enumeration_failure_returns_empty_not_raises(self):
        fake_quartz = unittest.mock.MagicMock()
        fake_quartz.CGWindowListCopyWindowInfo.side_effect = RuntimeError("boom")

        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with self.assertLogs(level="WARNING"):
                self.assertEqual(pmac.get_window_titles(), {})


if __name__ == "__main__":
    unittest.main()
