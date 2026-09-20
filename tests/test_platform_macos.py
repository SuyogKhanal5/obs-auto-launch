import os
import plistlib
import shutil
import sys
import tempfile
import threading
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


class InstallOptionalDependencyTests(unittest.TestCase):
    def test_has_package_manager_true_when_brew_present(self):
        with unittest.mock.patch.object(pmac.shutil, "which", return_value="/opt/homebrew/bin/brew"):
            self.assertTrue(pmac.has_package_manager())

    def test_has_package_manager_false_when_brew_missing(self):
        with unittest.mock.patch.object(pmac.shutil, "which", return_value=None):
            self.assertFalse(pmac.has_package_manager())

    def test_unknown_package_returns_false_without_running_anything(self):
        with unittest.mock.patch.object(pmac.subprocess, "run") as mock_run:
            success, reason = pmac.install_optional_dependency("notarealpackage")
        self.assertFalse(success)
        self.assertIn("notarealpackage", reason)
        mock_run.assert_not_called()

    def test_formula_package_installs_without_cask_flag(self):
        with unittest.mock.patch.object(pmac.subprocess, "run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
            success, reason = pmac.install_optional_dependency("ffmpeg")
        self.assertTrue(success)
        self.assertIsNone(reason)
        mock_run.assert_called_once_with(["brew", "install", "ffmpeg"], capture_output=True, text=True, timeout=600)

    def test_cask_package_installs_with_cask_flag(self):
        with unittest.mock.patch.object(pmac.subprocess, "run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
            pmac.install_optional_dependency("obs")
        mock_run.assert_called_once_with(
            ["brew", "install", "--cask", "obs"], capture_output=True, text=True, timeout=600
        )

    def test_nonzero_exit_code_reports_failure_with_reason(self):
        with unittest.mock.patch.object(pmac.subprocess, "run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=1, stdout="", stderr="no formula found")
            success, reason = pmac.install_optional_dependency("vlc")
        self.assertFalse(success)
        self.assertIn("no formula found", reason)

    def test_missing_brew_binary_reports_failure_not_raise(self):
        with unittest.mock.patch.object(pmac.subprocess, "run", side_effect=OSError("not found")):
            success, reason = pmac.install_optional_dependency("obs")  # must not raise
        self.assertFalse(success)
        self.assertIsNotNone(reason)


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp_home = tempfile.mkdtemp(prefix="obsautorec_test_home_")
        self.addCleanup(shutil.rmtree, self.tmp_home, ignore_errors=True)
        self.home_patcher = unittest.mock.patch.dict(os.environ, {"HOME": self.tmp_home}, clear=False)
        self.home_patcher.start()
        self.addCleanup(self.home_patcher.stop)

    def test_disabled_by_default(self):
        self.assertFalse(pmac.is_autostart_enabled())

    def test_enable_then_disable_round_trips(self):
        with unittest.mock.patch.object(pmac.subprocess, "run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=0)
            self.assertTrue(pmac.enable_autostart("/Applications/OBSAutoRecorder.app/Contents/MacOS/OBSAutoRecorder", "/Applications"))
        self.assertTrue(pmac.is_autostart_enabled())
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[0][0][0], "launchctl")
        self.assertEqual(mock_run.call_args[0][0][1], "load")

        plist_path = pmac._launch_agent_plist_path()
        with open(plist_path, "rb") as f:
            import plistlib
            plist = plistlib.load(f)
        self.assertEqual(plist["Label"], pmac._LAUNCH_AGENT_LABEL)
        self.assertTrue(plist["RunAtLoad"])

        with unittest.mock.patch.object(pmac.subprocess, "run") as mock_run2:
            mock_run2.return_value = unittest.mock.Mock(returncode=0)
            self.assertTrue(pmac.disable_autostart())
        self.assertFalse(pmac.is_autostart_enabled())

    def test_disable_when_never_enabled_is_a_no_op(self):
        self.assertTrue(pmac.disable_autostart())

    def test_enable_failure_to_write_plist_returns_false(self):
        with unittest.mock.patch.object(pmac.os, "makedirs", side_effect=OSError("boom")):
            self.assertFalse(pmac.enable_autostart("/Applications/App.app/Contents/MacOS/App", "/Applications"))

    def test_launchctl_unload_failure_still_removes_plist(self):
        with unittest.mock.patch.object(pmac.subprocess, "run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=0)
            pmac.enable_autostart("/Applications/App.app/Contents/MacOS/App", "/Applications")

        with unittest.mock.patch.object(pmac.subprocess, "run", side_effect=OSError("launchctl missing")):
            with self.assertLogs(level="WARNING"):
                self.assertTrue(pmac.disable_autostart())
        self.assertFalse(pmac.is_autostart_enabled())


class FindLibtkDylibTests(unittest.TestCase):
    # Real filesystem, real temp dirs -- this is pure path-searching logic with no macOS-specific
    # API involved, so (like ResolveBundleIdentifierTests) it's exercisable on any real OS.
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.lib_name = f"libtk{pmac.tkinter.TkVersion}.dylib"

    def test_finds_a_dylib_under_sys_prefix(self):
        lib_dir = os.path.join(self.tmp_dir, "lib")
        os.makedirs(lib_dir)
        lib_path = os.path.join(lib_dir, self.lib_name)
        open(lib_path, "w").close()

        with unittest.mock.patch.object(pmac.sys, "prefix", self.tmp_dir), \
             unittest.mock.patch.object(pmac.sys, "base_prefix", self.tmp_dir), \
             unittest.mock.patch.object(pmac.ctypes.util, "find_library", return_value=None):
            self.assertEqual(pmac._find_libtk_dylib(), lib_path)

    def test_finds_a_dylib_via_ctypes_find_library(self):
        lib_path = os.path.join(self.tmp_dir, self.lib_name)
        open(lib_path, "w").close()

        with unittest.mock.patch.object(pmac.sys, "prefix", "/nonexistent"), \
             unittest.mock.patch.object(pmac.sys, "base_prefix", "/nonexistent"), \
             unittest.mock.patch.object(pmac.ctypes.util, "find_library", return_value=lib_path):
            self.assertEqual(pmac._find_libtk_dylib(), lib_path)

    def test_returns_none_when_not_found_anywhere(self):
        with unittest.mock.patch.object(pmac.sys, "prefix", "/nonexistent"), \
             unittest.mock.patch.object(pmac.sys, "base_prefix", "/nonexistent"), \
             unittest.mock.patch.object(pmac.ctypes.util, "find_library", return_value=None), \
             unittest.mock.patch.object(pmac.glob, "glob", return_value=[]):
            self.assertIsNone(pmac._find_libtk_dylib())


class EmbedVideoPlayerTests(unittest.TestCase):
    # Confirmed live with a minimal reproduction script: winfo_id()'s raw value is NOT a real
    # NSView pointer (wrapping it via objc.objc_object, this function's old behavior, segfaulted
    # the instant anything tried to use it as one) -- the real, confirmed-correct conversion is
    # Tk's own TkMacOSXGetRootControl C function, called via ctypes, per python-vlc's own
    # official tkvlc.py example. No PyObjC/objc import is involved in the fixed version at all.
    def test_calls_set_nsobject_with_the_resolved_ns_view(self):
        player = unittest.mock.Mock()
        widget = unittest.mock.Mock()
        widget.winfo_id.return_value = 4242
        fake_lib = unittest.mock.Mock()
        fake_lib.TkMacOSXGetRootControl.return_value = 999999

        with unittest.mock.patch.object(pmac, "_find_libtk_dylib", return_value="/fake/libtk8.6.dylib"), \
             unittest.mock.patch.object(pmac.ctypes, "cdll") as mock_cdll:
            mock_cdll.LoadLibrary.return_value = fake_lib
            pmac.embed_video_player(player, widget)

        mock_cdll.LoadLibrary.assert_called_once_with("/fake/libtk8.6.dylib")
        fake_lib.TkMacOSXGetRootControl.assert_called_once_with(4242)
        player.set_nsobject.assert_called_once_with(999999)
        player.set_xwindow.assert_not_called()

    def test_falls_back_to_set_xwindow_when_libtk_cannot_be_found(self):
        player = unittest.mock.Mock()
        widget = unittest.mock.Mock()
        widget.winfo_id.return_value = 4242

        with unittest.mock.patch.object(pmac, "_find_libtk_dylib", return_value=None):
            with self.assertLogs(level="WARNING"):
                pmac.embed_video_player(player, widget)

        player.set_xwindow.assert_called_once_with(4242)
        player.set_nsobject.assert_not_called()

    def test_falls_back_to_set_xwindow_when_loading_libtk_fails(self):
        player = unittest.mock.Mock()
        widget = unittest.mock.Mock()
        widget.winfo_id.return_value = 4242

        with unittest.mock.patch.object(pmac, "_find_libtk_dylib", return_value="/fake/libtk8.6.dylib"), \
             unittest.mock.patch.object(pmac.ctypes, "cdll") as mock_cdll:
            mock_cdll.LoadLibrary.side_effect = OSError("boom")
            with self.assertLogs(level="WARNING"):
                pmac.embed_video_player(player, widget)

        player.set_xwindow.assert_called_once_with(4242)
        player.set_nsobject.assert_not_called()

    def test_falls_back_to_set_xwindow_when_ns_view_is_null(self):
        player = unittest.mock.Mock()
        widget = unittest.mock.Mock()
        widget.winfo_id.return_value = 4242
        fake_lib = unittest.mock.Mock()
        fake_lib.TkMacOSXGetRootControl.return_value = 0

        with unittest.mock.patch.object(pmac, "_find_libtk_dylib", return_value="/fake/libtk8.6.dylib"), \
             unittest.mock.patch.object(pmac.ctypes, "cdll") as mock_cdll:
            mock_cdll.LoadLibrary.return_value = fake_lib
            pmac.embed_video_player(player, widget)

        player.set_xwindow.assert_called_once_with(4242)
        player.set_nsobject.assert_not_called()


def make_fake_quartz(accessibility_trusted=True, tap_creation_succeeds=True):
    fake = unittest.mock.MagicMock()
    fake.kCGEventFlagMaskControl = 0x40000
    fake.kCGEventFlagMaskAlternate = 0x80000
    fake.kCGEventFlagMaskShift = 0x20000
    fake.kCGEventFlagMaskCommand = 0x100000
    fake.kCGSessionEventTap = 1
    fake.kCGHeadInsertEventTap = 0
    fake.kCGEventTapOptionListenOnly = 1
    fake.kCGEventKeyDown = 10
    fake.kCGKeyboardEventKeycode = 9
    fake.kCFRunLoopCommonModes = "kCFRunLoopCommonModes"
    fake.kAXTrustedCheckOptionPrompt = "AXTrustedCheckOptionPrompt"
    fake.AXIsProcessTrusted.return_value = accessibility_trusted
    fake.CGEventMaskBit.side_effect = lambda event_type: 1 << event_type
    fake.CGEventTapCreate.return_value = object() if tap_creation_succeeds else None
    fake.CFRunLoopGetCurrent.return_value = "run_loop"
    return fake


class MacosKeycodeForKeyTests(unittest.TestCase):
    def test_letter_and_digit_keys(self):
        self.assertEqual(pmac.macos_keycode_for_key("a"), 0x00)
        self.assertEqual(pmac.macos_keycode_for_key("S"), 0x01)
        self.assertEqual(pmac.macos_keycode_for_key("5"), 0x17)

    def test_function_keys(self):
        self.assertEqual(pmac.macos_keycode_for_key("F1"), 0x7A)
        self.assertEqual(pmac.macos_keycode_for_key("F12"), 0x6F)

    def test_invalid_key_returns_none(self):
        self.assertIsNone(pmac.macos_keycode_for_key("F13"))
        self.assertIsNone(pmac.macos_keycode_for_key("Enter"))
        self.assertIsNone(pmac.macos_keycode_for_key(""))
        self.assertIsNone(pmac.macos_keycode_for_key(None))


class AccessibilityPermissionGrantedTests(unittest.TestCase):
    def test_missing_pyobjc_returns_false(self):
        with unittest.mock.patch.dict(sys.modules, {"Quartz": None}):
            self.assertFalse(pmac.accessibility_permission_granted())

    def test_granted(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=True)
        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            self.assertTrue(pmac.accessibility_permission_granted())

    def test_not_granted(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=False)
        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            self.assertFalse(pmac.accessibility_permission_granted())

    def test_exception_returns_false_not_raised(self):
        fake_quartz = make_fake_quartz()
        fake_quartz.AXIsProcessTrusted.side_effect = RuntimeError("boom")
        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with self.assertLogs(level="ERROR"):
                self.assertFalse(pmac.accessibility_permission_granted())


class RequestAccessibilityPermissionTests(unittest.TestCase):
    def test_missing_pyobjc_is_a_no_op(self):
        with unittest.mock.patch.dict(sys.modules, {"Quartz": None}):
            pmac.request_accessibility_permission()  # must not raise

    def test_prompts_via_ax_is_process_trusted_with_options(self):
        fake_quartz = make_fake_quartz()
        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            pmac.request_accessibility_permission()
        fake_quartz.AXIsProcessTrustedWithOptions.assert_called_once_with(
            {fake_quartz.kAXTrustedCheckOptionPrompt: True}
        )

    def test_exception_is_swallowed(self):
        fake_quartz = make_fake_quartz()
        fake_quartz.AXIsProcessTrustedWithOptions.side_effect = RuntimeError("boom")
        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with self.assertLogs(level="ERROR"):
                pmac.request_accessibility_permission()  # must not raise


class RunCustomKeybindListenerTests(unittest.TestCase):
    def test_missing_pyobjc_is_logged_not_raised(self):
        with unittest.mock.patch.dict(sys.modules, {"Quartz": None}):
            with self.assertLogs(level="WARNING"):
                pmac.run_custom_keybind_listener(
                    [{"enabled": True, "key": "S", "modifiers": []}], get_client=lambda: None,
                    get_manual_split_buffer_seconds=lambda: 0, fire_keybind=unittest.mock.Mock(),
                    describe_keybind=unittest.mock.Mock(), notify=unittest.mock.Mock(),
                )  # must not raise

    def test_permission_not_granted_requests_and_notifies(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=False)
        notify = unittest.mock.Mock()
        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with self.assertLogs(level="WARNING"):
                pmac.run_custom_keybind_listener(
                    [{"enabled": True, "key": "S", "modifiers": []}], get_client=lambda: None,
                    get_manual_split_buffer_seconds=lambda: 0, fire_keybind=unittest.mock.Mock(),
                    describe_keybind=unittest.mock.Mock(), notify=notify,
                )
        fake_quartz.AXIsProcessTrustedWithOptions.assert_called_once()
        notify.assert_called_once()
        fake_quartz.CGEventTapCreate.assert_not_called()

    def test_invalid_key_is_skipped_and_no_tap_is_created(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=True)
        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with self.assertLogs(level="WARNING"):
                pmac.run_custom_keybind_listener(
                    [{"enabled": True, "key": "NotAKey", "modifiers": []}], get_client=lambda: None,
                    get_manual_split_buffer_seconds=lambda: 0, fire_keybind=unittest.mock.Mock(),
                    describe_keybind=unittest.mock.Mock(), notify=unittest.mock.Mock(),
                )
        fake_quartz.CGEventTapCreate.assert_not_called()

    def test_fires_matching_binding_on_a_dedicated_thread(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=True)
        binding = {"enabled": True, "key": "S", "modifiers": ["ctrl"], "action": "save_replay_buffer"}
        fire_keybind = unittest.mock.Mock()
        fake_thread = unittest.mock.MagicMock()

        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with unittest.mock.patch.object(pmac.threading, "Thread", return_value=fake_thread) as mock_thread_cls:
                pmac.run_custom_keybind_listener(
                    [binding], get_client="get_client", get_manual_split_buffer_seconds="get_seconds",
                    fire_keybind=fire_keybind, describe_keybind=unittest.mock.Mock(return_value="Ctrl+S"),
                    notify=unittest.mock.Mock(), icon="icon", notifications_config="notif_cfg", status="status",
                )

                fake_quartz.CGEventTapCreate.assert_called_once()
                tap_callback = fake_quartz.CGEventTapCreate.call_args[0][4]

                # threading.Thread must still be the patched mock (not the real class) when this
                # fires -- both patches need to stay active across the callback invocation, not
                # just across run_custom_keybind_listener's own setup.
                fake_event = unittest.mock.Mock()
                fake_quartz.CGEventGetIntegerValueField.return_value = pmac.macos_keycode_for_key("S")
                fake_quartz.CGEventGetFlags.return_value = fake_quartz.kCGEventFlagMaskControl
                result = tap_callback(None, fake_quartz.kCGEventKeyDown, fake_event, None)

                self.assertIs(result, fake_event)
                mock_thread_cls.assert_called_once_with(
                    target=fire_keybind,
                    args=(binding, "get_client", "get_seconds", "icon", "notif_cfg", "status"),
                    daemon=True,
                )
                fake_thread.start.assert_called_once()
        fake_quartz.CFRunLoopRun.assert_called_once()

    def test_non_matching_key_press_does_not_fire(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=True)
        binding = {"enabled": True, "key": "S", "modifiers": ["ctrl"], "action": "save_replay_buffer"}
        fire_keybind = unittest.mock.Mock()

        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with unittest.mock.patch.object(pmac.threading, "Thread") as mock_thread_cls:
                pmac.run_custom_keybind_listener(
                    [binding], get_client=lambda: None, get_manual_split_buffer_seconds=lambda: 0,
                    fire_keybind=fire_keybind, describe_keybind=unittest.mock.Mock(), notify=unittest.mock.Mock(),
                )

                tap_callback = fake_quartz.CGEventTapCreate.call_args[0][4]
                fake_quartz.CGEventGetIntegerValueField.return_value = pmac.macos_keycode_for_key("Q")
                fake_quartz.CGEventGetFlags.return_value = fake_quartz.kCGEventFlagMaskControl
                tap_callback(None, fake_quartz.kCGEventKeyDown, unittest.mock.Mock(), None)

                mock_thread_cls.assert_not_called()

    def test_tap_creation_failure_is_logged_not_raised(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=True, tap_creation_succeeds=False)
        binding = {"enabled": True, "key": "S", "modifiers": []}
        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with self.assertLogs(level="WARNING"):
                pmac.run_custom_keybind_listener(
                    [binding], get_client=lambda: None, get_manual_split_buffer_seconds=lambda: 0,
                    fire_keybind=unittest.mock.Mock(), describe_keybind=unittest.mock.Mock(),
                    notify=unittest.mock.Mock(),
                )  # must not raise
        fake_quartz.CFRunLoopRun.assert_not_called()


class RunClipEditorSpaceBarListenerTests(unittest.TestCase):
    def test_missing_pyobjc_is_logged_not_raised(self):
        with unittest.mock.patch.dict(sys.modules, {"Quartz": None}):
            with self.assertLogs(level="WARNING"):
                pmac.run_clip_editor_space_bar_listener(
                    None, unittest.mock.Mock(), unittest.mock.Mock(),
                )  # must not raise

    def test_permission_not_granted_is_logged_not_raised(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=False)
        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with self.assertLogs(level="WARNING"):
                pmac.run_clip_editor_space_bar_listener(
                    None, unittest.mock.Mock(), unittest.mock.Mock(),
                )
        fake_quartz.CGEventTapCreate.assert_not_called()

    def test_toggles_when_this_process_is_frontmost(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=True)
        fake_frontmost_app = unittest.mock.Mock()
        fake_frontmost_app.processIdentifier.return_value = os.getpid()
        fake_quartz.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = fake_frontmost_app
        on_toggle = unittest.mock.Mock()

        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            pmac.run_clip_editor_space_bar_listener(None, on_toggle, unittest.mock.Mock())

        tap_callback = fake_quartz.CGEventTapCreate.call_args[0][4]
        fake_quartz.CGEventGetIntegerValueField.return_value = pmac.MACOS_SPACE_KEYCODE
        tap_callback(None, fake_quartz.kCGEventKeyDown, unittest.mock.Mock(), None)

        on_toggle.assert_called_once()

    def test_does_not_toggle_when_a_different_process_is_frontmost(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=True)
        fake_frontmost_app = unittest.mock.Mock()
        fake_frontmost_app.processIdentifier.return_value = os.getpid() + 1
        fake_quartz.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = fake_frontmost_app
        on_toggle = unittest.mock.Mock()

        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            pmac.run_clip_editor_space_bar_listener(None, on_toggle, unittest.mock.Mock())

        tap_callback = fake_quartz.CGEventTapCreate.call_args[0][4]
        fake_quartz.CGEventGetIntegerValueField.return_value = pmac.MACOS_SPACE_KEYCODE
        tap_callback(None, fake_quartz.kCGEventKeyDown, unittest.mock.Mock(), None)

        on_toggle.assert_not_called()

    def test_non_space_key_does_not_toggle(self):
        fake_quartz = make_fake_quartz(accessibility_trusted=True)
        on_toggle = unittest.mock.Mock()

        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            pmac.run_clip_editor_space_bar_listener(None, on_toggle, unittest.mock.Mock())

        tap_callback = fake_quartz.CGEventTapCreate.call_args[0][4]
        fake_quartz.CGEventGetIntegerValueField.return_value = pmac.macos_keycode_for_key("A")
        tap_callback(None, fake_quartz.kCGEventKeyDown, unittest.mock.Mock(), None)

        on_toggle.assert_not_called()

    def test_stop_event_stops_the_run_loop(self):
        # The real watcher thread run_clip_editor_space_bar_listener spawns to call
        # CFRunLoopStop runs asynchronously on a real background thread -- racy to assert on
        # directly, so threading.Thread is replaced with something that runs its target
        # synchronously instead, making the effect of stop_event already being set deterministic
        # to observe here.
        fake_quartz = make_fake_quartz(accessibility_trusted=True)
        stop_event = threading.Event()
        stop_event.set()

        def run_target_synchronously(target=None, daemon=None):
            target()
            return unittest.mock.MagicMock()

        with unittest.mock.patch.dict(sys.modules, {"Quartz": fake_quartz}):
            with unittest.mock.patch.object(pmac.threading, "Thread", side_effect=run_target_synchronously):
                pmac.run_clip_editor_space_bar_listener(None, unittest.mock.Mock(), stop_event)

        fake_quartz.CFRunLoopStop.assert_called_once_with("run_loop")


class ResolveBundleIdentifierTests(unittest.TestCase):
    # Fake .app bundles built with real tempfile dirs and a real plistlib-written Info.plist --
    # this is pure file I/O with no macOS-specific API involved, so it's exercisable (and
    # confirmed correct) on any real OS running the test, not just macOS.
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def make_app_bundle(self, app_name, bundle_id):
        app_dir = os.path.join(self.tmp_dir, f"{app_name}.app")
        contents_dir = os.path.join(app_dir, "Contents")
        macos_dir = os.path.join(contents_dir, "MacOS")
        os.makedirs(macos_dir)
        exe_path = os.path.join(macos_dir, app_name)
        with open(exe_path, "w") as f:
            f.write("")
        with open(os.path.join(contents_dir, "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
        return exe_path

    def test_resolves_a_real_looking_app_bundle(self):
        exe_path = self.make_app_bundle("Balatro", "com.playstack.balatro")
        self.assertEqual(pmac.resolve_bundle_identifier(exe_path), "com.playstack.balatro")

    def test_none_when_exe_path_is_none(self):
        self.assertIsNone(pmac.resolve_bundle_identifier(None))

    def test_none_when_exe_path_is_not_inside_an_app_bundle(self):
        bare_exe = os.path.join(self.tmp_dir, "bare-binary")
        with open(bare_exe, "w") as f:
            f.write("")
        self.assertIsNone(pmac.resolve_bundle_identifier(bare_exe))

    def test_none_when_info_plist_is_missing(self):
        macos_dir = os.path.join(self.tmp_dir, "Broken.app", "Contents", "MacOS")
        os.makedirs(macos_dir)
        exe_path = os.path.join(macos_dir, "Broken")
        with open(exe_path, "w") as f:
            f.write("")
        self.assertIsNone(pmac.resolve_bundle_identifier(exe_path))

    def test_none_when_info_plist_is_corrupt(self):
        app_dir = os.path.join(self.tmp_dir, "Corrupt.app")
        os.makedirs(os.path.join(app_dir, "Contents", "MacOS"))
        exe_path = os.path.join(app_dir, "Contents", "MacOS", "Corrupt")
        with open(exe_path, "w") as f:
            f.write("")
        with open(os.path.join(app_dir, "Contents", "Info.plist"), "w") as f:
            f.write("not a real plist")
        self.assertIsNone(pmac.resolve_bundle_identifier(exe_path))

    def test_none_when_plist_has_no_bundle_identifier_key(self):
        app_dir = os.path.join(self.tmp_dir, "NoId.app")
        os.makedirs(os.path.join(app_dir, "Contents", "MacOS"))
        exe_path = os.path.join(app_dir, "Contents", "MacOS", "NoId")
        with open(exe_path, "w") as f:
            f.write("")
        with open(os.path.join(app_dir, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"SomeOtherKey": "value"}, f)
        self.assertIsNone(pmac.resolve_bundle_identifier(exe_path))


class ProcessAudioCaptureSettingsTests(unittest.TestCase):
    # Confirmed live against a real OBS 30.2.3 instance on macOS 15.5 -- see
    # platform_macos.py's own PROCESS_AUDIO_CAPTURE_KIND comment for the full empirical findings
    # (GetInputKindList, GetInputDefaultSettings, and GetInputPropertiesListPropertyItems output).
    def test_kind_is_sck_audio_capture(self):
        self.assertEqual(pmac.PROCESS_AUDIO_CAPTURE_KIND, "sck_audio_capture")

    def test_builds_application_mode_settings_when_bundle_id_resolves(self):
        with unittest.mock.patch.object(pmac, "resolve_bundle_identifier", return_value="com.hnc.Discord"):
            settings = pmac.process_audio_capture_settings("Discord", "/Applications/Discord.app/Contents/MacOS/Discord")
        self.assertEqual(settings, {"type": 1, "application": "com.hnc.Discord"})
        self.assertEqual(settings["type"], pmac.SCK_AUDIO_CAPTURE_TYPE_APPLICATION)

    def test_none_when_bundle_id_cannot_be_resolved(self):
        with unittest.mock.patch.object(pmac, "resolve_bundle_identifier", return_value=None):
            settings = pmac.process_audio_capture_settings("Balatro", None)
        self.assertIsNone(settings)

    def test_process_name_is_accepted_but_unused(self):
        # macOS's "application" field needs a bundle identifier, which only exe_path can
        # resolve -- process_name exists purely for signature parity with the Windows backend.
        with unittest.mock.patch.object(pmac, "resolve_bundle_identifier", return_value="com.hnc.Discord"):
            settings_a = pmac.process_audio_capture_settings("Discord", "/some/path")
            settings_b = pmac.process_audio_capture_settings("SomethingElseEntirely", "/some/path")
        self.assertEqual(settings_a, settings_b)


class HideDockIconTests(unittest.TestCase):
    def test_sets_accessory_activation_policy(self):
        fake_appkit = unittest.mock.MagicMock()
        fake_appkit.NSApplicationActivationPolicyAccessory = 1
        fake_app = unittest.mock.MagicMock()
        fake_appkit.NSApplication.sharedApplication.return_value = fake_app

        with unittest.mock.patch.dict(sys.modules, {"AppKit": fake_appkit}):
            pmac.hide_dock_icon()

        fake_app.setActivationPolicy_.assert_called_once_with(1)

    def test_missing_pyobjc_is_logged_not_raised(self):
        with unittest.mock.patch.dict(sys.modules, {"AppKit": None}):
            with self.assertLogs(level="WARNING"):
                pmac.hide_dock_icon()  # must not raise


if __name__ == "__main__":
    unittest.main()
