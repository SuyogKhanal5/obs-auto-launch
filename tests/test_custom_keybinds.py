import ctypes
import os
import sys
import threading
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
from tests.fakes import FakeObsClient


class VkCodeForKeyTests(unittest.TestCase):
    def test_letter_keys(self):
        self.assertEqual(a.vk_code_for_key("s"), ord("S"))
        self.assertEqual(a.vk_code_for_key("Z"), ord("Z"))

    def test_digit_keys(self):
        self.assertEqual(a.vk_code_for_key("5"), ord("5"))

    def test_function_keys(self):
        self.assertEqual(a.vk_code_for_key("F1"), 0x70)
        self.assertEqual(a.vk_code_for_key("F12"), 0x7B)

    def test_invalid_key_returns_none(self):
        self.assertIsNone(a.vk_code_for_key("F13"))
        self.assertIsNone(a.vk_code_for_key("Enter"))
        self.assertIsNone(a.vk_code_for_key(""))
        self.assertIsNone(a.vk_code_for_key(None))


class ModFlagsForTests(unittest.TestCase):
    def test_combines_flags(self):
        self.assertEqual(a.mod_flags_for(["ctrl", "alt"]), a.MOD_CONTROL | a.MOD_ALT)

    def test_empty_or_none_is_zero(self):
        self.assertEqual(a.mod_flags_for([]), 0)
        self.assertEqual(a.mod_flags_for(None), 0)

    def test_unknown_modifier_is_ignored(self):
        self.assertEqual(a.mod_flags_for(["ctrl", "bogus"]), a.MOD_CONTROL)


class DescribeKeybindTests(unittest.TestCase):
    def test_formats_modifiers_and_key(self):
        self.assertEqual(a.describe_keybind({"modifiers": ["ctrl", "alt"], "key": "S"}), "Ctrl+Alt+S")

    def test_no_modifiers(self):
        self.assertEqual(a.describe_keybind({"modifiers": [], "key": "F5"}), "F5")


class PerformKeybindActionTests(unittest.TestCase):
    def test_split_uses_buffered_split_with_configured_seconds(self):
        client = FakeObsClient()
        a.perform_keybind_action(client, "split_record_file", manual_split_buffer_seconds=0)
        self.assertIn(("split_record_file",), client.calls)

    def test_save_replay_buffer(self):
        client = FakeObsClient()
        a.perform_keybind_action(client, "save_replay_buffer")
        self.assertIn(("save_replay_buffer",), client.calls)

    def test_unknown_action_is_logged_not_raised(self):
        client = FakeObsClient()
        with self.assertLogs(level="WARNING"):
            a.perform_keybind_action(client, "not_a_real_action")  # must not raise
        self.assertEqual(client.calls, [])

    def test_client_exception_is_logged_not_raised(self):
        client = FakeObsClient()

        def raise_error():
            raise RuntimeError("boom")

        client.save_replay_buffer = raise_error
        with self.assertLogs(level="ERROR"):
            a.perform_keybind_action(client, "save_replay_buffer")  # must not raise

    def test_all_catalog_actions_dispatch_without_error(self):
        # Every action in CUSTOM_KEYBIND_ACTIONS should be wired up in perform_keybind_action --
        # this catches a new action being added to the picker but never implemented.
        for action in a.CUSTOM_KEYBIND_ACTIONS:
            client = FakeObsClient()
            a.perform_keybind_action(client, action)
            self.assertTrue(client.calls, f"action '{action}' did not call anything on the client")


class FireCustomKeybindTests(unittest.TestCase):
    def test_warns_and_skips_when_obs_not_connected(self):
        with self.assertLogs(level="WARNING"):
            a.fire_custom_keybind(
                {"action": "split_record_file", "modifiers": [], "key": "S"},
                get_client=lambda: None,
                get_manual_split_buffer_seconds=lambda: 0,
            )  # must not raise

    def test_dispatches_to_client_when_connected(self):
        client = FakeObsClient()
        a.fire_custom_keybind(
            {"action": "save_replay_buffer", "modifiers": [], "key": "R"},
            get_client=lambda: client,
            get_manual_split_buffer_seconds=lambda: 0,
        )
        self.assertIn(("save_replay_buffer",), client.calls)


@unittest.skipUnless(sys.platform == "win32", "exercises the real Win32 ctypes.windll.user32 hotkey API")
class RunCustomKeybindListenerRegistrationRetryTests(unittest.TestCase):
    # A self-restart doesn't guarantee the previous process's hotkey registrations are released
    # by the time this thread starts -- confirmed live as a real bug: a keybind that lost this
    # race on one restart stayed dead for the rest of that session with only one WARNING logged.
    #
    # run_custom_keybind_listener/run_clip_editor_space_bar_listener (below) are still the
    # Windows-only ctypes.windll implementation -- global hotkeys get an X11/Quartz port in
    # CROSS_PLATFORM_PLAN.md Phase 3, at which point these move to platform_windows.py and this
    # guard gets replaced with real per-OS coverage rather than a skip.
    def setUp(self):
        self.mock_user32 = unittest.mock.Mock()
        self.mock_user32.GetMessageW.return_value = 0  # exit the message loop immediately
        self.bindings = [{"enabled": True, "action": "add_marker", "modifiers": ["ctrl"], "key": "F3"}]
        self.patchers = [
            unittest.mock.patch.object(a.ctypes.windll, "user32", self.mock_user32),
            unittest.mock.patch.object(a.time, "sleep"),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_retries_and_succeeds_after_transient_failures(self):
        self.mock_user32.RegisterHotKey.side_effect = [False, False, True]
        a.run_custom_keybind_listener(
            self.bindings, get_client=lambda: None, get_manual_split_buffer_seconds=lambda: 0,
        )
        self.assertEqual(self.mock_user32.RegisterHotKey.call_count, 3)

    def test_gives_up_after_max_retries_and_notifies(self):
        self.mock_user32.RegisterHotKey.return_value = False
        icon = unittest.mock.Mock()
        with unittest.mock.patch.object(a, "notify") as mock_notify:
            with self.assertLogs(level="WARNING"):
                a.run_custom_keybind_listener(
                    self.bindings, get_client=lambda: None, get_manual_split_buffer_seconds=lambda: 0,
                    icon=icon, notifications_config={"enabled": True},
                )
        self.assertEqual(self.mock_user32.RegisterHotKey.call_count, 5)
        mock_notify.assert_called_once()


class IsSpaceBarToggleEventTests(unittest.TestCase):
    EDITOR_HWND = 12345
    OTHER_HWND = 99999

    def test_matching_keydown_on_editor_window_toggles(self):
        self.assertTrue(a.is_space_bar_toggle_event(
            a.HC_ACTION, a.WM_KEYDOWN, a.VK_SPACE, self.EDITOR_HWND, self.EDITOR_HWND,
        ))

    def test_matching_syskeydown_on_editor_window_toggles(self):
        self.assertTrue(a.is_space_bar_toggle_event(
            a.HC_ACTION, a.WM_SYSKEYDOWN, a.VK_SPACE, self.EDITOR_HWND, self.EDITOR_HWND,
        ))

    def test_wrong_ncode_is_ignored(self):
        self.assertFalse(a.is_space_bar_toggle_event(
            a.HC_ACTION + 1, a.WM_KEYDOWN, a.VK_SPACE, self.EDITOR_HWND, self.EDITOR_HWND,
        ))

    def test_key_up_is_ignored(self):
        self.assertFalse(a.is_space_bar_toggle_event(
            a.HC_ACTION, 0x0101, a.VK_SPACE, self.EDITOR_HWND, self.EDITOR_HWND,  # WM_KEYUP
        ))

    def test_other_keys_are_ignored(self):
        self.assertFalse(a.is_space_bar_toggle_event(
            a.HC_ACTION, a.WM_KEYDOWN, 0x41, self.EDITOR_HWND, self.EDITOR_HWND,  # 'A'
        ))

    def test_space_pressed_while_a_different_window_is_foreground_is_ignored(self):
        self.assertFalse(a.is_space_bar_toggle_event(
            a.HC_ACTION, a.WM_KEYDOWN, a.VK_SPACE, self.OTHER_HWND, self.EDITOR_HWND,
        ))


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
        self.patcher = unittest.mock.patch.object(a.ctypes.windll, "user32", self.mock_user32)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_install_failure_logs_and_returns_without_looping(self):
        self.mock_user32.SetWindowsHookExW.return_value = 0
        stop_event = threading.Event()
        with self.assertLogs(level="WARNING"):
            a.run_clip_editor_space_bar_listener(12345, lambda: None, stop_event)
        self.mock_user32.PeekMessageW.assert_not_called()
        self.mock_user32.UnhookWindowsHookEx.assert_not_called()

    def test_pre_set_stop_event_still_unhooks_before_returning(self):
        stop_event = threading.Event()
        stop_event.set()
        a.run_clip_editor_space_bar_listener(12345, lambda: None, stop_event)
        self.mock_user32.UnhookWindowsHookEx.assert_called_once_with(777)

    def test_callback_toggles_only_for_editor_window_space_keydown(self):
        editor_hwnd = 12345
        stop_event = threading.Event()
        stop_event.set()  # exits the message loop immediately; only installing the hook matters
        calls = []
        a.run_clip_editor_space_bar_listener(editor_hwnd, lambda: calls.append(1), stop_event)

        # The real ctypes-wrapped callback passed to SetWindowsHookExW -- calling it directly
        # here exercises the SAME code path a real keystroke would, without needing an actual OS
        # hook installed (there's no physical keyboard in a test environment).
        callback = self.mock_user32.SetWindowsHookExW.call_args[0][1]

        self.mock_user32.GetForegroundWindow.return_value = editor_hwnd
        info = a.KBDLLHOOKSTRUCT(vkCode=a.VK_SPACE)
        result = callback(a.HC_ACTION, a.WM_KEYDOWN, ctypes.pointer(info))
        self.assertEqual(calls, [1])
        # ctypes round-trips lparam through a fresh POINTER(KBDLLHOOKSTRUCT) wrapper object on
        # its way into this callback, so it's never the SAME Python object as what was passed in
        # above -- comparing the args it was actually forwarded to CallNextHookEx with by their
        # dereferenced value (not object identity/equality) is what actually verifies pass-through.
        next_hook_args = self.mock_user32.CallNextHookEx.call_args[0]
        self.assertEqual(next_hook_args[:3], (None, a.HC_ACTION, a.WM_KEYDOWN))
        self.assertEqual(next_hook_args[3].contents.vkCode, a.VK_SPACE)
        self.assertEqual(result, 0)

    def test_callback_does_not_toggle_for_other_window_or_other_keys(self):
        editor_hwnd = 12345
        stop_event = threading.Event()
        stop_event.set()
        calls = []
        a.run_clip_editor_space_bar_listener(editor_hwnd, lambda: calls.append(1), stop_event)
        callback = self.mock_user32.SetWindowsHookExW.call_args[0][1]

        self.mock_user32.GetForegroundWindow.return_value = 99999  # a different window
        info = a.KBDLLHOOKSTRUCT(vkCode=a.VK_SPACE)
        callback(a.HC_ACTION, a.WM_KEYDOWN, ctypes.pointer(info))
        self.assertEqual(calls, [])

        self.mock_user32.GetForegroundWindow.return_value = editor_hwnd
        info_other_key = a.KBDLLHOOKSTRUCT(vkCode=0x41)  # 'A'
        callback(a.HC_ACTION, a.WM_KEYDOWN, ctypes.pointer(info_other_key))
        self.assertEqual(calls, [])

    def test_callback_exception_is_swallowed_and_still_calls_next_hook(self):
        editor_hwnd = 12345
        stop_event = threading.Event()
        stop_event.set()
        a.run_clip_editor_space_bar_listener(editor_hwnd, lambda: (_ for _ in ()).throw(RuntimeError("boom")), stop_event)
        callback = self.mock_user32.SetWindowsHookExW.call_args[0][1]

        self.mock_user32.GetForegroundWindow.return_value = editor_hwnd
        info = a.KBDLLHOOKSTRUCT(vkCode=a.VK_SPACE)
        with self.assertLogs(level="ERROR"):
            callback(a.HC_ACTION, a.WM_KEYDOWN, ctypes.pointer(info))
        self.mock_user32.CallNextHookEx.assert_called_once()


if __name__ == "__main__":
    unittest.main()
