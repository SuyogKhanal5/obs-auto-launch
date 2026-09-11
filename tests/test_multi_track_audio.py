import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
from tests.fakes import FakeObsClient


class NormalizeTrackEntriesTests(unittest.TestCase):
    def test_keeps_valid_entries(self):
        tracks = [{"input_name": "Mic", "track": 1}, {"input_name": "Desktop Audio", "track": 2}]
        self.assertEqual(a.normalize_track_entries(tracks), [("Mic", 1), ("Desktop Audio", 2)])

    def test_drops_entries_with_missing_input_name(self):
        self.assertEqual(a.normalize_track_entries([{"track": 1}]), [])
        self.assertEqual(a.normalize_track_entries([{"input_name": "", "track": 1}]), [])

    def test_drops_entries_with_invalid_track_number(self):
        tracks = [
            {"input_name": "Mic", "track": 0},
            {"input_name": "Mic", "track": 7},
            {"input_name": "Mic", "track": "not a number"},
            {"input_name": "Mic"},
        ]
        self.assertEqual(a.normalize_track_entries(tracks), [])

    def test_coerces_string_track_numbers(self):
        self.assertEqual(a.normalize_track_entries([{"input_name": "Mic", "track": "3"}]), [("Mic", 3)])

    def test_none_or_empty_input(self):
        self.assertEqual(a.normalize_track_entries(None), [])
        self.assertEqual(a.normalize_track_entries([]), [])


class ApplyMultiTrackRoutingTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeObsClient(inputs={
            "Desktop Audio": {"kind": "wasapi_output_capture", "tracks": {str(i): False for i in range(1, 7)}},
            "Scarlet": {"kind": "wasapi_input_capture", "tracks": {str(i): False for i in range(1, 7)}},
            "Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {str(i): False for i in range(1, 7)}},
            # Fifine is deliberately left routed to track 6 from some earlier manual setup, and
            # is NOT going to be listed in entries below -- this is the exact "stale mapping left
            # behind after removing an input from the list" scenario the app hit for real.
            "Fifine": {"kind": "wasapi_input_capture", "tracks": {**{str(i): False for i in range(1, 7)}, "6": True}},
            # Non-audio-kind input; must never be touched even though it's unlisted.
            "Display Capture": {"kind": "monitor_capture", "tracks": {**{str(i): False for i in range(1, 7)}, "1": True}},
        })
        self.entries = [
            ("Desktop Audio", 1),
            ("Scarlet", 1),
            ("Scarlet", 2),
            ("Game Audio", 3),
        ]

    def test_routes_listed_inputs_to_exact_tracks(self):
        a.apply_multi_track_routing(self.client, self.entries)
        self.assertEqual(
            self.client.inputs["Desktop Audio"]["tracks"],
            {"1": True, "2": False, "3": False, "4": False, "5": False, "6": False},
        )
        self.assertEqual(
            self.client.inputs["Scarlet"]["tracks"],
            {"1": True, "2": True, "3": False, "4": False, "5": False, "6": False},
        )
        self.assertEqual(
            self.client.inputs["Game Audio"]["tracks"],
            {"1": False, "2": False, "3": True, "4": False, "5": False, "6": False},
        )

    def test_clears_stale_routing_on_unlisted_audio_input(self):
        a.apply_multi_track_routing(self.client, self.entries)
        self.assertEqual(self.client.inputs["Fifine"]["tracks"], {str(i): False for i in range(1, 7)})

    def test_leaves_non_audio_kind_input_untouched(self):
        a.apply_multi_track_routing(self.client, self.entries)
        # Display Capture is a video source; even though it's unlisted, its kind isn't in
        # AUDIO_TRACK_MANAGED_KINDS so it should never be called with SetInputAudioTracks.
        self.assertEqual(self.client.inputs["Display Capture"]["tracks"]["1"], True)

    def test_skips_nonexistent_input_without_raising(self):
        entries = self.entries + [("Nonexistent Input", 4)]
        with self.assertLogs(level="WARNING"):
            a.apply_multi_track_routing(self.client, entries)  # must not raise
        self.assertNotIn("Nonexistent Input", self.client.inputs)

    def test_input_listed_on_multiple_tracks_via_repeated_entries(self):
        a.apply_multi_track_routing(self.client, [("Scarlet", 1), ("Scarlet", 2), ("Scarlet", 4)])
        self.assertEqual(
            self.client.inputs["Scarlet"]["tracks"],
            {"1": True, "2": True, "3": False, "4": True, "5": False, "6": False},
        )


class SyncMultiTrackOutputSettingsTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeObsClient()

    def test_switches_simple_mode_to_advanced(self):
        with self.assertLogs(level="WARNING"):
            a.sync_multi_track_output_settings(self.client, [("Mic", 1)], "OBS Auto Recorder")
        self.assertEqual(a.get_profile_parameter_value(self.client, "Output", "Mode"), "Advanced")

    def test_leaves_advanced_mode_alone(self):
        self.client.set_profile_parameter("Output", "Mode", "Advanced")
        self.client.calls.clear()
        a.sync_multi_track_output_settings(self.client, [("Mic", 1)], "OBS Auto Recorder")
        self.assertNotIn(("set_profile_parameter", "Output", "Mode", "Advanced"), self.client.calls)

    def test_computes_correct_track_bitmask(self):
        # tracks 1, 2 and 4 -> bits 0, 1, 3 -> 1 + 2 + 8 = 11
        a.sync_multi_track_output_settings(self.client, [("Mic", 1), ("Desktop", 2), ("Discord", 4)], "P")
        self.assertEqual(a.get_profile_parameter_value(self.client, "AdvOut", "RecTracks"), "11")

    def test_warns_on_unreliable_recording_format(self):
        self.client.set_profile_parameter("AdvOut", "RecFormat2", "mp4")
        with self.assertLogs(level="WARNING") as cm:
            a.sync_multi_track_output_settings(self.client, [("Mic", 1)], "P")
        self.assertTrue(any("recording format" in msg for msg in cm.output))

    def test_no_warning_for_mkv_format(self):
        self.client.set_profile_parameter("AdvOut", "RecFormat2", "mkv")
        # Advanced-mode switch still logs a warning on a fresh Simple-mode profile; isolate the
        # format-specific warning by starting already in Advanced mode.
        self.client.set_profile_parameter("Output", "Mode", "Advanced")
        with self.assertRaises(AssertionError):
            with self.assertLogs(level="WARNING"):
                a.sync_multi_track_output_settings(self.client, [("Mic", 1)], "P")


class EnsureDedicatedProfileTests(unittest.TestCase):
    def test_creates_profile_and_clones_settings_from_source(self):
        client = FakeObsClient(profile_name="My Streaming Profile")
        client.profiles["My Streaming Profile"]["record_directory"] = r"D:\Recordings"
        client.profiles["My Streaming Profile"]["video"].base_width = 2560

        result = a.ensure_dedicated_profile(client, "OBS Auto Recorder")

        self.assertTrue(result)
        self.assertEqual(client.current_profile, "OBS Auto Recorder")
        self.assertEqual(client.profiles["OBS Auto Recorder"]["record_directory"], r"D:\Recordings")
        self.assertEqual(client.profiles["OBS Auto Recorder"]["video"].base_width, 2560)

    def test_switches_to_existing_dedicated_profile_without_recreating(self):
        client = FakeObsClient(profile_name="Main")
        client.profiles["OBS Auto Recorder"] = client.profiles["Main"]
        client.current_profile = "Main"

        a.ensure_dedicated_profile(client, "OBS Auto Recorder")

        self.assertEqual(client.current_profile, "OBS Auto Recorder")
        self.assertNotIn(("create_profile", "OBS Auto Recorder"), client.calls)

    def test_noop_when_already_on_dedicated_profile(self):
        client = FakeObsClient(profile_name="OBS Auto Recorder")
        a.ensure_dedicated_profile(client, "OBS Auto Recorder")
        self.assertEqual(client.calls, [])


class SyncMultiTrackAudioAppCapturesTests(unittest.TestCase):
    """obs.game_audio_capture's input gets re-pointed at its target on every recording start,
    which self-heals it if OBS ever silently rewrites its exe-only window match into a specific
    (and eventually stale) window title. Quick-Setup-created app captures (Discord, Spotify, ...)
    used to only get that treatment once, at creation -- meaning the exact same OBS-side drift
    would leave them silently broken until someone noticed and fixed it by hand. This checks
    sync_multi_track_audio now re-asserts every configured app_captures entry on every call."""

    def test_repoints_existing_app_capture_every_time(self):
        client = FakeObsClient(
            profile_name="OBS Auto Recorder",
            inputs={
                "Discord": {
                    "kind": "wasapi_process_output_capture",
                    "tracks": {str(i): False for i in range(1, 7)},
                    "settings": {"window": "#some-stale-channel-name:Chrome_WidgetWin_1:Discord.exe", "priority": 2},
                },
            },
        )
        multi_track_config = {
            "enabled": True,
            "tracks": [{"input_name": "Discord", "track": 4}],
            "app_captures": [{"input_name": "Discord", "process_name": "Discord.exe"}],
        }
        a.sync_multi_track_audio(client, multi_track_config)
        self.assertEqual(client.inputs["Discord"]["settings"]["window"], "::Discord.exe")

    def test_missing_app_capture_input_is_created_not_skipped(self):
        client = FakeObsClient(profile_name="OBS Auto Recorder")
        multi_track_config = {
            "enabled": True,
            "tracks": [{"input_name": "Spotify", "track": 5}],
            "app_captures": [{"input_name": "Spotify", "process_name": "Spotify.exe"}],
        }
        a.sync_multi_track_audio(client, multi_track_config)
        self.assertEqual(client.inputs["Spotify"]["settings"]["window"], "::Spotify.exe")

    def test_no_app_captures_configured_does_not_raise(self):
        client = FakeObsClient(profile_name="OBS Auto Recorder")
        multi_track_config = {"enabled": True, "tracks": [{"input_name": "Desktop Audio", "track": 1}]}
        a.sync_multi_track_audio(client, multi_track_config)  # must not raise


if __name__ == "__main__":
    unittest.main()
