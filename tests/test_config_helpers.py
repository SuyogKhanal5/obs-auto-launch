import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a


class AutoSplitDisabledTests(unittest.TestCase):
    def test_disabled_config_never_matches(self):
        self.assertFalse(a.is_automatic_split({}, None, {"enabled": False}))
        self.assertFalse(a.is_automatic_split({}, None, None))


class AutoSplitByTimeTests(unittest.TestCase):
    def config(self, **overrides):
        cfg = {"enabled": True, "by": "time", "minutes": 2, "tolerance_seconds": 8}
        cfg.update(overrides)
        return cfg

    def state_with_elapsed(self, elapsed_seconds):
        return {"segment_start_time": time.time() - elapsed_seconds}

    def test_matches_within_tolerance_window(self):
        # target is 120s; 122s elapsed is inside the 8s tolerance window.
        self.assertTrue(a.is_automatic_split(self.state_with_elapsed(122), None, self.config()))

    def test_does_not_match_before_target(self):
        self.assertFalse(a.is_automatic_split(self.state_with_elapsed(60), None, self.config()))

    def test_does_not_match_well_past_tolerance(self):
        self.assertFalse(a.is_automatic_split(self.state_with_elapsed(300), None, self.config()))

    def test_zero_minutes_never_matches(self):
        self.assertFalse(a.is_automatic_split(self.state_with_elapsed(0), None, self.config(minutes=0)))


class AutoSplitBySizeTests(unittest.TestCase):
    def config(self, **overrides):
        cfg = {"enabled": True, "by": "size", "megabytes": 4096, "tolerance_megabytes": 50}
        cfg.update(overrides)
        return cfg

    def test_matches_within_tolerance(self):
        size_bytes = int(4110 * 1024 * 1024)  # 4096 + 14 MB, inside the 50 MB tolerance
        with patch("os.path.getsize", return_value=size_bytes):
            self.assertTrue(a.is_automatic_split({}, "fake.mkv", self.config()))

    def test_does_not_match_below_target(self):
        size_bytes = int(2000 * 1024 * 1024)
        with patch("os.path.getsize", return_value=size_bytes):
            self.assertFalse(a.is_automatic_split({}, "fake.mkv", self.config()))

    def test_no_path_never_matches(self):
        self.assertFalse(a.is_automatic_split({}, None, self.config()))

    def test_missing_file_does_not_raise(self):
        with patch("os.path.getsize", side_effect=OSError("missing")):
            self.assertFalse(a.is_automatic_split({}, "gone.mkv", self.config()))


class IsSegmentSilentTests(unittest.TestCase):
    def test_disabled_never_flags_silent(self):
        self.assertFalse(a.is_segment_silent({"heard_any_audio": False}, {"enabled": False}))

    def test_flags_silent_when_no_audio_heard(self):
        self.assertTrue(a.is_segment_silent({"heard_any_audio": False}, {"enabled": True}))

    def test_not_silent_once_audio_heard(self):
        self.assertFalse(a.is_segment_silent({"heard_any_audio": True}, {"enabled": True}))


class CsvFieldTests(unittest.TestCase):
    def test_parse_splits_and_trims(self):
        self.assertEqual(a.parse_csv_field(" cs2.exe, valorant.exe ,  "), ["cs2.exe", "valorant.exe"])

    def test_parse_empty_string(self):
        self.assertEqual(a.parse_csv_field(""), [])

    def test_format_joins_with_comma_space(self):
        self.assertEqual(a.format_csv_field(["cs2.exe", "valorant.exe"]), "cs2.exe, valorant.exe")

    def test_format_none_is_empty_string(self):
        self.assertEqual(a.format_csv_field(None), "")

    def test_roundtrip(self):
        items = ["a.exe", "b.exe", "c.exe"]
        self.assertEqual(a.parse_csv_field(a.format_csv_field(items)), items)


class ObsRecoveryDefaultsTests(unittest.TestCase):
    def test_memory_limit_defaults_when_unset(self):
        expected = a.DEFAULT_OBS_MEMORY_LIMIT_GB * 1024 ** 3
        self.assertEqual(a.get_obs_memory_limit_bytes({}), expected)

    def test_memory_limit_uses_configured_value(self):
        self.assertEqual(a.get_obs_memory_limit_bytes({"recovery": {"memory_limit_gb": 2}}), 2 * 1024 ** 3)

    def test_cooldown_defaults_when_unset(self):
        self.assertEqual(a.get_obs_recovery_cooldown_seconds({}), a.DEFAULT_OBS_RECOVERY_COOLDOWN_SECONDS)

    def test_cooldown_uses_configured_value(self):
        self.assertEqual(a.get_obs_recovery_cooldown_seconds({"recovery": {"cooldown_seconds": 5}}), 5)


class HasSufficientDiskSpaceTests(unittest.TestCase):
    def test_disabled_guard_always_passes(self):
        self.assertTrue(a.has_sufficient_disk_space({"enabled": False}, "C:\\"))

    def test_passes_when_free_space_above_minimum(self):
        usage = SimpleNamespace(free=20 * 1024 ** 3)
        with patch("shutil.disk_usage", return_value=usage):
            self.assertTrue(a.has_sufficient_disk_space({"enabled": True, "minimum_free_gb": 10}, "C:\\"))

    def test_fails_when_free_space_below_minimum(self):
        usage = SimpleNamespace(free=5 * 1024 ** 3)
        with patch("shutil.disk_usage", return_value=usage):
            self.assertFalse(a.has_sufficient_disk_space({"enabled": True, "minimum_free_gb": 10}, "C:\\"))

    def test_unreadable_path_fails_open(self):
        # A guard that can't even check free space shouldn't be the reason recording never
        # starts -- fail open (allow) rather than block indefinitely on a bad/missing drive.
        with patch("shutil.disk_usage", side_effect=OSError("no such drive")):
            self.assertTrue(a.has_sufficient_disk_space({"enabled": True, "minimum_free_gb": 10}, "Z:\\"))


class EnforceStorageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def make_clip(self, name, size_bytes, age_seconds):
        path = os.path.join(self.tmpdir.name, name)
        with open(path, "wb") as f:
            f.write(b"0" * size_bytes)
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))
        return path

    def disk_usage(self, free_bytes):
        return SimpleNamespace(free=free_bytes)

    def test_disabled_does_nothing(self):
        path = self.make_clip("a.mp4", 1024, 100)
        a.enforce_storage_budget({"enabled": False}, {"output_folder": self.tmpdir.name})
        self.assertTrue(os.path.isfile(path))

    def test_no_watch_folder_configured_does_nothing(self):
        with patch("shutil.disk_usage") as mock_disk_usage:
            a.enforce_storage_budget({"enabled": True}, {})
        mock_disk_usage.assert_not_called()

    def test_sufficient_free_space_deletes_nothing(self):
        path = self.make_clip("a.mp4", 1024, 100)
        usage = self.disk_usage(50 * 1024 ** 3)
        with patch("shutil.disk_usage", return_value=usage):
            a.enforce_storage_budget(
                {"enabled": True, "reserved_free_gb": 10, "watch_folder": self.tmpdir.name}, {}
            )
        self.assertTrue(os.path.isfile(path))

    def test_deletes_oldest_clips_first_until_reserve_is_met(self):
        one_gb = 1024 ** 3
        oldest = self.make_clip("oldest.mp4", one_gb, age_seconds=300)
        middle = self.make_clip("middle.mp4", one_gb, age_seconds=200)
        newest = self.make_clip("newest.mp4", one_gb, age_seconds=100)
        # 8 GB free against a 10 GB reserve is a 2 GB shortfall -- deleting just the oldest 1 GB
        # file isn't enough, so the second-oldest should go too, but the newest should survive.
        usage = self.disk_usage(8 * one_gb)
        with patch("shutil.disk_usage", return_value=usage):
            a.enforce_storage_budget(
                {"enabled": True, "reserved_free_gb": 10, "watch_folder": self.tmpdir.name}, {}
            )
        self.assertFalse(os.path.isfile(oldest))
        self.assertFalse(os.path.isfile(middle))
        self.assertTrue(os.path.isfile(newest))

    def test_never_deletes_the_actively_recording_file(self):
        one_gb = 1024 ** 3
        active = self.make_clip("active.mp4", one_gb, age_seconds=1000)  # oldest, but in progress
        newer = self.make_clip("newer.mp4", one_gb, age_seconds=10)
        usage = self.disk_usage(5 * one_gb)
        with patch("shutil.disk_usage", return_value=usage):
            a.enforce_storage_budget(
                {"enabled": True, "reserved_free_gb": 10, "watch_folder": self.tmpdir.name},
                {},
                recording_state={"current_path": active},
            )
        self.assertTrue(os.path.isfile(active))
        self.assertFalse(os.path.isfile(newer))

    def test_ignores_non_clip_extensions(self):
        other = self.make_clip("notes.txt", 1024 ** 3, age_seconds=100)
        usage = self.disk_usage(1 * 1024 ** 3)
        with patch("shutil.disk_usage", return_value=usage):
            a.enforce_storage_budget(
                {"enabled": True, "reserved_free_gb": 10, "watch_folder": self.tmpdir.name}, {}
            )
        self.assertTrue(os.path.isfile(other))

    def test_watch_folder_falls_back_to_obs_output_folder(self):
        path = self.make_clip("a.mp4", 1024 ** 3, age_seconds=100)
        usage = self.disk_usage(1 * 1024 ** 3)
        with patch("shutil.disk_usage", return_value=usage):
            a.enforce_storage_budget(
                {"enabled": True, "reserved_free_gb": 10}, {"output_folder": self.tmpdir.name}
            )
        self.assertFalse(os.path.isfile(path))


class CleanupOrphanedPyinstallerTempDirsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        patcher = patch.dict(os.environ, {"TEMP": self.tmpdir.name, "TMP": self.tmpdir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_dir(self, name):
        path = os.path.join(self.tmpdir.name, name)
        os.mkdir(path)
        return path

    def test_removes_folder_whose_pid_is_not_running(self):
        dead = self.make_dir("_MEI999999")
        with patch("psutil.pid_exists", return_value=False):
            a.cleanup_orphaned_pyinstaller_temp_dirs()
        self.assertFalse(os.path.isdir(dead))

    def test_leaves_folder_whose_pid_is_still_running(self):
        alive = self.make_dir(f"_MEI{os.getpid()}")
        with patch("psutil.pid_exists", return_value=True):
            a.cleanup_orphaned_pyinstaller_temp_dirs()
        self.assertTrue(os.path.isdir(alive))

    def test_ignores_non_mei_folders(self):
        other = self.make_dir("SomeOtherApp")
        with patch("psutil.pid_exists", return_value=False):
            a.cleanup_orphaned_pyinstaller_temp_dirs()
        self.assertTrue(os.path.isdir(other))

    def test_missing_temp_env_var_does_not_raise(self):
        with patch.dict(os.environ, {}, clear=True):
            a.cleanup_orphaned_pyinstaller_temp_dirs()  # must not raise


class DefaultOverlayMonitorIndexTests(unittest.TestCase):
    def test_picks_monitor_at_origin(self):
        monitors = [
            {"left": -1920, "top": 0, "right": 0, "bottom": 1080},
            {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
        ]
        self.assertEqual(a.default_overlay_monitor_index(monitors), 1)

    def test_falls_back_to_first_monitor_when_none_at_origin(self):
        monitors = [{"left": 100, "top": 50, "right": 1920, "bottom": 1080}]
        self.assertEqual(a.default_overlay_monitor_index(monitors), 0)

    def test_no_monitors_returns_none(self):
        self.assertIsNone(a.default_overlay_monitor_index([]))


class ResolveDefaultOverlayMonitorIndexTests(unittest.TestCase):
    def setUp(self):
        self.monitors = [
            {"left": 0, "top": 0, "right": 1920, "bottom": 1080, "label": "1920x1080 monitor at (0, 0)"},
            {"left": 1920, "top": 0, "right": 3840, "bottom": 1080, "label": "1920x1080 monitor at (1920, 0)"},
        ]

    def test_unset_label_returns_none(self):
        self.assertIsNone(a.resolve_default_overlay_monitor_index(self.monitors, {}))

    def test_matching_label_returns_its_index(self):
        overlay_config = {"default_monitor_label": "1920x1080 monitor at (1920, 0)"}
        self.assertEqual(a.resolve_default_overlay_monitor_index(self.monitors, overlay_config), 1)

    def test_stale_label_not_matching_any_current_monitor_returns_none(self):
        # A saved monitor that's no longer connected (multi-monitor setup changed) should fall
        # back to Off rather than silently guessing a different monitor.
        overlay_config = {"default_monitor_label": "2560x1440 monitor at (0, 0)"}
        self.assertIsNone(a.resolve_default_overlay_monitor_index(self.monitors, overlay_config))

    def test_no_monitors_returns_none(self):
        overlay_config = {"default_monitor_label": "1920x1080 monitor at (0, 0)"}
        self.assertIsNone(a.resolve_default_overlay_monitor_index([], overlay_config))


if __name__ == "__main__":
    unittest.main()
