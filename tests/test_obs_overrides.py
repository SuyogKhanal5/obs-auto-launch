import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
from tests.fakes import FakeObsClient


class ApplyOutputFolderTests(unittest.TestCase):
    def test_none_is_a_noop(self):
        client = FakeObsClient()
        client.set_record_directory(r"C:\Original")
        a.apply_output_folder(client, None)
        self.assertEqual(client.get_record_directory().record_directory, r"C:\Original")

    def test_sets_new_folder_and_creates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "Recordings")
            client = FakeObsClient()
            a.apply_output_folder(client, target)
            self.assertEqual(client.get_record_directory().record_directory, target)
            self.assertTrue(os.path.isdir(target))

    def test_already_matching_folder_is_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeObsClient()
            client.set_record_directory(tmp)
            client.calls.clear()
            a.apply_output_folder(client, tmp)
            self.assertNotIn(("set_record_directory", tmp), client.calls)


class ApplyRecordingFormatTests(unittest.TestCase):
    def test_none_is_a_noop(self):
        client = FakeObsClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "mp4")
        a.apply_recording_format(client, None)
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecFormat2"), "mp4")

    def test_sets_new_format(self):
        client = FakeObsClient()
        a.apply_recording_format(client, "mkv")
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecFormat2"), "mkv")

    def test_already_matching_format_is_not_reset(self):
        client = FakeObsClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        client.calls.clear()
        a.apply_recording_format(client, "hybrid_mp4")
        self.assertEqual(client.calls, [])

    def test_accepts_a_format_outside_the_curated_options_list(self):
        # The field is deliberately editable, not a locked dropdown -- must not reject/validate
        # against RECORDING_FORMAT_OPTIONS.
        client = FakeObsClient()
        a.apply_recording_format(client, "some_future_format_code")
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecFormat2"), "some_future_format_code")


class GetReplayBufferModeTests(unittest.TestCase):
    def test_explicit_mode_wins(self):
        self.assertEqual(a.get_replay_buffer_mode({"mode": "only", "enabled": False}), "only")

    def test_unknown_mode_falls_back_to_legacy_enabled(self):
        self.assertEqual(a.get_replay_buffer_mode({"mode": "bogus", "enabled": True}), "with_recording")

    def test_legacy_enabled_true_maps_to_with_recording(self):
        self.assertEqual(a.get_replay_buffer_mode({"enabled": True}), "with_recording")

    def test_legacy_enabled_false_or_missing_maps_to_off(self):
        self.assertEqual(a.get_replay_buffer_mode({"enabled": False}), "off")
        self.assertEqual(a.get_replay_buffer_mode({}), "off")


class ApplyReplayBufferSettingsTests(unittest.TestCase):
    def test_off_mode_disables_in_simple_output(self):
        client = FakeObsClient()
        client.set_profile_parameter("SimpleOutput", "RecRB", "true")
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "off"})
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRB"), "false")
        # Turning it off never needs a restart -- nothing has to restart just to stop calling
        # StartReplayBuffer.
        self.assertFalse(needs_restart)

    def test_fresh_unset_value_with_off_mode_does_not_need_restart(self):
        # A brand-new profile that has never touched this setting reads back None, not "false" --
        # None != "false" must not be treated as a real change requiring a pointless restart on
        # every fresh install's first game launch.
        client = FakeObsClient()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "off"})
        self.assertFalse(needs_restart)

    def test_with_recording_mode_enables_and_sets_length(self):
        client = FakeObsClient()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording", "max_seconds": 45})
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRB"), "true")
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRBTime"), "45")
        # Enabling it for the first time is exactly the case OBS won't pick up without a restart.
        self.assertTrue(needs_restart)

    def test_only_mode_also_enables_the_obs_setting(self):
        client = FakeObsClient()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "only", "max_seconds": 30})
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRB"), "true")
        self.assertTrue(needs_restart)

    def test_default_length_used_when_not_configured(self):
        client = FakeObsClient()
        a.apply_replay_buffer_settings(client, {"mode": "with_recording"})
        self.assertEqual(
            a.get_profile_parameter_value(client, "SimpleOutput", "RecRBTime"),
            str(a.DEFAULT_REPLAY_BUFFER_SECONDS),
        )

    def test_uses_advout_category_when_output_mode_is_advanced(self):
        client = FakeObsClient()
        client.set_profile_parameter("Output", "Mode", "Advanced")
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording", "max_seconds": 60})
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecRB"), "true")
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecRBTime"), "60")
        self.assertTrue(needs_restart)

    def test_already_matching_settings_are_not_rewritten(self):
        client = FakeObsClient()
        client.set_profile_parameter("SimpleOutput", "RecRB", "true")
        client.set_profile_parameter("SimpleOutput", "RecRBTime", "30")
        client.calls.clear()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording", "max_seconds": 30})
        self.assertEqual(client.calls, [])
        # Nothing changed, so no restart is needed either.
        self.assertFalse(needs_restart)

    def test_changing_only_the_length_still_needs_restart(self):
        # RecRB was already "true" -- only RecRBTime differs. Must still report needs_restart,
        # since OBS also needs a restart to pick up a changed buffer length.
        client = FakeObsClient()
        client.set_profile_parameter("SimpleOutput", "RecRB", "true")
        client.set_profile_parameter("SimpleOutput", "RecRBTime", "30")
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording", "max_seconds": 60})
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRBTime"), "60")
        self.assertTrue(needs_restart)

    def test_never_raises_when_client_errors(self):
        class RaisingClient(FakeObsClient):
            def set_profile_parameter(self, category, name, value):
                raise RuntimeError("boom")

        client = RaisingClient()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording"})  # must not raise
        self.assertFalse(needs_restart)


class IsEventClientConnectedTests(unittest.TestCase):
    class FakeSocket:
        def __init__(self, connected):
            self.connected = connected

    class FakeBaseClient:
        def __init__(self, connected):
            self.ws = IsEventClientConnectedTests.FakeSocket(connected)

    class FakeEventClient:
        def __init__(self, connected):
            self.base_client = IsEventClientConnectedTests.FakeBaseClient(connected)

    def test_true_when_socket_reports_connected(self):
        self.assertTrue(a.is_event_client_connected(self.FakeEventClient(True)))

    def test_false_when_socket_reports_disconnected(self):
        self.assertFalse(a.is_event_client_connected(self.FakeEventClient(False)))

    def test_assumes_connected_when_attribute_is_unavailable(self):
        # A version/API mismatch shouldn't itself force unnecessary reconnect churn.
        self.assertTrue(a.is_event_client_connected(object()))


class SetGameAudioCaptureTargetTests(unittest.TestCase):
    def test_repoints_an_existing_input(self):
        client = FakeObsClient(inputs={
            "Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {}, "settings": {}},
        })
        a.set_game_audio_capture_target(client, "Game Audio", "Balatro.exe")
        self.assertEqual(
            client.inputs["Game Audio"]["settings"],
            {"window": "::Balatro.exe", "priority": a.WINDOW_MATCH_PRIORITY_EXE_FALLBACK},
        )
        self.assertNotIn("Game Audio", [c[2] for c in client.calls if c[0] == "create_input"])

    def test_creates_the_input_if_it_does_not_exist_yet(self):
        client = FakeObsClient()  # no "Game Audio" input at all
        a.set_game_audio_capture_target(client, "Game Audio", "Balatro.exe")
        self.assertIn("Game Audio", client.inputs)
        self.assertEqual(client.inputs["Game Audio"]["kind"], "wasapi_process_output_capture")
        self.assertEqual(
            client.inputs["Game Audio"]["settings"],
            {"window": "::Balatro.exe", "priority": a.WINDOW_MATCH_PRIORITY_EXE_FALLBACK},
        )

    def test_created_input_is_added_to_the_current_scene(self):
        client = FakeObsClient(current_scene="My Scene")
        a.set_game_audio_capture_target(client, "Game Audio", "Balatro.exe")
        create_calls = [c for c in client.calls if c[0] == "create_input"]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0][1], "My Scene")

    def test_does_not_create_on_an_unrelated_error(self):
        # Only a "resource not found" error should trigger the create-fallback -- anything else
        # (e.g. a connection hiccup) should just be logged, not misread as "input is missing".
        client = FakeObsClient()

        def raise_unrelated(name, settings, overlay):
            raise a.obsws.error.OBSSDKRequestError("SetInputSettings", 500, "internal error")

        client.set_input_settings = raise_unrelated
        with self.assertLogs(level="WARNING"):
            a.set_game_audio_capture_target(client, "Game Audio", "Balatro.exe")  # must not raise
        self.assertNotIn("Game Audio", client.inputs)


if __name__ == "__main__":
    unittest.main()
