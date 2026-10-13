import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a


class RenameWithGamePrefixTests(unittest.TestCase):
    def make_file(self, tmp, name="recording.mkv"):
        path = os.path.join(tmp, name)
        open(path, "w").close()
        return path

    def test_no_game_display_name_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp)
            result = a.rename_with_game_prefix(src, None)
            self.assertEqual(result, src)
            self.assertTrue(os.path.isfile(src))

    def test_plain_rename_prefixes_game_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp)
            result = a.rename_with_game_prefix(src, "Warframe")
            self.assertEqual(os.path.basename(result), "Warframe - recording.mkv")
            self.assertTrue(os.path.isfile(result))

    def test_split_part_uses_split_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp)
            result = a.rename_with_game_prefix(src, "Warframe", split_part=2)
            self.assertEqual(os.path.basename(result), "Warframe - Split 2 - recording.mkv")

    def test_is_replay_uses_replay_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp, "Replay 2026-01-01.mkv")
            result = a.rename_with_game_prefix(src, "Warframe", is_replay=True)
            self.assertEqual(os.path.basename(result), "Warframe - Replay - Replay 2026-01-01.mkv")

    def test_split_part_takes_precedence_over_is_replay(self):
        # Not a real caller combination today, but the precedence should still be well-defined
        # rather than silently picking whichever branch happens to run last.
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp)
            result = a.rename_with_game_prefix(src, "Warframe", split_part=1, is_replay=True)
            self.assertIn("Split 1", os.path.basename(result))
            self.assertNotIn("Replay", os.path.basename(result))

    def test_silent_prefix_combines_with_replay_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp)
            result = a.rename_with_game_prefix(src, "Warframe", silent=True, is_replay=True)
            self.assertEqual(os.path.basename(result), "[NO AUDIO] - Warframe - Replay - recording.mkv")

    def test_subfolder_omits_game_name_from_filename_but_keeps_replay_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp)
            result = a.rename_with_game_prefix(src, "Warframe", use_subfolder=True, is_replay=True)
            self.assertEqual(os.path.basename(result), "Replay - recording.mkv")
            self.assertEqual(os.path.basename(os.path.dirname(result)), "Warframe")
            self.assertTrue(os.path.isfile(result))

    def test_neither_split_nor_replay_has_no_extra_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp)
            result = a.rename_with_game_prefix(src, "Warframe", is_replay=False)
            self.assertEqual(os.path.basename(result), "Warframe - recording.mkv")

    def test_retries_past_a_transient_lock_and_eventually_succeeds(self):
        # OBS can still hold its own handle on the file for a while after stop_record() returns
        # (confirmed live: a game crashing or being force-quit made this noticeably worse) --
        # a few early WinError 32s shouldn't be treated as a permanent failure.
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp)
            real_rename = os.rename
            attempts = {"count": 0}

            def flaky_rename(old, new):
                attempts["count"] += 1
                if attempts["count"] < 4:
                    raise OSError("The process cannot access the file because it is being used by another process")
                real_rename(old, new)

            with patch.object(a.os, "rename", side_effect=flaky_rename), patch.object(a.time, "sleep"):
                result = a.rename_with_game_prefix(src, "Warframe")
            self.assertEqual(os.path.basename(result), "Warframe - recording.mkv")
            self.assertTrue(os.path.isfile(result))
            self.assertEqual(attempts["count"], 4)

    def test_gives_up_after_the_full_retry_budget_and_keeps_the_original_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self.make_file(tmp)
            with patch.object(a.os, "rename", side_effect=OSError("locked")), patch.object(a.time, "sleep") as mock_sleep:
                result = a.rename_with_game_prefix(src, "Warframe")
            self.assertEqual(result, src)
            self.assertTrue(os.path.isfile(src))
            # 20 attempts, 19 sleeps between them -- the fix this guards: the old budget (5
            # attempts) was confirmed live to not be generous enough for a real crash/force-quit.
            self.assertEqual(mock_sleep.call_count, 19)


if __name__ == "__main__":
    unittest.main()
