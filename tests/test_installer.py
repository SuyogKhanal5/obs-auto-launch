import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import installer


def base_options(**overrides):
    options = {
        "split_long_recordings": False,
        "delete_short_clips": True,
        "notifications": True,
        "per_game_folders": False,
        "multi_track_audio": False,
        "output_folder": "",
        "game_audio_isolation": False,
        "replay_buffer": False,
        "disk_space_guard": False,
        "disk_space_guard_min_gb": "",
    }
    options.update(overrides)
    return options


class BuildConfigTests(unittest.TestCase):
    def setUp(self):
        template_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.json")
        with open(template_path, encoding="utf-8") as f:
            self.template = json.load(f)

    def test_watched_games_and_windows_start_empty(self):
        config = installer.build_config(self.template, None, "pw", base_options())
        self.assertEqual(config["watched_games"], [])
        self.assertEqual(config["watched_windows"], [])

    def test_advanced_options_all_apply_when_enabled(self):
        options = base_options(
            multi_track_audio=True, output_folder=r"D:\Recs", game_audio_isolation=True,
            replay_buffer=True, disk_space_guard=True, disk_space_guard_min_gb="25",
        )
        config = installer.build_config(self.template, None, "pw", options)
        self.assertTrue(config["obs"]["multi_track_audio"]["enabled"])
        self.assertEqual(config["obs"]["output_folder"], r"D:\Recs")
        self.assertTrue(config["obs"]["game_audio_capture"]["enabled"])
        self.assertTrue(config["obs"]["replay_buffer"]["enabled"])
        self.assertEqual(config["disk_space_guard"], {"enabled": True, "minimum_free_gb": 25.0})

    def test_advanced_options_default_off(self):
        config = installer.build_config(self.template, None, "pw", base_options())
        self.assertFalse(config["obs"]["multi_track_audio"]["enabled"])
        self.assertFalse(config["obs"]["game_audio_capture"]["enabled"])
        self.assertFalse(config["obs"]["replay_buffer"]["enabled"])
        self.assertFalse(config["disk_space_guard"]["enabled"])
        self.assertNotIn("output_folder", config["obs"])

    def test_blank_output_folder_is_omitted_not_written_as_empty_string(self):
        config = installer.build_config(self.template, None, "pw", base_options(output_folder=""))
        self.assertNotIn("output_folder", config["obs"])

    def test_blank_disk_guard_minimum_falls_back_to_template_default(self):
        options = base_options(disk_space_guard=True, disk_space_guard_min_gb="")
        config = installer.build_config(self.template, None, "pw", options)
        self.assertEqual(config["disk_space_guard"]["minimum_free_gb"], self.template["disk_space_guard"]["minimum_free_gb"])

    def test_invalid_disk_guard_minimum_falls_back_to_template_default_without_raising(self):
        options = base_options(disk_space_guard=True, disk_space_guard_min_gb="not a number")
        config = installer.build_config(self.template, None, "pw", options)  # must not raise
        self.assertEqual(config["disk_space_guard"]["minimum_free_gb"], self.template["disk_space_guard"]["minimum_free_gb"])

    def test_obs_path_falls_back_to_template_when_not_found(self):
        config = installer.build_config(self.template, None, "pw", base_options())
        self.assertEqual(config["obs"]["path"], self.template["obs"]["path"])

    def test_obs_path_uses_detected_path_when_given(self):
        config = installer.build_config(self.template, r"C:\OBS\obs64.exe", "pw", base_options())
        self.assertEqual(config["obs"]["path"], r"C:\OBS\obs64.exe")

    def test_password_is_written_into_websocket_settings(self):
        config = installer.build_config(self.template, None, "generated-pw", base_options())
        self.assertEqual(config["obs"]["websocket"]["password"], "generated-pw")


if __name__ == "__main__":
    unittest.main()
