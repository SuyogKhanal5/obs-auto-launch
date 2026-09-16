import ctypes
import os
import sys
import threading
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
        # Builds the expected path via os.path.join, the same way the real function does, rather
        # than a hardcoded backslash-literal string -- os.path.join on a backslash-containing
        # *input* string behaves differently depending on which real OS runs the test (ntpath
        # treats "\" as a separator, posixpath treats it as a literal character), so only a
        # same-mechanism comparison is correct on every OS this suite actually runs on, per
        # CROSS_PLATFORM_PLAN.md §3.2.
        install_location = r"C:\OBS Install"
        expected = os.path.join(install_location, "bin", "64bit", "obs64.exe")

        fake_winreg = unittest.mock.MagicMock()
        fake_winreg.HKEY_LOCAL_MACHINE = "HKLM"
        fake_key = unittest.mock.MagicMock()
        fake_winreg.OpenKey.return_value.__enter__.return_value = fake_key
        fake_winreg.QueryValueEx.return_value = (install_location, 1)

        def isfile(path):
            return path == expected

        with unittest.mock.patch.object(pw, "winreg", fake_winreg, create=True):
            with unittest.mock.patch.object(pw.os.path, "isfile", side_effect=isfile):
                self.assertEqual(pw.find_obs_executable(), expected)

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


class GetWindowTitlesTests(unittest.TestCase):
    # Unlike the winreg-based tests above, this calls the REAL ctypes.windll.user32.EnumWindows
    # -- genuinely real on this OS (Windows), so it's kept as a real smoke test here rather than
    # mocked/skipped: confirms the actual EnumWindows callback plumbing works end to end against
    # this machine's real, currently-open windows, without asserting on their specific contents
    # (which vary run to run) since that's not the point -- the point is "does this crash."
    @unittest.skipUnless(sys.platform == "win32", "calls the real Win32 EnumWindows API")
    def test_returns_a_dict_without_raising(self):
        result = pw.get_window_titles()
        self.assertIsInstance(result, dict)
        for pid, titles in result.items():
            self.assertIsInstance(pid, int)
            self.assertIsInstance(titles, list)


class VkCodeForKeyTests(unittest.TestCase):
    def test_letter_keys(self):
        self.assertEqual(pw.vk_code_for_key("s"), ord("S"))
        self.assertEqual(pw.vk_code_for_key("Z"), ord("Z"))

    def test_digit_keys(self):
        self.assertEqual(pw.vk_code_for_key("5"), ord("5"))

    def test_function_keys(self):
        self.assertEqual(pw.vk_code_for_key("F1"), 0x70)
        self.assertEqual(pw.vk_code_for_key("F12"), 0x7B)

    def test_invalid_key_returns_none(self):
        self.assertIsNone(pw.vk_code_for_key("F13"))
        self.assertIsNone(pw.vk_code_for_key("Enter"))
        self.assertIsNone(pw.vk_code_for_key(""))
        self.assertIsNone(pw.vk_code_for_key(None))


class ModFlagsForTests(unittest.TestCase):
    def test_combines_flags(self):
        self.assertEqual(pw.mod_flags_for(["ctrl", "alt"]), pw.MOD_CONTROL | pw.MOD_ALT)

    def test_empty_or_none_is_zero(self):
        self.assertEqual(pw.mod_flags_for([]), 0)
        self.assertEqual(pw.mod_flags_for(None), 0)

    def test_unknown_modifier_is_ignored(self):
        self.assertEqual(pw.mod_flags_for(["ctrl", "bogus"]), pw.MOD_CONTROL)


class IsSpaceBarToggleEventTests(unittest.TestCase):
    EDITOR_HWND = 12345
    OTHER_HWND = 99999

    def test_matching_keydown_on_editor_window_toggles(self):
        self.assertTrue(pw.is_space_bar_toggle_event(
            pw.HC_ACTION, pw.WM_KEYDOWN, pw.VK_SPACE, self.EDITOR_HWND, self.EDITOR_HWND,
        ))

    def test_matching_syskeydown_on_editor_window_toggles(self):
        self.assertTrue(pw.is_space_bar_toggle_event(
            pw.HC_ACTION, pw.WM_SYSKEYDOWN, pw.VK_SPACE, self.EDITOR_HWND, self.EDITOR_HWND,
        ))

    def test_wrong_ncode_is_ignored(self):
        self.assertFalse(pw.is_space_bar_toggle_event(
            pw.HC_ACTION + 1, pw.WM_KEYDOWN, pw.VK_SPACE, self.EDITOR_HWND, self.EDITOR_HWND,
        ))

    def test_key_up_is_ignored(self):
        self.assertFalse(pw.is_space_bar_toggle_event(
            pw.HC_ACTION, 0x0101, pw.VK_SPACE, self.EDITOR_HWND, self.EDITOR_HWND,  # WM_KEYUP
        ))

    def test_other_keys_are_ignored(self):
        self.assertFalse(pw.is_space_bar_toggle_event(
            pw.HC_ACTION, pw.WM_KEYDOWN, 0x41, self.EDITOR_HWND, self.EDITOR_HWND,  # 'A'
        ))

    def test_space_pressed_while_a_different_window_is_foreground_is_ignored(self):
        self.assertFalse(pw.is_space_bar_toggle_event(
            pw.HC_ACTION, pw.WM_KEYDOWN, pw.VK_SPACE, self.OTHER_HWND, self.EDITOR_HWND,
        ))


@unittest.skipUnless(sys.platform == "win32", "exercises the real Win32 ctypes.windll.user32 hotkey API")
class RunCustomKeybindListenerRegistrationRetryTests(unittest.TestCase):
    # A self-restart doesn't guarantee the previous process's hotkey registrations are released
    # by the time this thread starts -- confirmed live as a real bug: a keybind that lost this
    # race on one restart stayed dead for the rest of that session with only one WARNING logged.
    def setUp(self):
        self.mock_user32 = unittest.mock.Mock()
        self.mock_user32.GetMessageW.return_value = 0  # exit the message loop immediately
        self.bindings = [{"enabled": True, "action": "add_marker", "modifiers": ["ctrl"], "key": "F3"}]
        self.fire_keybind = unittest.mock.Mock()
        self.describe_keybind = unittest.mock.Mock(return_value="Ctrl+F3")
        self.notify = unittest.mock.Mock()
        self.patchers = [
            unittest.mock.patch.object(pw.ctypes.windll, "user32", self.mock_user32),
            unittest.mock.patch.object(pw.time, "sleep"),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_retries_and_succeeds_after_transient_failures(self):
        self.mock_user32.RegisterHotKey.side_effect = [False, False, True]
        pw.run_custom_keybind_listener(
            self.bindings, lambda: None, lambda: 0, self.fire_keybind, self.describe_keybind, self.notify,
        )
        self.assertEqual(self.mock_user32.RegisterHotKey.call_count, 3)

    def test_gives_up_after_max_retries_and_notifies(self):
        self.mock_user32.RegisterHotKey.return_value = False
        icon = unittest.mock.Mock()
        with self.assertLogs(level="WARNING"):
            pw.run_custom_keybind_listener(
                self.bindings, lambda: None, lambda: 0, self.fire_keybind, self.describe_keybind, self.notify,
                icon=icon, notifications_config={"enabled": True},
            )
        self.assertEqual(self.mock_user32.RegisterHotKey.call_count, 5)
        self.notify.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "exercises the real Win32 ctypes.windll.user32 hotkey API")
class RunClipEditorSpaceBarListenerTests(unittest.TestCase):
    def setUp(self):
        self.mock_user32 = unittest.mock.Mock()
        # Every SetWindowsHookExW/UnhookWindowsHookEx/CallNextHookEx/GetForegroundWindow call
        # sets its own .restype/.argtypes as a real ctypes function pointer would let it -- a
        # bare Mock attribute happily accepts that assignment and ignores it, which is exactly
        # what's wanted here (these tests care about call counts/args, not real marshaling).
        self.mock_user32.SetWindowsHookExW.return_value = 777  # a fake, truthy hook handle
        self.mock_user32.PeekMessageW.return_value = 0  # no messages waiting
        # The real hook_proc's WINFUNCTYPE restype (c_ssize_t) enforces an actual integer return
        # value even when the callback is invoked directly like this (not through a real OS
        # callback) -- a bare Mock() default here fails that conversion inside ctypes itself.
        self.mock_user32.CallNextHookEx.return_value = 0
        self.patcher = unittest.mock.patch.object(pw.ctypes.windll, "user32", self.mock_user32)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_install_failure_logs_and_returns_without_looping(self):
        self.mock_user32.SetWindowsHookExW.return_value = 0
        stop_event = threading.Event()
        with self.assertLogs(level="WARNING"):
            pw.run_clip_editor_space_bar_listener(12345, lambda: None, stop_event)
        self.mock_user32.PeekMessageW.assert_not_called()
        self.mock_user32.UnhookWindowsHookEx.assert_not_called()

    def test_pre_set_stop_event_still_unhooks_before_returning(self):
        stop_event = threading.Event()
        stop_event.set()
        pw.run_clip_editor_space_bar_listener(12345, lambda: None, stop_event)
        self.mock_user32.UnhookWindowsHookEx.assert_called_once_with(777)

    def test_callback_toggles_only_for_editor_window_space_keydown(self):
        editor_hwnd = 12345
        stop_event = threading.Event()
        stop_event.set()  # exits the message loop immediately; only installing the hook matters
        calls = []
        pw.run_clip_editor_space_bar_listener(editor_hwnd, lambda: calls.append(1), stop_event)

        # The real ctypes-wrapped callback passed to SetWindowsHookExW -- calling it directly
        # here exercises the SAME code path a real keystroke would, without needing an actual OS
        # hook installed (there's no physical keyboard in a test environment).
        callback = self.mock_user32.SetWindowsHookExW.call_args[0][1]

        self.mock_user32.GetForegroundWindow.return_value = editor_hwnd
        info = pw.KBDLLHOOKSTRUCT(vkCode=pw.VK_SPACE)
        result = callback(pw.HC_ACTION, pw.WM_KEYDOWN, ctypes.pointer(info))
        self.assertEqual(calls, [1])
        # ctypes round-trips lparam through a fresh POINTER(KBDLLHOOKSTRUCT) wrapper object on
        # its way into this callback, so it's never the SAME Python object as what was passed in
        # above -- comparing the args it was actually forwarded to CallNextHookEx with by their
        # dereferenced value (not object identity/equality) is what actually verifies pass-through.
        next_hook_args = self.mock_user32.CallNextHookEx.call_args[0]
        self.assertEqual(next_hook_args[:3], (None, pw.HC_ACTION, pw.WM_KEYDOWN))
        self.assertEqual(next_hook_args[3].contents.vkCode, pw.VK_SPACE)
        self.assertEqual(result, 0)

    def test_callback_does_not_toggle_for_other_window_or_other_keys(self):
        editor_hwnd = 12345
        stop_event = threading.Event()
        stop_event.set()
        calls = []
        pw.run_clip_editor_space_bar_listener(editor_hwnd, lambda: calls.append(1), stop_event)
        callback = self.mock_user32.SetWindowsHookExW.call_args[0][1]

        self.mock_user32.GetForegroundWindow.return_value = 99999  # a different window
        info = pw.KBDLLHOOKSTRUCT(vkCode=pw.VK_SPACE)
        callback(pw.HC_ACTION, pw.WM_KEYDOWN, ctypes.pointer(info))
        self.assertEqual(calls, [])

        self.mock_user32.GetForegroundWindow.return_value = editor_hwnd
        info_other_key = pw.KBDLLHOOKSTRUCT(vkCode=0x41)  # 'A'
        callback(pw.HC_ACTION, pw.WM_KEYDOWN, ctypes.pointer(info_other_key))
        self.assertEqual(calls, [])

    def test_callback_exception_is_swallowed_and_still_calls_next_hook(self):
        editor_hwnd = 12345
        stop_event = threading.Event()
        stop_event.set()
        pw.run_clip_editor_space_bar_listener(
            editor_hwnd, lambda: (_ for _ in ()).throw(RuntimeError("boom")), stop_event,
        )
        callback = self.mock_user32.SetWindowsHookExW.call_args[0][1]

        self.mock_user32.GetForegroundWindow.return_value = editor_hwnd
        info = pw.KBDLLHOOKSTRUCT(vkCode=pw.VK_SPACE)
        with self.assertLogs(level="ERROR"):
            callback(pw.HC_ACTION, pw.WM_KEYDOWN, ctypes.pointer(info))
        self.mock_user32.CallNextHookEx.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "constructs a real ctypes.wintypes.HWND")
class ResolveEditorTopLevelWindowTests(unittest.TestCase):
    def test_resolves_via_get_ancestor(self):
        mock_user32 = unittest.mock.Mock()
        mock_user32.GetAncestor.return_value = 999
        with unittest.mock.patch.object(pw.ctypes, "windll", unittest.mock.Mock(user32=mock_user32)):
            result = pw.resolve_editor_top_level_window(12345)
        self.assertEqual(result, 999)


if __name__ == "__main__":
    unittest.main()
