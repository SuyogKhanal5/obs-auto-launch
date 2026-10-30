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
        name, exe, pid, display, window_entry = a.find_target_process({"cs2.exe"}, common_dirs, [], [], [], processes)
        self.assertEqual((name, pid, display, window_entry), ("cs2.exe", 111, None, None))

    def test_falls_back_to_common_dirs(self):
        exe = os.path.join("SteamLibrary", "steamapps", "common", "Balatro", "Balatro.exe")
        processes = [("Balatro.exe", exe, 222)]
        common_dirs = [os.path.join("steamlibrary", "steamapps", "common")]
        name, exe, pid, display, window_entry = a.find_target_process(set(), common_dirs, [], [], [], processes)
        self.assertEqual((name, pid, display, window_entry), ("Balatro.exe", 222, None, None))

    def test_falls_back_to_manifest_games(self):
        exe = os.path.join("Games", "HITMAN 3", "HITMAN3.exe")
        processes = [("HITMAN3.exe", exe, 333)]
        install_dir = os.path.join("games", "hitman 3")
        manifest_games = [{"install_dir": install_dir, "display_name": "HITMAN 3"}]
        name, exe, pid, display, window_entry = a.find_target_process(set(), [], [], manifest_games, [], processes)
        self.assertEqual((name, pid, display, window_entry), ("HITMAN3.exe", 333, "HITMAN 3", None))

    def test_window_title_rule_matches_when_title_contains_needle(self):
        processes = [("javaw.exe", r"C:\Minecraft\javaw.exe", 444)]
        watched_windows = [{"process_name": "javaw.exe", "title_contains": "minecraft", "display_name": "Minecraft"}]
        with patch.object(a, "get_window_titles", return_value={444: ["Minecraft 1.20.4"]}):
            name, exe, pid, display, window_entry = a.find_target_process(set(), [], [], [], watched_windows, processes)
        self.assertEqual((name, pid, display, window_entry), ("javaw.exe", 444, "Minecraft", watched_windows[0]))

    def test_window_title_rule_does_not_match_wrong_title(self):
        processes = [("javaw.exe", r"C:\SomeOtherApp\javaw.exe", 555)]
        watched_windows = [{"process_name": "javaw.exe", "title_contains": "minecraft", "display_name": "Minecraft"}]
        with patch.object(a, "get_window_titles", return_value={555: ["Some Other Java App"]}):
            result = a.find_target_process(set(), [], [], [], watched_windows, processes)
        self.assertEqual(result, (None, None, None, None, None))

    def test_no_match_returns_all_none(self):
        processes = [("explorer.exe", r"C:\Windows\explorer.exe", 666)]
        result = a.find_target_process(set(), [], [], [], [], processes)
        self.assertEqual(result, (None, None, None, None, None))

    def test_window_title_rule_extracts_dynamic_display_name_when_opted_in(self):
        # Confirmed live against a real GeForce NOW session: its window title changes from the
        # generic "GeForce NOW" (idling at its own menu) to "<game> on GeForce NOW" once a game
        # actually loads -- use_title_as_display_name recovers the specific game's name from
        # that, since a static configured display_name can't (the game played changes session to
        # session, unlike Minecraft's own entry, which never sets this flag).
        processes = [("GeForceNOW", "/Applications/GeForceNOW.app/Contents/MacOS/GeForceNOW", 777)]
        watched_windows = [{
            "process_name": "GeForceNOW", "title_contains": "on GeForce NOW",
            "display_name": "GeForce NOW", "use_title_as_display_name": True,
        }]
        with patch.object(a, "get_window_titles", return_value={777: ["Destiny 2 on GeForce NOW"]}):
            name, exe, pid, display, window_entry = a.find_target_process(set(), [], [], [], watched_windows, processes)
        self.assertEqual((name, pid, display), ("GeForceNOW", 777, "Destiny 2"))
        self.assertIs(window_entry, watched_windows[0])

    def test_window_title_rule_falls_back_to_static_name_when_extraction_is_blank(self):
        # The idle-menu title itself ("GeForce NOW") never actually matches "on GeForce NOW", so
        # this specifically covers a needle positioned at the very start of a matched title (an
        # edge case the "extract everything before it" logic should degrade safely for, not crash
        # or silently use an empty name).
        processes = [("GeForceNOW", "/Applications/GeForceNOW.app/Contents/MacOS/GeForceNOW", 888)]
        watched_windows = [{
            "process_name": "GeForceNOW", "title_contains": "GeForce NOW",
            "display_name": "GeForce NOW", "use_title_as_display_name": True,
        }]
        with patch.object(a, "get_window_titles", return_value={888: ["GeForce NOW"]}):
            name, exe, pid, display, window_entry = a.find_target_process(set(), [], [], [], watched_windows, processes)
        self.assertEqual((name, pid, display), ("GeForceNOW", 888, "GeForce NOW"))

    def test_window_title_rule_uses_static_name_when_opt_in_flag_is_absent(self):
        # Default/existing behavior (Minecraft's own real config entry) is unaffected: no
        # use_title_as_display_name means the configured display_name is used as-is, regardless
        # of where title_contains happens to appear in the actual matched title.
        processes = [("javaw.exe", r"C:\Minecraft\javaw.exe", 444)]
        watched_windows = [{"process_name": "javaw.exe", "title_contains": "minecraft", "display_name": "Minecraft"}]
        with patch.object(a, "get_window_titles", return_value={444: ["My World - Minecraft 1.20.4"]}):
            name, exe, pid, display, window_entry = a.find_target_process(set(), [], [], [], watched_windows, processes)
        self.assertEqual(display, "Minecraft")


class ExtractDisplayNameFromTitleTests(unittest.TestCase):
    def test_extracts_text_before_needle(self):
        self.assertEqual(a.extract_display_name_from_title("Destiny 2 on GeForce NOW", "on GeForce NOW"), "Destiny 2")

    def test_is_case_insensitive(self):
        self.assertEqual(a.extract_display_name_from_title("Destiny 2 ON geforce now", "on GeForce NOW"), "Destiny 2")

    def test_needle_not_found_returns_none(self):
        self.assertIsNone(a.extract_display_name_from_title("Some Other Title", "on GeForce NOW"))

    def test_needle_at_start_returns_none_not_blank_string(self):
        self.assertIsNone(a.extract_display_name_from_title("GeForce NOW", "GeForce NOW"))


class WatchedWindowStillMatchesTests(unittest.TestCase):
    # Confirmed live: GeForce NOW's own process keeps running when you back out to its menu after
    # a game, only its window's title reverts from "<game> on GeForce NOW" to the generic
    # "GeForce NOW" -- this is what the watcher loop's "is this session still active" check now
    # also re-verifies for a watched_windows-detected session, on top of is_process_running.
    def test_true_while_title_still_matches(self):
        entry = {"process_name": "GeForceNOW", "title_contains": "on GeForce NOW"}
        with patch.object(a, "get_window_titles", return_value={777: ["Destiny 2 on GeForce NOW"]}):
            self.assertTrue(a.watched_window_still_matches(777, entry))

    def test_false_once_title_reverts_to_the_generic_menu_title(self):
        entry = {"process_name": "GeForceNOW", "title_contains": "on GeForce NOW"}
        with patch.object(a, "get_window_titles", return_value={777: ["GeForce NOW"]}):
            self.assertFalse(a.watched_window_still_matches(777, entry))

    def test_false_when_pid_has_no_windows_at_all(self):
        entry = {"process_name": "GeForceNOW", "title_contains": "on GeForce NOW"}
        with patch.object(a, "get_window_titles", return_value={}):
            self.assertFalse(a.watched_window_still_matches(777, entry))


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
