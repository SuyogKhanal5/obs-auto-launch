import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a


class IsExeUnderDirsTests(unittest.TestCase):
    def setUp(self):
        self.dirs = [r"d:\steamlibrary\steamapps\common"]

    def test_matches_under_common_dir(self):
        self.assertTrue(a.is_exe_under_dirs(r"D:\SteamLibrary\steamapps\common\Balatro\Balatro.exe", self.dirs, []))

    def test_excluded_keyword_blocks_match(self):
        exe = r"D:\SteamLibrary\steamapps\common\SomeGame\_CommonRedist\vcredist.exe"
        self.assertFalse(a.is_exe_under_dirs(exe, self.dirs, ["_commonredist"]))

    def test_outside_common_dirs_does_not_match(self):
        self.assertFalse(a.is_exe_under_dirs(r"C:\Windows\System32\notepad.exe", self.dirs, []))

    def test_empty_exe_path_does_not_match(self):
        self.assertFalse(a.is_exe_under_dirs("", self.dirs, []))
        self.assertFalse(a.is_exe_under_dirs(None, self.dirs, []))


class GetDisplayNameFromDirsTests(unittest.TestCase):
    def test_extracts_top_level_folder(self):
        dirs = [r"d:\steamlibrary\steamapps\common"]
        name = a.get_display_name_from_dirs(r"D:\SteamLibrary\steamapps\common\Balatro\Balatro.exe", dirs)
        self.assertEqual(name, "Balatro")

    def test_no_match_returns_none(self):
        dirs = [r"d:\steamlibrary\steamapps\common"]
        self.assertIsNone(a.get_display_name_from_dirs(r"C:\Games\Foo\Foo.exe", dirs))


class FindGameByInstallDirTests(unittest.TestCase):
    def test_matches_manifest_install_dir(self):
        manifest_games = [{"install_dir": r"c:\games\hitman 3", "display_name": "HITMAN 3"}]
        result = a.find_game_by_install_dir(r"C:\Games\HITMAN 3\Retail\HITMAN3.exe", manifest_games)
        self.assertEqual(result, "HITMAN 3")

    def test_no_manifest_match_returns_none(self):
        self.assertIsNone(a.find_game_by_install_dir(r"C:\Games\Other\other.exe", []))


class FindTargetProcessTests(unittest.TestCase):
    """Covers the priority order: explicit watched_games first, then launcher-detected
    (Steam/Xbox/Battle.net) common dirs, then manifest-based launchers (Epic/GOG), then
    window-title rules last -- each cheaper/more-specific check should win over a later,
    more expensive one when multiple could match the same process list."""

    def test_watched_games_takes_priority(self):
        processes = [("cs2.exe", r"D:\Steam\steamapps\common\cs2\cs2.exe", 111)]
        name, exe, pid, display = a.find_target_process(
            {"cs2.exe"}, [r"d:\steam\steamapps\common"], [], [], [], processes
        )
        self.assertEqual((name, pid, display), ("cs2.exe", 111, None))

    def test_falls_back_to_common_dirs(self):
        processes = [("Balatro.exe", r"D:\SteamLibrary\steamapps\common\Balatro\Balatro.exe", 222)]
        name, exe, pid, display = a.find_target_process(
            set(), [r"d:\steamlibrary\steamapps\common"], [], [], [], processes
        )
        self.assertEqual((name, pid, display), ("Balatro.exe", 222, None))

    def test_falls_back_to_manifest_games(self):
        processes = [("HITMAN3.exe", r"C:\Games\HITMAN 3\HITMAN3.exe", 333)]
        manifest_games = [{"install_dir": r"c:\games\hitman 3", "display_name": "HITMAN 3"}]
        name, exe, pid, display = a.find_target_process(set(), [], [], manifest_games, [], processes)
        self.assertEqual((name, pid, display), ("HITMAN3.exe", 333, "HITMAN 3"))

    def test_window_title_rule_matches_when_title_contains_needle(self):
        processes = [("javaw.exe", r"C:\Minecraft\javaw.exe", 444)]
        watched_windows = [{"process_name": "javaw.exe", "title_contains": "minecraft", "display_name": "Minecraft"}]
        with patch.object(a, "get_window_titles", return_value={444: ["Minecraft 1.20.4"]}):
            name, exe, pid, display = a.find_target_process(set(), [], [], [], watched_windows, processes)
        self.assertEqual((name, pid, display), ("javaw.exe", 444, "Minecraft"))

    def test_window_title_rule_does_not_match_wrong_title(self):
        processes = [("javaw.exe", r"C:\SomeOtherApp\javaw.exe", 555)]
        watched_windows = [{"process_name": "javaw.exe", "title_contains": "minecraft", "display_name": "Minecraft"}]
        with patch.object(a, "get_window_titles", return_value={555: ["Some Other Java App"]}):
            result = a.find_target_process(set(), [], [], [], watched_windows, processes)
        self.assertEqual(result, (None, None, None, None))

    def test_no_match_returns_all_none(self):
        processes = [("explorer.exe", r"C:\Windows\explorer.exe", 666)]
        result = a.find_target_process(set(), [], [], [], [], processes)
        self.assertEqual(result, (None, None, None, None))


class SanitizeFilenamePartTests(unittest.TestCase):
    def test_strips_invalid_filename_characters(self):
        self.assertEqual(a.sanitize_filename_part('Game: Part <2>?'), "Game Part 2")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(a.sanitize_filename_part("  Balatro  "), "Balatro")


if __name__ == "__main__":
    unittest.main()
