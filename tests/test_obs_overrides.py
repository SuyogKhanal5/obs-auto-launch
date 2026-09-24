import os
import sys
import tempfile
import time
import unittest
import unittest.mock

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


class WantsMarkersTests(unittest.TestCase):
    def test_no_keybinds_returns_false(self):
        self.assertFalse(a.wants_markers([]))
        self.assertFalse(a.wants_markers(None))

    def test_enabled_add_marker_keybind_returns_true(self):
        self.assertTrue(a.wants_markers([{"enabled": True, "action": "add_marker"}]))

    def test_disabled_add_marker_keybind_returns_false(self):
        self.assertFalse(a.wants_markers([{"enabled": False, "action": "add_marker"}]))

    def test_missing_enabled_key_defaults_to_true(self):
        self.assertTrue(a.wants_markers([{"action": "add_marker"}]))

    def test_other_actions_do_not_count(self):
        self.assertFalse(a.wants_markers([{"enabled": True, "action": "split_record_file"}]))


class EnsureHybridMp4ForMarkersTests(unittest.TestCase):
    def test_sets_format_and_reports_restart_needed(self):
        client = FakeObsClient()
        needs_restart = a.ensure_hybrid_mp4_for_markers(client)
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecFormat2"), "hybrid_mp4")
        self.assertTrue(needs_restart)

    def test_already_hybrid_mp4_does_not_rewrite_or_need_restart(self):
        client = FakeObsClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        client.calls.clear()
        needs_restart = a.ensure_hybrid_mp4_for_markers(client)
        self.assertEqual(client.calls, [])
        self.assertFalse(needs_restart)

    def test_never_raises_when_client_errors(self):
        class RaisingClient(FakeObsClient):
            def set_profile_parameter(self, category, name, value):
                raise RuntimeError("boom")

        client = RaisingClient()
        needs_restart = a.ensure_hybrid_mp4_for_markers(client)  # must not raise
        self.assertFalse(needs_restart)


class PredictMicBoostOutputMulTests(unittest.TestCase):
    def test_zero_input_is_zero_output(self):
        self.assertEqual(a.predict_mic_boost_output_mul(0.0, 12.0), 0.0)

    def test_below_compressor_threshold_gets_a_flat_boost(self):
        # -40dBFS is below the compressor's -30dB threshold -- no compression applies, so a
        # +12dB boost should land exactly 12dB higher, unclamped (well under the -1dB limiter).
        result = a.predict_mic_boost_output_mul(10 ** (-40 / 20), 12.0)
        self.assertAlmostEqual(result, 10 ** (-28 / 20), places=6)

    def test_above_compressor_threshold_is_compressed_before_the_boost(self):
        # -20dBFS is 10dB above the -30dB threshold; at a 3:1 ratio that 10dB becomes 3.33dB
        # above threshold (-26.67dBFS) BEFORE the +12dB makeup gain is added.
        result = a.predict_mic_boost_output_mul(10 ** (-20 / 20), 12.0)
        expected_db = -30 + (-20 - -30) / 3 + 12
        self.assertAlmostEqual(result, 10 ** (expected_db / 20), places=6)

    def test_limiter_clamps_a_large_boost(self):
        # A full-scale (0dBFS) input with a generous +30dB boost would land at +10dBFS post-
        # compression -- the limiter's -1dB ceiling must cap it there instead.
        result = a.predict_mic_boost_output_mul(1.0, 30.0)
        self.assertAlmostEqual(result, 10 ** (-1 / 20), places=6)

    def test_noise_gate_silences_below_its_threshold(self):
        # -60dBFS is below a -50dB open_threshold -- the gate should silence it entirely,
        # regardless of whatever boost_db would otherwise apply.
        result = a.predict_mic_boost_output_mul(
            10 ** (-60 / 20), 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-50.0,
        )
        self.assertEqual(result, 0.0)

    def test_noise_gate_passes_through_above_its_threshold(self):
        result = a.predict_mic_boost_output_mul(
            10 ** (-60 / 20), 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-70.0,
        )
        self.assertGreater(result, 0.0)

    def test_disabled_noise_gate_never_silences_anything(self):
        result = a.predict_mic_boost_output_mul(
            10 ** (-60 / 20), 12.0, noise_gate_enabled=False, noise_gate_threshold_db=-10.0,
        )
        self.assertGreater(result, 0.0)

    def test_result_never_exceeds_the_full_scale_multiplier(self):
        result = a.predict_mic_boost_output_mul(1.0, 200.0)
        self.assertLessEqual(result, 1.0)


class ApplyMicBoostFromConfigTests(unittest.TestCase):
    def _client(self):
        return FakeObsClient(inputs={"Scarlet": {"kind": "wasapi_input_capture", "tracks": {}, "settings": {}}})

    def test_disabled_is_a_noop(self):
        client = self._client()
        a.apply_mic_boost_from_config(client, {"mic_boost": {"enabled": False, "input_name": "Scarlet"}})
        self.assertEqual(client.calls, [])

    def test_enabled_with_no_input_name_is_a_noop(self):
        client = self._client()
        a.apply_mic_boost_from_config(client, {"mic_boost": {"enabled": True, "input_name": ""}})
        self.assertEqual(client.calls, [])

    def test_enabled_applies_the_configured_boost(self):
        client = self._client()
        a.apply_mic_boost_from_config(
            client, {"mic_boost": {"enabled": True, "input_name": "Scarlet", "boost_db": 24.0}},
        )
        compressor = client.get_source_filter("Scarlet", a.MIC_BOOST_COMPRESSOR_FILTER_NAME)
        self.assertEqual(compressor.filter_settings["output_gain"], 24.0)

    def test_enabled_applies_the_configured_noise_gate(self):
        client = self._client()
        a.apply_mic_boost_from_config(
            client,
            {
                "mic_boost": {
                    "enabled": True, "input_name": "Scarlet", "boost_db": 24.0,
                    "noise_gate": {"enabled": True, "threshold_db": -30.0},
                },
            },
        )
        gate = client.get_source_filter("Scarlet", a.MIC_BOOST_NOISE_GATE_FILTER_NAME)
        self.assertTrue(gate.filter_enabled)
        self.assertEqual(gate.filter_settings["open_threshold"], -30.0)

    def test_missing_mic_boost_key_entirely_is_a_noop(self):
        client = self._client()
        a.apply_mic_boost_from_config(client, {})
        self.assertEqual(client.calls, [])


class EnsureMicBoostFilterTests(unittest.TestCase):
    def _client(self):
        return FakeObsClient(inputs={"Scarlet": {"kind": "wasapi_input_capture", "tracks": {}, "settings": {}}})

    def test_no_input_name_is_a_noop(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "", 12.0)
        self.assertEqual(client.calls, [])

    def test_creates_both_filters_on_a_fresh_source(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        filters = client.get_source_filter_list("Scarlet").filters
        names = {f.filter_name: f.filter_kind for f in filters}
        self.assertEqual(
            names,
            {
                a.MIC_BOOST_COMPRESSOR_FILTER_NAME: "compressor_filter",
                a.MIC_BOOST_LIMITER_FILTER_NAME: "limiter_filter",
            },
        )
        compressor = client.get_source_filter("Scarlet", a.MIC_BOOST_COMPRESSOR_FILTER_NAME)
        self.assertEqual(compressor.filter_settings["output_gain"], 12.0)
        limiter = client.get_source_filter("Scarlet", a.MIC_BOOST_LIMITER_FILTER_NAME)
        self.assertEqual(limiter.filter_settings["threshold"], -1.0)

    def test_compressor_created_before_limiter_in_the_chain(self):
        # Ordering matters here: a limiter has to come AFTER the compressor's own output_gain to
        # actually catch what that gain produces, not before it.
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        create_calls = [c for c in client.calls if c[0] == "create_source_filter"]
        self.assertEqual(
            [c[2] for c in create_calls], [a.MIC_BOOST_COMPRESSOR_FILTER_NAME, a.MIC_BOOST_LIMITER_FILTER_NAME],
        )

    def test_already_matching_settings_are_not_rewritten(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        self.assertEqual(client.calls, [])

    def test_changed_boost_db_updates_the_compressor_only(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 24.0)
        self.assertEqual(
            client.calls,
            [(
                "set_source_filter_settings", "Scarlet", a.MIC_BOOST_COMPRESSOR_FILTER_NAME,
                dict(a.MIC_BOOST_COMPRESSOR_BASE_SETTINGS, output_gain=24.0), False,
            )],
        )
        compressor = client.get_source_filter("Scarlet", a.MIC_BOOST_COMPRESSOR_FILTER_NAME)
        self.assertEqual(compressor.filter_settings["output_gain"], 24.0)

    def test_never_raises_when_client_errors(self):
        class RaisingClient(FakeObsClient):
            def create_source_filter(self, *args, **kwargs):
                raise RuntimeError("boom")

        client = RaisingClient(inputs={"Scarlet": {"kind": "wasapi_input_capture", "tracks": {}, "settings": {}}})
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)  # must not raise

    def test_unknown_source_is_logged_not_raised(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "No Such Source", 12.0)  # must not raise

    def test_no_threshold_given_skips_the_noise_gate_entirely(self):
        # Backward compatible with a caller that never opted into the noise gate at all (the
        # default noise_gate_threshold_db=None) -- must not create it or touch anything gate-named.
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        names = {f.filter_name for f in client.get_source_filter_list("Scarlet").filters}
        self.assertNotIn(a.MIC_BOOST_NOISE_GATE_FILTER_NAME, names)

    def test_noise_gate_created_with_derived_close_threshold_and_moved_to_front(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-40.0)
        gate = client.get_source_filter("Scarlet", a.MIC_BOOST_NOISE_GATE_FILTER_NAME)
        self.assertEqual(gate.filter_settings["open_threshold"], -40.0)
        # Hysteresis: close_threshold must stay BELOW open_threshold regardless of how low the
        # user's own chosen threshold is, or the gate's open/close logic would run backwards.
        self.assertEqual(gate.filter_settings["close_threshold"], -40.0 - a.MIC_BOOST_NOISE_GATE_HYSTERESIS_DB)
        self.assertTrue(gate.filter_enabled)
        names = [f.filter_name for f in client.get_source_filter_list("Scarlet").filters]
        self.assertEqual(names[0], a.MIC_BOOST_NOISE_GATE_FILTER_NAME)

    def test_noise_gate_disabled_toggle_is_synced_even_though_the_filter_stays(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-30.0)
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=False, noise_gate_threshold_db=-30.0)
        gate = client.get_source_filter("Scarlet", a.MIC_BOOST_NOISE_GATE_FILTER_NAME)
        self.assertFalse(gate.filter_enabled)  # toggled off, but not deleted -- threshold survives
        self.assertEqual(gate.filter_settings["open_threshold"], -30.0)

    def test_noise_gate_reenabled_restores_without_recreating(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-30.0)
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=False, noise_gate_threshold_db=-30.0)
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-30.0)
        self.assertEqual(
            [c for c in client.calls if c[0] == "create_source_filter"], [],  # never recreated
        )
        self.assertIn(("set_source_filter_enabled", "Scarlet", a.MIC_BOOST_NOISE_GATE_FILTER_NAME, True), client.calls)

    def test_unchanged_noise_gate_state_is_not_rewritten(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-30.0)
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-30.0)
        self.assertEqual(client.calls, [])

    def test_changed_threshold_updates_settings_without_touching_enabled_state(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-30.0)
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, noise_gate_enabled=True, noise_gate_threshold_db=-20.0)
        self.assertEqual(
            [c for c in client.calls if c[0] == "set_source_filter_enabled"], [],
        )
        gate = client.get_source_filter("Scarlet", a.MIC_BOOST_NOISE_GATE_FILTER_NAME)
        self.assertEqual(gate.filter_settings["open_threshold"], -20.0)


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


class AddRecordingMarkerTests(unittest.TestCase):
    def test_success_calls_create_record_chapter(self):
        client = FakeObsClient()
        result = a.add_recording_marker(client)
        self.assertTrue(result)
        self.assertIn(("create_record_chapter", None), client.calls)

    def test_unsupported_format_toasts_and_returns_false(self):
        class RejectingClient(FakeObsClient):
            def create_record_chapter(self, chapter_name=None):
                raise a.obsws.error.OBSSDKRequestError("CreateRecordChapter", a.OBS_CHAPTER_NOT_SUPPORTED_CODE, "")

        client = RejectingClient()
        with unittest.mock.patch.object(a, "notify") as mock_notify:
            result = a.add_recording_marker(client, icon=None, notifications_config={"enabled": True})
        self.assertFalse(result)
        mock_notify.assert_called_once()
        self.assertIn("format", mock_notify.call_args[0][3].lower())

    def test_other_error_does_not_toast(self):
        class RejectingClient(FakeObsClient):
            def create_record_chapter(self, chapter_name=None):
                raise a.obsws.error.OBSSDKRequestError("CreateRecordChapter", 500, "")

        client = RejectingClient()
        with unittest.mock.patch.object(a, "notify") as mock_notify:
            result = a.add_recording_marker(client)
        self.assertFalse(result)
        mock_notify.assert_not_called()

    def test_success_flashes_the_tray_icon_when_icon_and_status_given(self):
        # Same "something just happened" flash as a manual split or a replay-buffer save --
        # there's no OBS event for a chapter being created, so this only fires from here.
        client = FakeObsClient()
        icon = unittest.mock.Mock()
        status = {"recording": True}
        before = time.time()
        result = a.add_recording_marker(client, icon=icon, status=status)
        self.assertTrue(result)
        self.assertGreater(status.get("flash_until", 0), before)
        self.assertIsInstance(icon.icon, a.Image.Image)  # build_tray_image() actually ran

    def test_success_does_not_flash_without_icon_or_status(self):
        client = FakeObsClient()
        result = a.add_recording_marker(client)  # icon=None, status=None -- must not raise
        self.assertTrue(result)


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


class ActiveSessionMarkerTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.mkdtemp()
        self.marker_path = os.path.join(tmp_dir, ".active_recording_session.json")
        self.patcher = unittest.mock.patch.object(a, "ACTIVE_SESSION_MARKER_PATH", self.marker_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_read_returns_none_when_no_marker_written(self):
        self.assertIsNone(a.read_active_session_marker())

    def test_write_then_read_round_trips_the_display_name(self):
        a.write_active_session_marker("Street Fighter 6")
        self.assertEqual(a.read_active_session_marker(), {"display_name": "Street Fighter 6"})

    def test_clear_removes_the_marker(self):
        a.write_active_session_marker("Street Fighter 6")
        a.clear_active_session_marker()
        self.assertIsNone(a.read_active_session_marker())

    def test_clear_does_not_raise_when_no_marker_exists(self):
        a.clear_active_session_marker()  # must not raise


class ResolveRecordingResolutionTests(unittest.TestCase):
    def test_match_canvas_returns_base_resolution_unchanged(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "Match canvas (no scaling)"), (1920, 1080))

    def test_unrecognized_choice_falls_back_to_base_resolution(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "nonsense"), (1920, 1080))

    def test_1080p_preset_keeps_canvas_aspect_ratio(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "1080p"), (1920, 1080))

    def test_720p_preset_scales_width_from_canvas_aspect_ratio(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "720p"), (1280, 720))

    def test_ultrawide_canvas_still_rounds_width_to_even(self):
        # 3440x1440 at height 720 -> width 1720.0 exactly, but exercised with a canvas whose
        # ratio doesn't divide evenly to confirm the even-rounding actually does something.
        width, height = a.resolve_recording_resolution(3441, 1440, "720p")
        self.assertEqual(height, 720)
        self.assertEqual(width % 2, 0)


class ApplyRecordingResolutionTests(unittest.TestCase):
    def test_no_configured_choice_does_not_touch_video_settings(self):
        client = FakeObsClient()
        a.apply_recording_resolution(client, {})
        self.assertNotIn("set_video_settings", [c[0] for c in client.calls])

    def test_changes_output_resolution_when_different_from_target(self):
        client = FakeObsClient()  # base/output default to 1920x1080
        a.apply_recording_resolution(client, {"recording_resolution": "720p"})
        calls = [c for c in client.calls if c[0] == "set_video_settings"]
        self.assertEqual(len(calls), 1)
        video = client.get_video_settings()
        self.assertEqual((video.output_width, video.output_height), (1280, 720))
        # base resolution and fps must be left untouched
        self.assertEqual((video.base_width, video.base_height), (1920, 1080))

    def test_no_write_when_already_at_the_target_resolution(self):
        client = FakeObsClient()
        client.set_video_settings(60, 1, 1920, 1080, 1280, 720)
        client.calls.clear()
        a.apply_recording_resolution(client, {"recording_resolution": "720p"})
        self.assertNotIn("set_video_settings", [c[0] for c in client.calls])

    def test_match_canvas_actively_undoes_an_existing_downscale(self):
        # This is the whole point of the "Match canvas" option -- fixing exactly the "my
        # recording came out smaller than the canvas" problem it was added for, not just being
        # a synonym for "leave whatever OBS already has".
        client = FakeObsClient()
        client.set_video_settings(60, 1, 1920, 1080, 1280, 720)
        client.calls.clear()
        a.apply_recording_resolution(client, {"recording_resolution": "Match canvas (no scaling)"})
        video = client.get_video_settings()
        self.assertEqual((video.output_width, video.output_height), (1920, 1080))

    def test_client_error_is_logged_not_raised(self):
        client = FakeObsClient()

        def raise_error(*_args):
            raise RuntimeError("boom")

        client.set_video_settings = raise_error
        with self.assertLogs(level="ERROR"):
            a.apply_recording_resolution(client, {"recording_resolution": "720p"})  # must not raise


class ApplyProcessCaptureSyncOffsetTests(unittest.TestCase):
    def test_sets_offset_when_different(self):
        client = FakeObsClient(inputs={"Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {}}})
        a.apply_process_capture_sync_offset(client, "Game Audio", -55)
        self.assertIn(("set_input_audio_sync_offset", "Game Audio", -55), client.calls)
        self.assertEqual(client.get_input_audio_sync_offset("Game Audio").input_audio_sync_offset, -55)

    def test_no_write_when_already_correct(self):
        client = FakeObsClient(inputs={"Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {}}})
        client.inputs["Game Audio"]["sync_offset"] = -55
        a.apply_process_capture_sync_offset(client, "Game Audio", -55)
        self.assertNotIn("set_input_audio_sync_offset", [c[0] for c in client.calls])

    def test_missing_input_is_logged_not_raised(self):
        client = FakeObsClient()
        with self.assertLogs(level="WARNING"):
            a.apply_process_capture_sync_offset(client, "Nonexistent", -55)  # must not raise


if __name__ == "__main__":
    unittest.main()
