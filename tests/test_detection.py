import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a


# These fixtures build paths with os.path.join rather than hardcoded backslash literals -- the
# functions under test use the ambient os.sep/os.path (ntpath on Windows, posixpath elsewhere),
# so a literal r"D:\..." string is only a realistic exe path on Windows; on Linux/macOS it's just
# an opaque string with no path separators posixpath recognizes at all, which would silently test
# the wrong thing rather than the real cross-platform prefix-matching logic. Building fixtures the
# same way the real code builds paths keeps these tests meaningful on every OS the suite runs on
# (per CROSS_PLATFORM_PLAN.md §3.2/§5.11), instead of being Windows-only assertions.
class IsExeUnderDirsTests(unittest.TestCase):
    def setUp(self):
        self.dirs = [os.path.join("steamlibrary", "steamapps", "common")]

    def test_matches_under_common_dir(self):
        exe = os.path.join("SteamLibrary", "steamapps", "common", "Balatro", "Balatro.exe")
        self.assertTrue(a.is_exe_under_dirs(exe, self.dirs, []))

    def test_excluded_keyword_blocks_match(self):
        exe = os.path.join("SteamLibrary", "steamapps", "common", "SomeGame", "_CommonRedist", "vcredist.exe")
        self.assertFalse(a.is_exe_under_dirs(exe, self.dirs, ["_commonredist"]))

    def test_outside_common_dirs_does_not_match(self):
        exe = os.path.join("System", "System32", "notepad.exe")
        self.assertFalse(a.is_exe_under_dirs(exe, self.dirs, []))

    def test_empty_exe_path_does_not_match(self):
        self.assertFalse(a.is_exe_under_dirs("", self.dirs, []))
        self.assertFalse(a.is_exe_under_dirs(None, self.dirs, []))


class GetDisplayNameFromDirsTests(unittest.TestCase):
    def test_extracts_top_level_folder(self):
        dirs = [os.path.join("steamlibrary", "steamapps", "common")]
        exe = os.path.join("SteamLibrary", "steamapps", "common", "Balatro", "Balatro.exe")
        name = a.get_display_name_from_dirs(exe, dirs)
        self.assertEqual(name, "Balatro")

    def test_no_match_returns_none(self):
        dirs = [os.path.join("steamlibrary", "steamapps", "common")]
        exe = os.path.join("Games", "Foo", "Foo.exe")
        self.assertIsNone(a.get_display_name_from_dirs(exe, dirs))


class FindGameByInstallDirTests(unittest.TestCase):
    def test_matches_manifest_install_dir(self):
        install_dir = os.path.join("games", "hitman 3")
        manifest_games = [{"install_dir": install_dir, "display_name": "HITMAN 3"}]
        exe = os.path.join("Games", "HITMAN 3", "Retail", "HITMAN3.exe")
        result = a.find_game_by_install_dir(exe, manifest_games)
        self.assertEqual(result, "HITMAN 3")

    def test_no_manifest_match_returns_none(self):
        exe = os.path.join("Games", "Other", "other.exe")
        self.assertIsNone(a.find_game_by_install_dir(exe, []))


class FindTargetProcessTests(unittest.TestCase):
    """Covers the priority order: explicit watched_games first, then launcher-detected
    (Steam/Xbox/Battle.net) common dirs, then manifest-based launchers (Epic/GOG), then
    window-title rules last -- each cheaper/more-specific check should win over a later,
    more expensive one when multiple could match the same process list."""

    def test_watched_games_takes_priority(self):
        exe = os.path.join("Steam", "steamapps", "common", "cs2", "cs2.exe")
        processes = [("cs2.exe", exe, 111)]
        common_dirs = [os.path.join("steam", "steamapps", "common")]
        name, exe, pid, display = a.find_target_process({"cs2.exe"}, common_dirs, [], [], [], processes)
        self.assertEqual((name, pid, display), ("cs2.exe", 111, None))

    def test_falls_back_to_common_dirs(self):
        exe = os.path.join("SteamLibrary", "steamapps", "common", "Balatro", "Balatro.exe")
        processes = [("Balatro.exe", exe, 222)]
        common_dirs = [os.path.join("steamlibrary", "steamapps", "common")]
        name, exe, pid, display = a.find_target_process(set(), common_dirs, [], [], [], processes)
        self.assertEqual((name, pid, display), ("Balatro.exe", 222, None))

    def test_falls_back_to_manifest_games(self):
        exe = os.path.join("Games", "HITMAN 3", "HITMAN3.exe")
        processes = [("HITMAN3.exe", exe, 333)]
        install_dir = os.path.join("games", "hitman 3")
        manifest_games = [{"install_dir": install_dir, "display_name": "HITMAN 3"}]
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


class NormalizeAllowedLibraryRootsTests(unittest.TestCase):
    def test_full_path_entries_pass_through_unchanged(self):
        self.assertEqual(
            a.normalize_allowed_library_roots(["/mnt/data", "/Volumes/External"]),
            ["/mnt/data", "/Volumes/External"],
        )

    def test_bare_drive_letter_expands_to_root_on_windows(self):
        with patch.object(a.sys, "platform", "win32"):
            self.assertEqual(a.normalize_allowed_library_roots(["C", "d:"]), ["C:\\", "D:\\"])

    def test_bare_drive_letter_dropped_with_warning_on_non_windows(self):
        with patch.object(a.sys, "platform", "linux"):
            with self.assertLogs(level="WARNING"):
                result = a.normalize_allowed_library_roots(["C"])
        self.assertEqual(result, [])

    def test_blank_entries_ignored(self):
        self.assertEqual(a.normalize_allowed_library_roots(["", "   "]), [])

    def test_none_input_returns_empty_list(self):
        self.assertEqual(a.normalize_allowed_library_roots(None), [])


class GetSteamCommonDirsAllowedRootsFilterTests(unittest.TestCase):
    def test_filters_to_matching_root_prefix(self):
        install_path = os.path.join("C:" + os.sep, "Steam") if os.name == "nt" else "/steam"
        with patch.object(a, "get_steam_install_path", return_value=install_path):
            with patch.object(a.os.path, "isfile", return_value=False):
                dirs = a.get_steam_common_dirs({"allowed_drives": [install_path]})
        self.assertEqual(dirs, [os.path.join(install_path, "steamapps", "common").lower()])

    def test_excludes_non_matching_root(self):
        install_path = os.path.join("C:" + os.sep, "Steam") if os.name == "nt" else "/steam"
        with patch.object(a, "get_steam_install_path", return_value=install_path):
            with patch.object(a.os.path, "isfile", return_value=False):
                dirs = a.get_steam_common_dirs({"allowed_drives": ["/somewhere/else"]})
        self.assertEqual(dirs, [])

    def test_no_filter_when_allowed_drives_unset(self):
        install_path = os.path.join("C:" + os.sep, "Steam") if os.name == "nt" else "/steam"
        with patch.object(a, "get_steam_install_path", return_value=install_path):
            with patch.object(a.os.path, "isfile", return_value=False):
                dirs = a.get_steam_common_dirs({})
        self.assertEqual(dirs, [os.path.join(install_path, "steamapps", "common").lower()])


class SanitizeFilenamePartTests(unittest.TestCase):
    def test_strips_invalid_filename_characters(self):
        self.assertEqual(a.sanitize_filename_part('Game: Part <2>?'), "Game Part 2")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(a.sanitize_filename_part("  Balatro  "), "Balatro")


if __name__ == "__main__":
    unittest.main()
