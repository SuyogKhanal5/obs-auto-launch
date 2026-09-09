import os
import sys
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


if __name__ == "__main__":
    unittest.main()
