import os
import sys
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


class EmbedVideoPlayerTests(unittest.TestCase):
    def test_wraps_winfo_id_as_ns_view_and_calls_set_nsobject(self):
        fake_objc = unittest.mock.MagicMock()
        fake_ns_view = object()
        fake_objc.objc_object.return_value = fake_ns_view

        player = unittest.mock.Mock()
        widget = unittest.mock.Mock()
        widget.winfo_id.return_value = 4242

        with unittest.mock.patch.dict(sys.modules, {"objc": fake_objc}):
            pmac.embed_video_player(player, widget)

        fake_objc.objc_object.assert_called_once_with(c_void_p=4242)
        player.set_nsobject.assert_called_once_with(fake_ns_view)


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


if __name__ == "__main__":
    unittest.main()
