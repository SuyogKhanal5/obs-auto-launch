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


class IsWaylandSessionTests(unittest.TestCase):
    def test_wayland(self):
        with unittest.mock.patch.dict(pl.os.environ, {"XDG_SESSION_TYPE": "wayland"}, clear=True):
            self.assertTrue(pl.is_wayland_session())

    def test_x11(self):
        with unittest.mock.patch.dict(pl.os.environ, {"XDG_SESSION_TYPE": "x11"}, clear=True):
            self.assertFalse(pl.is_wayland_session())

    def test_unset(self):
        with unittest.mock.patch.dict(pl.os.environ, {}, clear=True):
            self.assertFalse(pl.is_wayland_session())


class GetWindowTitlesTests(unittest.TestCase):
    def test_wayland_session_returns_empty_without_touching_xlib(self):
        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=True):
            self.assertEqual(pl.get_window_titles(), {})

    def test_missing_python_xlib_returns_empty_not_raises(self):
        # python-xlib genuinely isn't installed in this test environment (it's a Linux-only
        # requirements.txt dependency -- see that file's sys_platform marker), so this exercises
        # the real ImportError path, not a simulated one.
        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=False):
            with self.assertLogs(level="WARNING"):
                self.assertEqual(pl.get_window_titles(), {})

    def test_parses_client_list_into_pid_keyed_titles(self):
        # Builds a minimal fake Xlib module tree and injects it into sys.modules -- lets this
        # test exercise get_window_titles' real EWMH-property-parsing logic end to end without
        # python-xlib actually being installed, the same cross-OS-testability spirit as
        # CROSS_PLATFORM_PLAN.md §3.2 (just via sys.modules injection instead of
        # patch.object(..., create=True), since the real code does `import Xlib.display` etc.
        # rather than referencing a pre-existing module attribute).
        fake_xlib = unittest.mock.MagicMock()
        fake_xlib.X.AnyPropertyType = 0
        fake_xlib.error.DisplayError = type("DisplayError", (Exception,), {})
        fake_xlib.error.XError = type("XError", (Exception,), {})

        atoms = {"_NET_CLIENT_LIST": "atom-client-list", "_NET_WM_NAME": "atom-wm-name",
                 "_NET_WM_PID": "atom-wm-pid", "UTF8_STRING": "atom-utf8"}

        fake_display = unittest.mock.MagicMock()
        fake_display.intern_atom.side_effect = lambda name: atoms[name]

        fake_root = unittest.mock.MagicMock()
        fake_display.screen.return_value.root = fake_root
        fake_root.get_full_property.return_value = unittest.mock.MagicMock(value=[101, 102])

        def make_window(title, pid):
            window = unittest.mock.MagicMock()

            def get_full_property(atom, prop_type):
                if atom == atoms["_NET_WM_NAME"]:
                    return unittest.mock.MagicMock(value=title)
                if atom == atoms["_NET_WM_PID"]:
                    return unittest.mock.MagicMock(value=[pid])
                return None

            window.get_full_property.side_effect = get_full_property
            return window

        windows_by_id = {101: make_window(b"Balatro", 4321), 102: make_window("Discord", 5555)}
        fake_display.create_resource_object.side_effect = lambda kind, window_id: windows_by_id[window_id]
        fake_xlib.display.Display.return_value = fake_display

        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=False):
            with unittest.mock.patch.dict(
                sys.modules,
                {"Xlib": fake_xlib, "Xlib.X": fake_xlib.X, "Xlib.display": fake_xlib.display, "Xlib.error": fake_xlib.error},
            ):
                result = pl.get_window_titles()

        self.assertEqual(result, {4321: ["Balatro"], 5555: ["Discord"]})
        fake_display.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
