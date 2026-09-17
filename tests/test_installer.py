import json
import os
import sys
import unittest
from unittest.mock import patch

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

    def test_selected_plain_exe_game_lands_in_watched_games(self):
        options = base_options(selected_games=["Warframe"])
        config = installer.build_config(self.template, None, "pw", options)
        self.assertEqual(config["watched_games"], ["Warframe.x64.exe"])
        self.assertEqual(config["watched_windows"], [])

    def test_selected_title_contains_game_lands_in_watched_windows(self):
        options = base_options(selected_games=["Minecraft: Java Edition"])
        config = installer.build_config(self.template, None, "pw", options)
        self.assertEqual(config["watched_games"], [])
        self.assertEqual(
            config["watched_windows"],
            [{"process_name": "javaw.exe", "title_contains": "minecraft", "display_name": "Minecraft: Java Edition"}],
        )

    def test_unknown_selected_game_name_is_ignored(self):
        options = base_options(selected_games=["Some Game Not In The List"])
        config = installer.build_config(self.template, None, "pw", options)
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
        self.assertEqual(config["obs"]["replay_buffer"]["mode"], "with_recording")
        self.assertEqual(config["disk_space_guard"], {"enabled": True, "minimum_free_gb": 25.0})

    def test_advanced_options_default_off(self):
        config = installer.build_config(self.template, None, "pw", base_options())
        self.assertFalse(config["obs"]["multi_track_audio"]["enabled"])
        self.assertFalse(config["obs"]["game_audio_capture"]["enabled"])
        self.assertEqual(config["obs"]["replay_buffer"]["mode"], "off")
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


class IsObsRunningTests(unittest.TestCase):
    def make_fake_proc(self, name):
        proc = unittest.mock.Mock()
        proc.info = {"name": name}
        return proc

    def test_true_for_windows_process_names(self):
        for name in ("obs64.exe", "OBS32.EXE"):
            with patch.object(installer.psutil, "process_iter", return_value=[self.make_fake_proc(name)]):
                self.assertTrue(installer.is_obs_running())

    def test_true_for_linux_and_macos_process_name(self):
        for name in ("obs", "OBS"):
            with patch.object(installer.psutil, "process_iter", return_value=[self.make_fake_proc(name)]):
                self.assertTrue(installer.is_obs_running())

    def test_false_when_not_running(self):
        with patch.object(installer.psutil, "process_iter", return_value=[self.make_fake_proc("explorer.exe")]):
            self.assertFalse(installer.is_obs_running())


class GetObsWebsocketConfigPathTests(unittest.TestCase):
    def test_dispatches_to_platform_common_obs_config_dir(self):
        with patch.object(installer.platform_common, "obs_config_dir", return_value=r"C:\AppData\obs-studio"):
            path = installer.get_obs_websocket_config_path()
        self.assertEqual(path, os.path.join(r"C:\AppData\obs-studio", "plugin_config", "obs-websocket", "config.json"))

    def test_returns_none_when_obs_config_dir_unresolvable(self):
        with patch.object(installer.platform_common, "obs_config_dir", return_value=None):
            self.assertIsNone(installer.get_obs_websocket_config_path())


class EnsureFfmpegTests(unittest.TestCase):
    """ensure_ffmpeg must never block or fail setup regardless of what's on the machine -- these
    pin its possible outcomes against mocked presence/package-manager checks. The real per-OS
    install mechanism now lives in platform_common.install_optional_dependency() (winget/
    Homebrew) -- see test_platform_windows.py/test_platform_macos.py for that -- and this only
    covers ensure_ffmpeg's own dispatch logic, pinned to win32 so its behavior doesn't vary
    depending on which real OS happens to run this test (Linux takes a different, "manual_only"
    branch entirely -- covered separately below)."""

    def test_already_present_skips_install_entirely(self):
        with patch.object(installer, "is_ffmpeg_installed", return_value=True):
            with patch.object(installer.platform_common, "install_optional_dependency") as mock_install:
                status = installer.ensure_ffmpeg(lambda _msg: None)
        self.assertEqual(status, "already_present")
        mock_install.assert_not_called()

    def test_no_package_manager_reports_without_attempting_install(self):
        with patch.object(installer.sys, "platform", "win32"):
            with patch.object(installer, "is_ffmpeg_installed", return_value=False):
                with patch.object(installer.platform_common, "has_package_manager", return_value=False):
                    with patch.object(installer.platform_common, "install_optional_dependency") as mock_install:
                        status = installer.ensure_ffmpeg(lambda _msg: None)
        self.assertEqual(status, "no_package_manager")
        mock_install.assert_not_called()

    def test_successful_install_reports_installed(self):
        with patch.object(installer.sys, "platform", "win32"):
            with patch.object(installer, "is_ffmpeg_installed", return_value=False):
                with patch.object(installer.platform_common, "has_package_manager", return_value=True):
                    with patch.object(installer.platform_common, "install_optional_dependency", return_value=(True, None)):
                        status = installer.ensure_ffmpeg(lambda _msg: None)
        self.assertEqual(status, "installed")

    def test_failed_install_reports_install_failed_not_raise(self):
        with patch.object(installer.sys, "platform", "win32"):
            with patch.object(installer, "is_ffmpeg_installed", return_value=False):
                with patch.object(installer.platform_common, "has_package_manager", return_value=True):
                    with patch.object(
                        installer.platform_common, "install_optional_dependency", return_value=(False, "network error"),
                    ):
                        status = installer.ensure_ffmpeg(lambda _msg: None)  # must not raise
        self.assertEqual(status, "install_failed")

    def test_linux_never_attempts_a_silent_install(self):
        with patch.object(installer.sys, "platform", "linux"):
            with patch.object(installer, "is_ffmpeg_installed", return_value=False):
                with patch.object(installer.platform_common, "install_optional_dependency") as mock_install:
                    status = installer.ensure_ffmpeg(lambda _msg: None)
        self.assertEqual(status, "manual_only")
        mock_install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
