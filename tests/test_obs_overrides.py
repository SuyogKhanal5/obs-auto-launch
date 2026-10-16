import json
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
from tests.fakes import FakeObsClient


class WaitUntilObsReadyTests(unittest.TestCase):
    # Confirmed live, twice in one session: OBS can accept a websocket connection and finish the
    # identify handshake well before it's actually ready to answer ANY request -- connect_obs's
    # own retries only cover the connection itself, not this. wait_until_obs_ready is the guard
    # for callers that need OBS genuinely ready, not just connected.
    def test_ready_immediately_returns_true_without_sleeping(self):
        client = FakeObsClient()
        with unittest.mock.patch.object(a.time, "sleep") as mock_sleep:
            self.assertTrue(a.wait_until_obs_ready(client))
        mock_sleep.assert_not_called()

    def test_retries_past_transient_not_ready_then_succeeds(self):
        class FlakyClient(FakeObsClient):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def get_record_status(self):
                self.attempts += 1
                if self.attempts < 3:
                    raise a.obsws.error.OBSSDKRequestError("GetRecordStatus", a.OBS_NOT_READY_CODE, "not ready")
                return super().get_record_status()

        client = FlakyClient()
        with unittest.mock.patch.object(a.time, "sleep"):
            self.assertTrue(a.wait_until_obs_ready(client))
        self.assertEqual(client.attempts, 3)

    def test_gives_up_after_retries_exhausted(self):
        class AlwaysNotReadyClient(FakeObsClient):
            def get_record_status(self):
                raise a.obsws.error.OBSSDKRequestError("GetRecordStatus", a.OBS_NOT_READY_CODE, "not ready")

        client = AlwaysNotReadyClient()
        with unittest.mock.patch.object(a.time, "sleep"):
            self.assertFalse(a.wait_until_obs_ready(client, retries=3, delay=0))

    def test_a_different_error_code_gives_up_immediately_without_retrying(self):
        class DifferentErrorClient(FakeObsClient):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def get_record_status(self):
                self.attempts += 1
                raise a.obsws.error.OBSSDKRequestError("GetRecordStatus", 500, "something else entirely")

        client = DifferentErrorClient()
        self.assertFalse(a.wait_until_obs_ready(client))
        self.assertEqual(client.attempts, 1)


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


class ComputeMicBoostDbFromSamplesTests(unittest.TestCase):
    def test_no_samples_reports_no_signal(self):
        boost_db, error = a.compute_mic_boost_db_from_samples([], input_name="Scarlet")
        self.assertIsNone(boost_db)
        self.assertIn("Scarlet", error)
        self.assertIn("No signal", error)

    def test_near_silence_is_inconclusive_not_a_huge_boost(self):
        # Below MIC_BOOST_CALIBRATION_MIN_SIGNAL_DB -- shouldn't compute a boost against what's
        # basically silence, even though the math could technically produce a (meaningless) number.
        voice = [10 ** (-90 / 20)] * 10
        boost_db, error = a.compute_mic_boost_db_from_samples(voice)
        self.assertIsNone(boost_db)
        self.assertIn("normally", error)

    def test_quiet_voice_below_compressor_threshold_gets_a_flat_solve(self):
        # -50dBFS is below the compressor's -30dB threshold -- no compression applies, so the
        # needed boost is simply target - reference, unclamped.
        voice = [10 ** (-50 / 20)] * 10
        boost_db, error = a.compute_mic_boost_db_from_samples(voice, target_db=-18.0)
        self.assertIsNone(error)
        self.assertAlmostEqual(boost_db, -18.0 - -50.0, places=1)

    def test_loud_voice_above_compressor_threshold_accounts_for_the_ratio(self):
        # -20dBFS is 10dB above the -30dB threshold; at a 3:1 ratio the compressor itself already
        # pulls that down to just 3.33dB above threshold (-26.67dBFS) BEFORE any boost is added --
        # so reaching the target actually needs MORE boost than a naive flat solve (target minus
        # raw reference, ignoring what compression already did to the signal) would suggest.
        voice = [10 ** (-20 / 20)] * 10
        boost_db, error = a.compute_mic_boost_db_from_samples(voice, target_db=-18.0)
        self.assertIsNone(error)
        naive_flat_solve = -18.0 - -20.0
        self.assertGreater(boost_db, naive_flat_solve)

    def test_result_is_clamped_to_the_sane_range(self):
        very_quiet = [10 ** (-84 / 20)] * 10  # just above the min-signal floor
        boost_db, _error = a.compute_mic_boost_db_from_samples(very_quiet, target_db=-18.0)
        self.assertLessEqual(boost_db, a.MIC_BOOST_CALIBRATION_MAX_BOOST_DB)

        already_loud = [10 ** (10 / 20)] * 10
        boost_db, _error = a.compute_mic_boost_db_from_samples(already_loud, target_db=-18.0)
        self.assertGreaterEqual(boost_db, a.MIC_BOOST_CALIBRATION_MIN_BOOST_DB)

    def test_uses_the_median_so_a_few_loud_words_dont_dominate(self):
        # Mostly soft speech with a few loud words shouldn't drag the calibration toward the loud
        # end -- the median should reflect the typical (soft) level, not the occasional peak.
        voice = [10 ** (-50 / 20)] * 8 + [10 ** (-10 / 20)] * 2
        boost_db, _error = a.compute_mic_boost_db_from_samples(voice, target_db=-18.0)
        # Should land close to the flat solve for the typical (-50dB) level, not the loud one.
        self.assertGreater(boost_db, 20)


class MulToDbAndPercentileTests(unittest.TestCase):
    def test_mul_to_db_zero_is_a_very_low_floor_not_an_error(self):
        self.assertEqual(a._mul_to_db(0.0), -100.0)

    def test_mul_to_db_full_scale_is_zero_db(self):
        self.assertAlmostEqual(a._mul_to_db(1.0), 0.0, places=6)

    def test_mul_to_db_matches_known_value(self):
        self.assertAlmostEqual(a._mul_to_db(0.1), -20.0, places=6)

    def test_percentile_empty_is_zero(self):
        self.assertEqual(a._percentile([], 0.5), 0.0)

    def test_percentile_zero_is_the_minimum(self):
        self.assertEqual(a._percentile([3, 1, 2], 0.0), 1)

    def test_percentile_one_is_the_maximum(self):
        self.assertEqual(a._percentile([3, 1, 2], 1.0), 3)


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

    def test_result_never_exceeds_the_full_scale_multiplier(self):
        result = a.predict_mic_boost_output_mul(1.0, 200.0)
        self.assertLessEqual(result, 1.0)


class MicBoostVoiceIsolationSettingsTests(unittest.TestCase):
    def test_rnnoise_has_no_other_settings(self):
        self.assertEqual(a.mic_boost_voice_isolation_settings("rnnoise"), {"method": "rnnoise"})

    def test_speex_includes_the_apps_own_lighter_suppress_level(self):
        settings = a.mic_boost_voice_isolation_settings("speex")
        self.assertEqual(settings["method"], "speex")
        self.assertEqual(settings["suppress_level"], a.MIC_BOOST_VOICE_ISOLATION_SPEEX_SUPPRESS_LEVEL_DB)

    def test_unknown_method_falls_back_to_rnnoise(self):
        self.assertEqual(a.mic_boost_voice_isolation_settings("bogus"), {"method": "rnnoise"})


class ApplyMicBoostFromConfigTests(unittest.TestCase):
    def _client(self):
        a._legacy_noise_gate_checked_inputs.clear()  # see EnsureMicBoostFilterTests._client
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

    def test_enabled_applies_the_configured_voice_isolation(self):
        client = self._client()
        a.apply_mic_boost_from_config(
            client,
            {
                "mic_boost": {
                    "enabled": True, "input_name": "Scarlet", "boost_db": 24.0,
                    "voice_isolation": {"enabled": True},
                },
            },
        )
        voice_isolation = client.get_source_filter("Scarlet", a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME)
        self.assertTrue(voice_isolation.filter_enabled)
        self.assertEqual(voice_isolation.filter_settings["method"], "rnnoise")

    def test_configured_method_is_applied(self):
        client = self._client()
        a.apply_mic_boost_from_config(
            client,
            {
                "mic_boost": {
                    "enabled": True, "input_name": "Scarlet", "boost_db": 24.0,
                    "voice_isolation": {"enabled": True, "method": "speex"},
                },
            },
        )
        voice_isolation = client.get_source_filter("Scarlet", a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME)
        self.assertEqual(voice_isolation.filter_settings["method"], "speex")
        self.assertEqual(
            voice_isolation.filter_settings["suppress_level"], a.MIC_BOOST_VOICE_ISOLATION_SPEEX_SUPPRESS_LEVEL_DB,
        )

    def test_configured_eq_is_applied(self):
        client = self._client()
        a.apply_mic_boost_from_config(
            client,
            {
                "mic_boost": {
                    "enabled": True, "input_name": "Scarlet", "boost_db": 24.0,
                    "eq": {"enabled": True, "low_db": -6.0, "mid_db": 1.0, "high_db": 4.0},
                },
            },
        )
        eq = client.get_source_filter("Scarlet", a.MIC_BOOST_EQ_FILTER_NAME)
        self.assertTrue(eq.filter_enabled)
        self.assertEqual(eq.filter_settings, {"low": -6.0, "mid": 1.0, "high": 4.0})

    def test_missing_mic_boost_key_entirely_is_a_noop(self):
        client = self._client()
        a.apply_mic_boost_from_config(client, {})
        self.assertEqual(client.calls, [])


class EnsureMicBoostFilterTests(unittest.TestCase):
    def _client(self):
        # ensure_mic_boost_filter only ever checks for the legacy noise gate ONCE per input_name
        # per app run (see _legacy_noise_gate_checked_inputs) -- cleared here so each test starts
        # fresh instead of a later test silently inheriting an earlier one's "already checked"
        # state for the same "Scarlet" input name.
        a._legacy_noise_gate_checked_inputs.clear()
        return FakeObsClient(inputs={"Scarlet": {"kind": "wasapi_input_capture", "tracks": {}, "settings": {}}})

    def test_no_input_name_is_a_noop(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "", 12.0)
        self.assertEqual(client.calls, [])

    def test_creates_all_four_filters_on_a_fresh_source(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        filters = client.get_source_filter_list("Scarlet").filters
        names = {f.filter_name: f.filter_kind for f in filters}
        self.assertEqual(
            names,
            {
                a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME: "noise_suppress_filter_v2",
                a.MIC_BOOST_EQ_FILTER_NAME: "basic_eq_filter",
                a.MIC_BOOST_COMPRESSOR_FILTER_NAME: "compressor_filter",
                a.MIC_BOOST_LIMITER_FILTER_NAME: "limiter_filter",
            },
        )
        compressor = client.get_source_filter("Scarlet", a.MIC_BOOST_COMPRESSOR_FILTER_NAME)
        self.assertEqual(compressor.filter_settings["output_gain"], 12.0)
        limiter = client.get_source_filter("Scarlet", a.MIC_BOOST_LIMITER_FILTER_NAME)
        self.assertEqual(limiter.filter_settings["threshold"], -1.0)
        # Not enabled unless explicitly opted into -- voice_isolation_enabled/eq_enabled default
        # to False, same as the noise gate they replaced never enabled itself just by existing.
        voice_isolation = client.get_source_filter("Scarlet", a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME)
        self.assertFalse(voice_isolation.filter_enabled)
        self.assertEqual(voice_isolation.filter_settings["method"], "rnnoise")
        eq = client.get_source_filter("Scarlet", a.MIC_BOOST_EQ_FILTER_NAME)
        self.assertFalse(eq.filter_enabled)
        self.assertEqual(
            eq.filter_settings,
            {"low": a.MIC_BOOST_EQ_PRESET_LOW_DB, "mid": a.MIC_BOOST_EQ_PRESET_MID_DB, "high": a.MIC_BOOST_EQ_PRESET_HIGH_DB},
        )
        # EQ sits between Voice Isolation and the Compressor.
        ordered_names = [f.filter_name for f in filters]
        self.assertEqual(
            ordered_names,
            [
                a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME, a.MIC_BOOST_EQ_FILTER_NAME,
                a.MIC_BOOST_COMPRESSOR_FILTER_NAME, a.MIC_BOOST_LIMITER_FILTER_NAME,
            ],
        )

    def test_eq_enabled_with_custom_values(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, eq_enabled=True, eq_low_db=-6.0, eq_mid_db=0.0, eq_high_db=5.0)
        eq = client.get_source_filter("Scarlet", a.MIC_BOOST_EQ_FILTER_NAME)
        self.assertTrue(eq.filter_enabled)
        self.assertEqual(eq.filter_settings, {"low": -6.0, "mid": 0.0, "high": 5.0})

    def test_eq_toggle_and_value_changes_do_not_recreate(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, eq_enabled=True)
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, eq_enabled=False, eq_low_db=-8.0)
        self.assertEqual([c for c in client.calls if c[0] == "create_source_filter"], [])
        eq = client.get_source_filter("Scarlet", a.MIC_BOOST_EQ_FILTER_NAME)
        self.assertFalse(eq.filter_enabled)
        self.assertEqual(eq.filter_settings["low"], -8.0)

    def test_legacy_noise_gate_is_removed_if_present(self):
        # Simulates a source left over from before this app switched from a Noise Gate to voice
        # isolation -- must be actively removed, not just ignored, or it would keep gating the raw
        # signal alongside the new filter.
        client = self._client()
        client.create_source_filter(
            "Scarlet", a._LEGACY_MIC_BOOST_NOISE_GATE_FILTER_NAME, "noise_gate_filter", {"open_threshold": -30.0},
        )
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        names = {f.filter_name for f in client.get_source_filter_list("Scarlet").filters}
        self.assertNotIn(a._LEGACY_MIC_BOOST_NOISE_GATE_FILTER_NAME, names)
        self.assertIn(("remove_source_filter", "Scarlet", a._LEGACY_MIC_BOOST_NOISE_GATE_FILTER_NAME), client.calls)

    def test_no_legacy_noise_gate_present_sends_no_extra_requests(self):
        # The common case (nothing to clean up) must stay a true no-op -- no remove_source_filter
        # call, not even one that would just no-op server-side.
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        self.assertNotIn(
            "remove_source_filter", [c[0] for c in client.calls],
        )

    def test_compressor_created_before_limiter_in_the_chain(self):
        # Ordering matters here: a limiter has to come AFTER the compressor's own output_gain to
        # actually catch what that gain produces, not before it.
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0)
        create_calls = [c for c in client.calls if c[0] == "create_source_filter"]
        self.assertEqual(
            [c[2] for c in create_calls],
            [
                a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME, a.MIC_BOOST_EQ_FILTER_NAME,
                a.MIC_BOOST_COMPRESSOR_FILTER_NAME, a.MIC_BOOST_LIMITER_FILTER_NAME,
            ],
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

    def test_voice_isolation_created_enabled_and_moved_to_front(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=True)
        voice_isolation = client.get_source_filter("Scarlet", a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME)
        self.assertEqual(voice_isolation.filter_settings["method"], "rnnoise")
        self.assertTrue(voice_isolation.filter_enabled)
        names = [f.filter_name for f in client.get_source_filter_list("Scarlet").filters]
        self.assertEqual(names[0], a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME)

    def test_voice_isolation_disabled_toggle_is_synced_even_though_the_filter_stays(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=True)
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=False)
        voice_isolation = client.get_source_filter("Scarlet", a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME)
        self.assertFalse(voice_isolation.filter_enabled)  # toggled off, but not deleted

    def test_voice_isolation_reenabled_restores_without_recreating(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=True)
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=False)
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=True)
        self.assertEqual(
            [c for c in client.calls if c[0] == "create_source_filter"], [],  # never recreated
        )
        self.assertIn(("set_source_filter_enabled", "Scarlet", a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME, True), client.calls)

    def test_unchanged_voice_isolation_state_is_not_rewritten(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=True)
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=True)
        self.assertEqual(client.calls, [])

    def test_switching_method_updates_settings_without_recreating(self):
        client = self._client()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=True, voice_isolation_method="rnnoise")
        client.calls.clear()
        a.ensure_mic_boost_filter(client, "Scarlet", 12.0, voice_isolation_enabled=True, voice_isolation_method="speex")
        self.assertEqual([c for c in client.calls if c[0] == "create_source_filter"], [])  # never recreated
        voice_isolation = client.get_source_filter("Scarlet", a.MIC_BOOST_VOICE_ISOLATION_FILTER_NAME)
        self.assertEqual(voice_isolation.filter_settings["method"], "speex")
        self.assertEqual(
            voice_isolation.filter_settings["suppress_level"], a.MIC_BOOST_VOICE_ISOLATION_SPEEX_SUPPRESS_LEVEL_DB,
        )


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
    # add_recording_marker's primary mechanism is its own recording_state["markers"] list (read
    # from OBS's own GetRecordStatus elapsed time, not tied to recording format at all) --
    # CreateRecordChapter is only ever a best-effort bonus now, never required for success. See
    # CROSS_PLATFORM_PLAN.md §5.21 for why that forcing was removed.
    def test_success_appends_elapsed_seconds_to_recording_state(self):
        client = FakeObsClient()
        client.record_duration_ms = 12500
        recording_state = {}
        result = a.add_recording_marker(client, recording_state=recording_state)
        self.assertTrue(result)
        self.assertEqual(recording_state["markers"], [12.5])

    def test_multiple_markers_accumulate_in_order(self):
        client = FakeObsClient()
        recording_state = {}
        client.record_duration_ms = 1000
        a.add_recording_marker(client, recording_state=recording_state)
        client.record_duration_ms = 5000
        a.add_recording_marker(client, recording_state=recording_state)
        self.assertEqual(recording_state["markers"], [1.0, 5.0])

    def test_none_recording_state_does_not_raise(self):
        client = FakeObsClient()
        result = a.add_recording_marker(client, recording_state=None)
        self.assertTrue(result)

    def test_also_adds_a_native_obs_chapter_on_hybrid_mp4(self):
        client = FakeObsClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        a.add_recording_marker(client, recording_state={})
        self.assertIn(("create_record_chapter", None), client.calls)

    def test_skips_the_native_chapter_on_other_formats(self):
        # OBS rejects it anywhere else, and obsws_python logs that rejection as an ERROR with a
        # traceback on every marker before it can be caught.
        client = FakeObsClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "mkv")
        recording_state = {}
        self.assertTrue(a.add_recording_marker(client, recording_state=recording_state))
        self.assertNotIn(("create_record_chapter", None), client.calls)
        self.assertEqual(recording_state["markers"], [0.0])

    def test_unsupported_format_is_non_fatal(self):
        # A non-Hybrid-MP4 recording is the common case now that markers don't need that format
        # at all -- CreateRecordChapter failing this way must not affect the overall result.
        class RejectingClient(FakeObsClient):
            def create_record_chapter(self, chapter_name=None):
                raise a.obsws.error.OBSSDKRequestError("CreateRecordChapter", a.OBS_CHAPTER_NOT_SUPPORTED_CODE, "")

        client = RejectingClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        recording_state = {}
        result = a.add_recording_marker(client, recording_state=recording_state)
        self.assertTrue(result)
        self.assertEqual(recording_state["markers"], [0.0])

    def test_other_create_record_chapter_error_is_also_non_fatal(self):
        class RejectingClient(FakeObsClient):
            def create_record_chapter(self, chapter_name=None):
                raise a.obsws.error.OBSSDKRequestError("CreateRecordChapter", 500, "")

        client = RejectingClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        result = a.add_recording_marker(client, recording_state={})
        self.assertTrue(result)

    def test_cannot_read_record_status_returns_false_without_recording_a_marker(self):
        class BrokenClient(FakeObsClient):
            def get_record_status(self):
                raise RuntimeError("boom")

        client = BrokenClient()
        recording_state = {}
        result = a.add_recording_marker(client, recording_state=recording_state)
        self.assertFalse(result)
        self.assertNotIn("markers", recording_state)

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
    # set_game_audio_capture_target's own orchestration (repoint-vs-create, error handling) is
    # OS-agnostic -- mocking platform_common's two dispatch functions with Windows-shaped return
    # values makes these tests exercise that same orchestration regardless of which real OS runs
    # them, per platform_common.py's own testing philosophy (CROSS_PLATFORM_PLAN.md §3.2). The
    # platform backends themselves (Windows/macOS/Linux) have their own dedicated tests.
    def setUp(self):
        patcher1 = unittest.mock.patch.object(
            a.platform_common, "process_audio_capture_kind", return_value="wasapi_process_output_capture",
        )
        patcher2 = unittest.mock.patch.object(
            a.platform_common, "process_audio_capture_settings",
            side_effect=lambda process_name, exe_path: {
                "window": f"::{process_name}", "priority": a.WINDOW_MATCH_PRIORITY_EXE_FALLBACK,
            },
        )
        patcher1.start()
        patcher2.start()
        self.addCleanup(patcher1.stop)
        self.addCleanup(patcher2.stop)

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

    def test_force_reattach_changes_settings_before_restoring_them(self):
        # OBS ignores a re-send of identical settings, so a capture bound to a process that has
        # since restarted (Discord auto-update) stays bound to the dead one. Forcing a re-attach
        # has to make OBS see a real change first, then land on the real target.
        settings = {"window": "::Discord.exe", "priority": a.WINDOW_MATCH_PRIORITY_EXE_FALLBACK}
        client = FakeObsClient(inputs={
            "Discord": {"kind": "wasapi_process_output_capture", "tracks": {}, "settings": dict(settings)},
        })
        a.set_game_audio_capture_target(client, "Discord", "Discord.exe", force_reattach=True)
        sent = [c[2]["window"] for c in client.calls if c[0] == "set_input_settings"]
        self.assertEqual(sent, [f"::{a.REATTACH_PLACEHOLDER_PROCESS_NAME}", "::Discord.exe"])
        self.assertEqual(client.inputs["Discord"]["settings"], settings)

    def test_force_reattach_still_creates_a_missing_input(self):
        client = FakeObsClient()
        a.set_game_audio_capture_target(client, "Discord", "Discord.exe", force_reattach=True)
        self.assertEqual(client.inputs["Discord"]["settings"]["window"], "::Discord.exe")

    def test_no_reattach_by_default(self):
        # Game Audio is re-pointed after recording has started -- forcing a restart there would
        # cut the start of the game-audio track, so it stays opt-in.
        client = FakeObsClient(inputs={
            "Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {}, "settings": {}},
        })
        a.set_game_audio_capture_target(client, "Game Audio", "Balatro.exe")
        self.assertEqual(len([c for c in client.calls if c[0] == "set_input_settings"]), 1)


class ResolveProcessAudioCaptureTests(unittest.TestCase):
    def test_returns_none_kind_when_os_has_no_capture_kind(self):
        with unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value=None):
            kind, settings = a.resolve_process_audio_capture("Balatro.exe")
        self.assertIsNone(kind)
        self.assertIsNone(settings)

    def test_returns_none_settings_when_process_cannot_be_resolved(self):
        with unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value="sck_audio_capture"), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_settings", return_value=None):
            kind, settings = a.resolve_process_audio_capture("Balatro")
        self.assertEqual(kind, "sck_audio_capture")
        self.assertIsNone(settings)

    def test_looks_up_the_live_exe_path_for_a_running_process(self):
        # Only macOS's real backend actually uses exe_path (to resolve a bundle identifier), but
        # this orchestration function should always try to find it regardless of OS, rather than
        # assuming it's never needed.
        fake_processes = [("Balatro", "/Applications/Balatro.app/Contents/MacOS/Balatro", 123)]
        captured = {}

        def fake_settings(process_name, exe_path):
            captured["process_name"] = process_name
            captured["exe_path"] = exe_path
            return {"type": 1, "application": "com.playstack.balatro"}

        with unittest.mock.patch.object(a, "get_running_processes", return_value=fake_processes), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value="sck_audio_capture"), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_settings", side_effect=fake_settings):
            kind, settings = a.resolve_process_audio_capture("Balatro")
        self.assertEqual(kind, "sck_audio_capture")
        self.assertEqual(settings, {"type": 1, "application": "com.playstack.balatro"})
        self.assertEqual(captured["exe_path"], "/Applications/Balatro.app/Contents/MacOS/Balatro")

    def test_exe_path_is_none_when_process_is_not_running(self):
        captured = {}

        def fake_settings(process_name, exe_path):
            captured["exe_path"] = exe_path
            return None

        with unittest.mock.patch.object(a, "get_running_processes", return_value=[]), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value="sck_audio_capture"), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_settings", side_effect=fake_settings):
            a.resolve_process_audio_capture("Balatro")
        self.assertIsNone(captured["exe_path"])

    def test_logs_and_leaves_input_unchanged_when_os_has_no_capture_kind(self):
        client = FakeObsClient()
        with unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value=None):
            with self.assertLogs(level="WARNING"):
                a.set_game_audio_capture_target(client, "Game Audio", "Balatro")  # must not raise
        self.assertEqual(client.calls, [])

    def test_logs_and_leaves_input_unchanged_when_process_cannot_be_resolved(self):
        client = FakeObsClient()
        with unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value="sck_audio_capture"), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_settings", return_value=None):
            with self.assertLogs(level="WARNING"):
                a.set_game_audio_capture_target(client, "Game Audio", "Balatro")  # must not raise
        self.assertEqual(client.calls, [])


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


class RecordingMarkersSidecarTests(unittest.TestCase):
    # write_recording_markers/read_recording_markers back the clip editor's own marker display
    # (see refresh_markers) independently of OBS's recording format -- see
    # CROSS_PLATFORM_PLAN.md §5.21.
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.recording_path = os.path.join(self._tmp_ctx.name, "Balatro - 2026-10-06 09-53-29.mov")
        self.addCleanup(self._tmp_ctx.cleanup)

    def test_sidecar_path_appends_suffix(self):
        self.assertEqual(a.marker_sidecar_path(self.recording_path), self.recording_path + ".markers.json")

    def test_read_returns_none_when_no_sidecar_written(self):
        self.assertIsNone(a.read_recording_markers(self.recording_path))

    def test_write_then_read_round_trips_sorted_markers(self):
        a.write_recording_markers(self.recording_path, [47.0, 12.5, 103.2])
        self.assertEqual(a.read_recording_markers(self.recording_path), [12.5, 47.0, 103.2])

    def test_empty_markers_list_writes_no_sidecar_at_all(self):
        a.write_recording_markers(self.recording_path, [])
        self.assertFalse(os.path.isfile(a.marker_sidecar_path(self.recording_path)))
        self.assertIsNone(a.read_recording_markers(self.recording_path))

    def test_none_markers_does_not_raise(self):
        a.write_recording_markers(self.recording_path, None)  # must not raise
        self.assertIsNone(a.read_recording_markers(self.recording_path))

    def test_none_recording_path_does_not_raise(self):
        a.write_recording_markers(None, [1.0])  # must not raise

    def test_corrupt_sidecar_returns_none_instead_of_raising(self):
        with open(a.marker_sidecar_path(self.recording_path), "w", encoding="utf-8") as f:
            f.write("not valid json")
        self.assertIsNone(a.read_recording_markers(self.recording_path))

    def test_non_numeric_markers_returns_none_instead_of_raising(self):
        with open(a.marker_sidecar_path(self.recording_path), "w", encoding="utf-8") as f:
            json.dump({"markers": ["not", "numbers"]}, f)
        self.assertIsNone(a.read_recording_markers(self.recording_path))


class MarkersFolderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.recordings = os.path.join(self._tmp.name, "Movies")
        self.markers = os.path.join(self._tmp.name, "Markers")
        os.makedirs(self.recordings)
        self.recording = os.path.join(self.recordings, "Balatro - 2026-10-08 13-27-41.mkv")

    def test_configured_folder_holds_the_file_named_after_the_recording(self):
        self.assertEqual(
            a.marker_sidecar_path(self.recording, self.markers),
            os.path.join(self.markers, "Balatro - 2026-10-08 13-27-41.mkv.markers.json"),
        )

    def test_writes_into_the_configured_folder_creating_it(self):
        a.write_recording_markers(self.recording, [5.0], self.markers)
        self.assertTrue(os.path.isfile(a.marker_sidecar_path(self.recording, self.markers)))
        self.assertFalse(os.path.isfile(a.marker_sidecar_path(self.recording)))
        self.assertEqual(a.read_recording_markers(self.recording, self.markers), [5.0])

    def test_reads_markers_saved_next_to_the_recording_before_the_folder_was_set(self):
        a.write_recording_markers(self.recording, [3.0, 1.0])
        self.assertEqual(a.read_recording_markers(self.recording, self.markers), [1.0, 3.0])

    def test_configured_folder_wins_over_an_older_file_next_to_the_recording(self):
        a.write_recording_markers(self.recording, [1.0])
        a.write_recording_markers(self.recording, [9.0], self.markers)
        self.assertEqual(a.read_recording_markers(self.recording, self.markers), [9.0])

    def test_none_when_neither_exists(self):
        self.assertIsNone(a.read_recording_markers(self.recording, self.markers))


class ResolveRecordingResolutionTests(unittest.TestCase):
    def test_match_canvas_returns_base_resolution_unchanged(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "Match canvas (no scaling)"), (1920, 1080))

    def test_unrecognized_choice_falls_back_to_base_resolution(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "nonsense"), (1920, 1080))

    def test_1080p_preset_keeps_canvas_aspect_ratio(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "1080p"), (1920, 1080))

    def test_720p_preset_scales_width_from_canvas_aspect_ratio(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "720p"), (1280, 720))

    def test_ultrawide_canvas_rounds_width_to_a_multiple_of_4(self):
        # A canvas whose ratio doesn't divide evenly, to confirm the rounding actually does
        # something -- OBS itself rounds output sizes to multiples of 4.
        width, height = a.resolve_recording_resolution(3441, 1440, "720p")
        self.assertEqual(height, 720)
        self.assertEqual(width % 4, 0)


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


class NotifyTests(unittest.TestCase):
    def test_disabled_notifications_do_nothing(self):
        icon = unittest.mock.Mock()
        a.notify(icon, {"enabled": False}, "t", "m")
        icon.notify.assert_not_called()

    def test_shown_immediately_by_default(self):
        icon = unittest.mock.Mock()
        a.notify(icon, {"enabled": True}, "Recording started", "Balatro")
        icon.notify.assert_called_once_with("Balatro", "Recording started")

    def test_post_game_notification_is_delayed_on_macos(self):
        # Confirmed live: macOS hides banners while a fullscreen game's Space is closing.
        icon = unittest.mock.Mock()
        with unittest.mock.patch.object(a.sys, "platform", "darwin"), \
                unittest.mock.patch.object(a.threading, "Timer") as mock_timer:
            a.notify(icon, {"enabled": True}, "Recording stopped", "Balatro", after_game_exit=True)
        icon.notify.assert_not_called()
        self.assertEqual(mock_timer.call_args[0][0], a.MACOS_POST_GAME_NOTIFICATION_DELAY_SECONDS)
        mock_timer.call_args[0][1]()
        icon.notify.assert_called_once_with("Balatro", "Recording stopped")

    def test_post_game_notification_is_immediate_elsewhere(self):
        icon = unittest.mock.Mock()
        with unittest.mock.patch.object(a.sys, "platform", "win32"):
            a.notify(icon, {"enabled": True}, "Recording stopped", "Balatro", after_game_exit=True)
        icon.notify.assert_called_once()

    def test_failures_are_logged_as_warnings(self):
        icon = unittest.mock.Mock()
        icon.notify.side_effect = RuntimeError("boom")
        with self.assertLogs(level="WARNING"):
            a.notify(icon, {"enabled": True}, "t", "m")


class MakeObsClientThreadSafeTests(unittest.TestCase):
    # Confirmed live: the custom-keybind listener thread and the main watcher loop both issuing a
    # request on the same shared client at the same moment crossed wires -- one caller (Add
    # Marker's get_record_status()) got back an entirely different call's response
    # (GetSourceScreenshotDataclass, from the frozen-capture health check's periodic screenshot
    # request). make_obs_client_thread_safe's whole job is making that interleaving impossible.

    def test_concurrent_calls_from_two_threads_never_overlap(self):
        client = unittest.mock.Mock()
        intervals = []
        intervals_lock = threading.Lock()

        def fake_send(request_name):
            start = time.monotonic()
            time.sleep(0.05)  # long enough that two real threads would overlap without the lock
            end = time.monotonic()
            with intervals_lock:
                intervals.append((start, end))
            return request_name

        client.send = fake_send
        a.make_obs_client_thread_safe(client)

        threads = [threading.Thread(target=client.send, args=(f"Request{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(intervals), 4)
        intervals.sort()
        for (_s1, e1), (s2, _e2) in zip(intervals, intervals[1:]):
            self.assertLessEqual(e1, s2)  # no overlap: each call fully finishes before the next starts

    def test_return_value_and_arguments_pass_through_unchanged(self):
        client = unittest.mock.Mock()
        underlying_send = unittest.mock.Mock(return_value="the real response")
        client.send = underlying_send
        a.make_obs_client_thread_safe(client)
        result = client.send("GetRecordStatus", {"some": "data"})
        self.assertEqual(result, "the real response")
        underlying_send.assert_called_once_with("GetRecordStatus", {"some": "data"})

    def test_a_raised_exception_still_releases_the_lock(self):
        client = unittest.mock.Mock()

        def failing_send(*args, **kwargs):
            raise RuntimeError("boom")

        client.send = failing_send
        a.make_obs_client_thread_safe(client)
        with self.assertRaises(RuntimeError):
            client.send("GetRecordStatus")
        # If the lock leaked held, this second call would hang forever instead of also raising.
        with self.assertRaises(RuntimeError):
            client.send("GetRecordStatus")
