import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
from tests.fakes import FakeObsClient


class CommonAudioAppsDataTests(unittest.TestCase):
    """Sanity checks on the curated app list itself -- a typo'd or missing category here would
    silently break the wizard (KeyError deep inside a button click) without these."""

    def test_every_app_has_a_known_category(self):
        for app in a.COMMON_AUDIO_APPS:
            self.assertIn(app["category"], a.QUICK_SETUP_CATEGORY_TRACKS, app["name"])

    def test_app_names_are_unique(self):
        names = [app["name"] for app in a.COMMON_AUDIO_APPS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_app_has_a_process_name(self):
        for app in a.COMMON_AUDIO_APPS:
            self.assertTrue(app["process_name"].lower().endswith(".exe"), app["name"])


class ComputeQuickSetupTracksTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeObsClient(inputs={
            "Desktop Audio": {"kind": "wasapi_output_capture", "tracks": {str(i): False for i in range(1, 7)}},
            "Scarlet": {"kind": "wasapi_input_capture", "tracks": {str(i): False for i in range(1, 7)}},
            "Discord": {"kind": "wasapi_process_output_capture", "tracks": {str(i): False for i in range(1, 7)}},
        })
        self.discord = next(app for app in a.COMMON_AUDIO_APPS if app["name"] == "Discord")
        self.spotify = next(app for app in a.COMMON_AUDIO_APPS if app["name"] == "Spotify")

    def test_desktop_and_mic_route_to_tracks_one_and_two(self):
        tracks, created, app_captures = a.compute_quick_setup_tracks(self.client, "Desktop Audio", "Scarlet", None, [])
        self.assertIn({"input_name": "Desktop Audio", "track": 1}, tracks)
        self.assertIn({"input_name": "Scarlet", "track": 1}, tracks)
        self.assertIn({"input_name": "Scarlet", "track": 2}, tracks)
        self.assertEqual(created, [])
        self.assertEqual(app_captures, {})

    def test_none_desktop_and_mic_are_skipped(self):
        tracks, created, app_captures = a.compute_quick_setup_tracks(self.client, None, None, None, [])
        self.assertEqual(tracks, [])
        self.assertEqual(created, [])
        self.assertEqual(app_captures, {})

    def test_game_audio_routes_to_track_three(self):
        tracks, _, _ = a.compute_quick_setup_tracks(self.client, None, None, "Game Audio", [])
        self.assertEqual(tracks, [{"input_name": "Game Audio", "track": 3}])

    def test_existing_app_input_is_reused_not_recreated(self):
        tracks, created, app_captures = a.compute_quick_setup_tracks(self.client, None, None, None, [self.discord])
        self.assertEqual(created, [])
        self.assertEqual(tracks, [{"input_name": "Discord", "track": 4}])
        self.assertNotIn(("create_input", "Screen", "Discord", "wasapi_process_output_capture", {"window": "::Discord.exe", "priority": 2}), self.client.calls)
        # Reported even though it already existed -- the caller re-points existing app captures
        # every recording start to self-heal drift, not just newly-created ones.
        self.assertEqual(app_captures, {"Discord": "Discord.exe"})

    def test_missing_app_input_is_created_and_routed(self):
        tracks, created, app_captures = a.compute_quick_setup_tracks(self.client, None, None, None, [self.spotify])
        self.assertEqual(created, ["Spotify"])
        self.assertEqual(tracks, [{"input_name": "Spotify", "track": 5}])
        self.assertIn("Spotify", self.client.inputs)
        self.assertEqual(self.client.inputs["Spotify"]["kind"], "wasapi_process_output_capture")
        self.assertEqual(app_captures, {"Spotify": "Spotify.exe"})

    def test_created_input_targets_the_apps_exe_by_window_setting(self):
        a.compute_quick_setup_tracks(self.client, None, None, None, [self.spotify])
        create_calls = [c for c in self.client.calls if c[0] == "create_input"]
        self.assertEqual(len(create_calls), 1)
        _, scene, name, kind, settings = create_calls[0]
        self.assertEqual(name, "Spotify")
        self.assertEqual(kind, "wasapi_process_output_capture")
        self.assertEqual(settings["window"], "::Spotify.exe")

    def test_full_scheme_matches_the_six_track_layout(self):
        chrome = next(app for app in a.COMMON_AUDIO_APPS if app["name"] == "Chrome")
        tracks, created, app_captures = a.compute_quick_setup_tracks(
            self.client, "Desktop Audio", "Scarlet", "Game Audio", [self.discord, self.spotify, chrome]
        )
        by_track = {}
        for entry in tracks:
            by_track.setdefault(entry["track"], set()).add(entry["input_name"])
        self.assertEqual(by_track[1], {"Desktop Audio", "Scarlet"})
        self.assertEqual(by_track[2], {"Scarlet"})
        self.assertEqual(by_track[3], {"Game Audio"})
        self.assertEqual(by_track[4], {"Discord"})
        self.assertEqual(by_track[5], {"Spotify"})
        self.assertEqual(by_track[6], {"Chrome"})
        self.assertEqual(set(created), {"Spotify", "Chrome"})  # Discord already existed
        self.assertEqual(
            app_captures, {"Discord": "Discord.exe", "Spotify": "Spotify.exe", "Chrome": "chrome.exe"}
        )


if __name__ == "__main__":
    unittest.main()
