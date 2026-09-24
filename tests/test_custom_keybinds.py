import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
from tests.fakes import FakeObsClient

# vk_code_for_key/mod_flags_for/MOD_*/is_space_bar_toggle_event/run_custom_keybind_listener/
# run_clip_editor_space_bar_listener moved to platform_windows.py -- see
# CROSS_PLATFORM_PLAN.md Phase 3 and tests/test_platform_windows.py, which now covers them (in a
# way that also works cross-OS via patch.object(..., create=True) for the pure-logic pieces, and
# a real skip-guard for the ones that touch the real Win32 ctypes.windll API). describe_keybind/
# perform_keybind_action/fire_custom_keybind stay here: OS-agnostic business logic dispatched
# into by every backend's listener, not moved.


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


class RunCustomKeybindListenerDispatchTests(unittest.TestCase):
    """autostart_script.run_custom_keybind_listener is now a thin dispatcher to
    platform_common.run_custom_keybind_listener -- this just confirms it forwards
    fire_custom_keybind/describe_keybind/notify (this module's own OS-agnostic names) correctly,
    not the real per-OS listener logic itself (see test_platform_windows.py/test_platform_linux.py/
    test_platform_macos.py for that)."""

    def test_forwards_to_platform_common_with_this_modules_callables(self):
        with unittest.mock.patch.object(a.platform_common, "run_custom_keybind_listener") as mock_run:
            a.run_custom_keybind_listener(
                [{"enabled": True, "key": "S", "modifiers": []}], get_client="get_client",
                get_manual_split_buffer_seconds="get_seconds", icon="icon",
                notifications_config="notif_cfg", status="status",
            )
        mock_run.assert_called_once_with(
            [{"enabled": True, "key": "S", "modifiers": []}], "get_client", "get_seconds",
            a.fire_custom_keybind, a.describe_keybind, a.notify,
            icon="icon", notifications_config="notif_cfg", status="status", stop_event=None,
        )


class RunClipEditorSpaceBarListenerDispatchTests(unittest.TestCase):
    def test_forwards_to_platform_common(self):
        stop_event = unittest.mock.Mock()
        on_toggle = unittest.mock.Mock()
        with unittest.mock.patch.object(a.platform_common, "run_clip_editor_space_bar_listener") as mock_run:
            a.run_clip_editor_space_bar_listener(12345, on_toggle, stop_event)
        mock_run.assert_called_once_with(12345, on_toggle, stop_event)


if __name__ == "__main__":
    unittest.main()
