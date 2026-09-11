import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
from tests.fakes import FakeObsClient


class TriggerBufferedSplitTests(unittest.TestCase):
    def test_zero_buffer_splits_immediately_without_sleeping(self):
        client = FakeObsClient()
        with patch.object(a.time, "sleep") as mock_sleep:
            a.trigger_buffered_split(client, 0)
        mock_sleep.assert_not_called()
        self.assertIn(("split_record_file",), client.calls)

    def test_positive_buffer_sleeps_before_splitting(self):
        client = FakeObsClient()
        with patch.object(a.time, "sleep") as mock_sleep:
            a.trigger_buffered_split(client, 5)
        mock_sleep.assert_called_once_with(5)
        self.assertIn(("split_record_file",), client.calls)

    def test_split_failure_is_logged_not_raised(self):
        client = FakeObsClient()

        def raise_error():
            raise RuntimeError("not recording")

        client.split_record_file = raise_error
        with self.assertLogs(level="ERROR"):
            a.trigger_buffered_split(client, 0)  # must not raise


class TranscodeRecordingTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise transcode_recording's own logic (output naming, exit-code
        # handling, delete-original), not ffmpeg discovery -- pin resolve_ffmpeg_path so results
        # don't depend on whether this machine actually has ffmpeg installed anywhere.
        patcher = patch.object(a, "resolve_ffmpeg_path", return_value=r"C:\fake\ffmpeg.exe")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_run(self, returncode=0, stderr=""):
        def run(cmd, capture_output, text, creationflags):
            self.last_cmd = cmd
            return type("Result", (), {"returncode": returncode, "stderr": stderr})()
        return run

    def test_default_output_extension_reuses_input_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "recording.mkv")
            open(input_path, "w").close()
            with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                a.transcode_recording(input_path, {})
            self.assertTrue(self.last_cmd[-1].endswith("recording_compressed.mkv"))

    def test_output_extension_overrides_input_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "recording.mkv")
            open(input_path, "w").close()
            with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                a.transcode_recording(
                    input_path,
                    {"args": a.MKV_TO_MP4_PRESERVE_TRACKS_ARGS, "output_extension": ".mp4"},
                )
            self.assertTrue(self.last_cmd[-1].endswith("recording_compressed.mp4"))
            self.assertIn("-map", self.last_cmd)
            self.assertIn("0", self.last_cmd)

    def test_blank_output_extension_falls_back_to_input_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "recording.mkv")
            open(input_path, "w").close()
            with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                a.transcode_recording(input_path, {"output_extension": ""})
            self.assertTrue(self.last_cmd[-1].endswith("recording_compressed.mkv"))

    def test_failed_transcode_does_not_delete_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "recording.mkv")
            open(input_path, "w").close()
            with patch.object(a.subprocess, "run", side_effect=self._fake_run(returncode=1, stderr="boom")):
                with self.assertLogs(level="ERROR"):
                    a.transcode_recording(input_path, {"delete_original": True})
            self.assertTrue(os.path.isfile(input_path))

    def test_successful_transcode_deletes_original_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "recording.mkv")
            open(input_path, "w").close()
            with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                a.transcode_recording(input_path, {"delete_original": True})
            self.assertFalse(os.path.isfile(input_path))

    def test_missing_ffmpeg_is_logged_and_does_not_touch_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "recording.mkv")
            open(input_path, "w").close()
            with patch.object(a, "resolve_ffmpeg_path", return_value=None):
                with self.assertLogs(level="ERROR") as log_ctx:
                    a.transcode_recording(input_path, {"delete_original": True})
            self.assertTrue(any("ffmpeg not found" in message for message in log_ctx.output))
            self.assertTrue(os.path.isfile(input_path))


class ResolveFfmpegPathTests(unittest.TestCase):
    def test_existing_absolute_path_is_used_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = os.path.join(tmp, "ffmpeg.exe")
            open(fake_exe, "w").close()
            self.assertEqual(a.resolve_ffmpeg_path(fake_exe), fake_exe)

    def test_falls_back_to_path_lookup(self):
        with patch.object(a.shutil, "which", return_value=r"C:\PATH\ffmpeg.exe"):
            self.assertEqual(a.resolve_ffmpeg_path("ffmpeg"), r"C:\PATH\ffmpeg.exe")

    def test_falls_back_to_find_ffmpeg_when_nothing_else_resolves(self):
        with patch.object(a.shutil, "which", return_value=None):
            with patch.object(a, "find_ffmpeg", return_value=r"C:\ffmpeg\bin\ffmpeg.exe"):
                self.assertEqual(a.resolve_ffmpeg_path("ffmpeg"), r"C:\ffmpeg\bin\ffmpeg.exe")

    def test_returns_none_when_nothing_resolves_anywhere(self):
        with patch.object(a.shutil, "which", return_value=None):
            with patch.object(a, "find_ffmpeg", return_value=None):
                self.assertIsNone(a.resolve_ffmpeg_path("ffmpeg"))


if __name__ == "__main__":
    unittest.main()
