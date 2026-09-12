import os
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
