import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a


class ParseTimestampTests(unittest.TestCase):
    def test_parses_hms(self):
        self.assertAlmostEqual(a.parse_timestamp("01:23:45.500"), 1 * 3600 + 23 * 60 + 45.5)

    def test_parses_minutes_seconds(self):
        self.assertAlmostEqual(a.parse_timestamp("02:30"), 150.0)

    def test_parses_seconds_only(self):
        self.assertAlmostEqual(a.parse_timestamp("45.25"), 45.25)

    def test_parses_bare_integer_seconds(self):
        self.assertAlmostEqual(a.parse_timestamp("90"), 90.0)

    def test_strips_whitespace(self):
        self.assertAlmostEqual(a.parse_timestamp("  01:00  "), 60.0)

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            a.parse_timestamp("")
        with self.assertRaises(ValueError):
            a.parse_timestamp(None)

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            a.parse_timestamp("not a timestamp")

    def test_rejects_too_many_components(self):
        with self.assertRaises(ValueError):
            a.parse_timestamp("1:02:03:04")

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            a.parse_timestamp("-5")

    def test_rejects_overflowing_minutes(self):
        with self.assertRaises(ValueError):
            a.parse_timestamp("01:75:00")

    def test_rejects_overflowing_seconds(self):
        with self.assertRaises(ValueError):
            a.parse_timestamp("00:00:75")


class FormatTimestampTests(unittest.TestCase):
    def test_formats_hms(self):
        self.assertEqual(a.format_timestamp(3725.5), "01:02:05.500")

    def test_formats_zero(self):
        self.assertEqual(a.format_timestamp(0), "00:00:00.000")

    def test_negative_clamped_to_zero(self):
        self.assertEqual(a.format_timestamp(-5), "00:00:00.000")

    def test_roundtrips_with_parse_timestamp(self):
        original = 7384.25
        self.assertAlmostEqual(a.parse_timestamp(a.format_timestamp(original)), original, places=2)


class BuildTrimCommandTests(unittest.TestCase):
    def test_fast_mode_seeks_before_input_and_stream_copies(self):
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 10, "out.mkv", precise=False)
        i_index = cmd.index("-i")
        ss_index = cmd.index("-ss")
        self.assertLess(ss_index, i_index)  # -ss (input seek) comes before -i
        self.assertIn("-c", cmd)
        self.assertIn("copy", cmd)
        self.assertNotIn("-c:v", cmd)

    def test_precise_mode_seeks_after_input_and_reencodes(self):
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 10, "out.mkv", precise=True)
        i_index = cmd.index("-i")
        ss_index = cmd.index("-ss")
        self.assertGreater(ss_index, i_index)  # -ss (output seek) comes after -i
        self.assertIn("-c:v", cmd)
        self.assertIn("libx264", cmd)

    def test_duration_is_end_minus_start(self):
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 12, "out.mkv")
        t_index = cmd.index("-t")
        self.assertEqual(cmd[t_index + 1], a.format_timestamp(7))

    def test_output_path_is_last_argument(self):
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 0, 5, "out.mkv")
        self.assertEqual(cmd[-1], "out.mkv")

    def test_uses_configured_ffmpeg_path(self):
        cmd = a.build_trim_command(r"C:\custom\ffmpeg.exe", "in.mkv", 0, 5, "out.mkv")
        self.assertEqual(cmd[0], r"C:\custom\ffmpeg.exe")


class ComputeTrimOutputPathTests(unittest.TestCase):
    def test_defaults_to_source_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "recording.mp4")
            open(src, "w").close()
            result = a.compute_trim_output_path(src)
            self.assertEqual(os.path.dirname(result), tmp)
            self.assertEqual(os.path.basename(result), "recording_trimmed.mp4")

    def test_uses_output_folder_and_creates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "recording.mp4")
            open(src, "w").close()
            out_dir = os.path.join(tmp, "Clips")
            result = a.compute_trim_output_path(src, output_folder=out_dir)
            self.assertEqual(os.path.dirname(result), out_dir)
            self.assertTrue(os.path.isdir(out_dir))

    def test_avoids_collision_with_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "recording.mp4")
            open(src, "w").close()
            open(os.path.join(tmp, "recording_trimmed.mp4"), "w").close()
            result = a.compute_trim_output_path(src)
            self.assertEqual(os.path.basename(result), "recording_trimmed (2).mp4")

    def test_avoids_collision_with_multiple_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "recording.mp4")
            open(src, "w").close()
            open(os.path.join(tmp, "recording_trimmed.mp4"), "w").close()
            open(os.path.join(tmp, "recording_trimmed (2).mp4"), "w").close()
            open(os.path.join(tmp, "recording_trimmed (3).mp4"), "w").close()
            result = a.compute_trim_output_path(src)
            self.assertEqual(os.path.basename(result), "recording_trimmed (4).mp4")

    def test_custom_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "recording.mkv")
            open(src, "w").close()
            result = a.compute_trim_output_path(src, suffix="_clip")
            self.assertEqual(os.path.basename(result), "recording_clip.mkv")


class TrimClipTests(unittest.TestCase):
    def _fake_run(self, returncode=0, stderr="", write_output=True, output_bytes=b"data"):
        def run(cmd, capture_output, text, creationflags):
            self.last_cmd = cmd
            if write_output:
                output_path = cmd[-1]
                with open(output_path, "wb") as f:
                    f.write(output_bytes)
            return type("Result", (), {"returncode": returncode, "stderr": stderr})()
        return run

    def test_end_before_start_is_rejected_without_running_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a.subprocess, "run") as mock_run:
                with self.assertLogs(level="ERROR"):
                    result = a.trim_clip(src, 10, 5, out)
            self.assertFalse(result)
            mock_run.assert_not_called()

    def test_successful_trim_returns_true_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                result = a.trim_clip(src, 0, 5, out)
            self.assertTrue(result)
            self.assertTrue(os.path.isfile(out))

    def test_failed_trim_does_not_delete_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a.subprocess, "run", side_effect=self._fake_run(returncode=1, stderr="boom")):
                with self.assertLogs(level="ERROR"):
                    result = a.trim_clip(src, 0, 5, out, delete_original=True)
            self.assertFalse(result)
            self.assertTrue(os.path.isfile(src))

    def test_zero_byte_output_is_treated_as_failure_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a.subprocess, "run", side_effect=self._fake_run(output_bytes=b"")):
                with self.assertLogs(level="ERROR"):
                    result = a.trim_clip(src, 0, 5, out)
            self.assertFalse(result)
            self.assertFalse(os.path.isfile(out))
            self.assertTrue(os.path.isfile(src))

    def test_missing_output_is_treated_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a.subprocess, "run", side_effect=self._fake_run(write_output=False)):
                with self.assertLogs(level="ERROR"):
                    result = a.trim_clip(src, 0, 5, out)
            self.assertFalse(result)

    def test_successful_trim_deletes_original_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                result = a.trim_clip(src, 0, 5, out, delete_original=True)
            self.assertTrue(result)
            self.assertFalse(os.path.isfile(src))

    def test_ffmpeg_not_runnable_is_logged_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a.subprocess, "run", side_effect=OSError("not found")):
                with self.assertLogs(level="ERROR"):
                    result = a.trim_clip(src, 0, 5, out)  # must not raise
            self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
