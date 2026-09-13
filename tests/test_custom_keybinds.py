import os
import sys
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


class RunCustomKeybindListenerRegistrationRetryTests(unittest.TestCase):
    # A self-restart doesn't guarantee the previous process's hotkey registrations are released
    # by the time this thread starts -- confirmed live as a real bug: a keybind that lost this
    # race on one restart stayed dead for the rest of that session with only one WARNING logged.
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


if __name__ == "__main__":
    unittest.main()
