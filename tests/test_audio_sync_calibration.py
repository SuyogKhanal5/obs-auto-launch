import os
import sys
import tempfile
import wave
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
from tests.fakes import FakeObsClient


def write_mono_wav(path, samples, sr=48000):
    samples_i16 = np.clip(samples, -1.0, 1.0)
    samples_i16 = (samples_i16 * 32767).astype(np.int16)
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(samples_i16.tobytes())


def make_click_track(sr, duration_sec, click_positions_sec, click_len_samples=144, seed=0):
    n = int(sr * duration_sec)
    audio = np.zeros(n, dtype=np.float64)
    rng = np.random.default_rng(seed)
    for pos_sec in click_positions_sec:
        start = int(pos_sec * sr)
        audio[start:start + click_len_samples] = rng.uniform(-1.0, 1.0, click_len_samples)
    return audio


class GenerateCalibrationToneTests(unittest.TestCase):
    def test_writes_a_valid_wav_at_the_configured_rate_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tone.wav")
            a.generate_calibration_tone(path)
            with wave.open(path, "rb") as f:
                self.assertEqual(f.getframerate(), a.CALIBRATION_TONE_SAMPLE_RATE)
                self.assertEqual(f.getnchannels(), 1)
                expected_frames = int(a.CALIBRATION_TONE_SAMPLE_RATE * a.CALIBRATION_TONE_DURATION_SECONDS)
                self.assertEqual(f.getnframes(), expected_frames)

    def test_has_nonzero_signal_at_each_configured_click_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tone.wav")
            a.generate_calibration_tone(path)
            with wave.open(path, "rb") as f:
                sr = f.getframerate()
                data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
            for pos_sec in a.CALIBRATION_CLICK_POSITIONS_SECONDS:
                start = int(pos_sec * sr)
                window = data[start:start + int(a.CALIBRATION_CLICK_DURATION_SECONDS * sr)]
                self.assertGreater(np.abs(window).max(), 0)


class MeasureAudioLagMsTests(unittest.TestCase):
    def _write_pair(self, tmp, sr, duration_sec, click_positions_sec, shift_samples):
        ref = make_click_track(sr, duration_sec, click_positions_sec)
        # A positive shift delays the target signal relative to the reference -- padding with
        # zeros at the front, same as a genuinely later-arriving capture would look.
        if shift_samples >= 0:
            target = np.concatenate([np.zeros(shift_samples), ref])[: len(ref)]
        else:
            target = np.concatenate([ref[-shift_samples:], np.zeros(-shift_samples)])
        ref_path = os.path.join(tmp, "ref.wav")
        target_path = os.path.join(tmp, "target.wav")
        write_mono_wav(ref_path, ref, sr)
        write_mono_wav(target_path, target, sr)
        return ref_path, target_path

    def test_recovers_a_known_positive_shift(self):
        sr = 48000
        shift_ms = 55
        shift_samples = int(shift_ms * sr / 1000)
        with tempfile.TemporaryDirectory() as tmp:
            ref_path, target_path = self._write_pair(tmp, sr, 8.0, [1.0, 3.0, 5.0, 7.0], shift_samples)
            measured = a.measure_audio_lag_ms(ref_path, target_path)
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured, shift_ms, delta=1.0)

    def test_recovers_a_known_negative_shift(self):
        sr = 48000
        shift_ms = -30
        shift_samples = int(shift_ms * sr / 1000)
        with tempfile.TemporaryDirectory() as tmp:
            ref_path, target_path = self._write_pair(tmp, sr, 8.0, [1.0, 3.0, 5.0, 7.0], shift_samples)
            measured = a.measure_audio_lag_ms(ref_path, target_path)
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured, shift_ms, delta=1.0)

    def test_zero_shift_measures_near_zero(self):
        sr = 48000
        with tempfile.TemporaryDirectory() as tmp:
            ref_path, target_path = self._write_pair(tmp, sr, 8.0, [1.0, 3.0, 5.0, 7.0], 0)
            measured = a.measure_audio_lag_ms(ref_path, target_path)
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured, 0.0, delta=1.0)

    def test_silent_target_returns_none(self):
        sr = 48000
        with tempfile.TemporaryDirectory() as tmp:
            ref = make_click_track(sr, 8.0, [1.0, 3.0, 5.0, 7.0])
            ref_path = os.path.join(tmp, "ref.wav")
            target_path = os.path.join(tmp, "target.wav")
            write_mono_wav(ref_path, ref, sr)
            write_mono_wav(target_path, np.zeros_like(ref), sr)
            measured = a.measure_audio_lag_ms(ref_path, target_path)
        self.assertIsNone(measured)

    def test_one_spurious_outlier_does_not_skew_the_median(self):
        # Real AAC-encoded recordings occasionally produce one bogus extra/misaligned peak --
        # median (not mean) is specifically what keeps that from skewing the result.
        sr = 48000
        shift_samples = int(20 * sr / 1000)
        with tempfile.TemporaryDirectory() as tmp:
            ref_path, target_path = self._write_pair(tmp, sr, 8.0, [1.0, 3.0, 5.0, 7.0], shift_samples)
            # Inject one extra, wildly-offset click into the target only.
            with wave.open(target_path, "rb") as f:
                sr2 = f.getframerate()
                data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float64) / 32767.0
            data[int(6.5 * sr2):int(6.5 * sr2) + 144] = 1.0
            write_mono_wav(target_path, data, sr2)
            measured = a.measure_audio_lag_ms(ref_path, target_path)
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured, 20.0, delta=1.0)

    def test_differing_click_smear_pattern_does_not_misalign_pairing(self):
        # AAC's transient smearing can split the *same* real click into a different number of
        # separate above-threshold runs in each signal (a brief below-threshold dip mid-click on
        # one side but not the other). Naively pairing raw per-sample peaks by index would
        # silently misalign every click from that point on; clustering each click down to one
        # point *before* pairing is specifically what prevents that.
        sr = 48000
        shift_ms = 40
        shift_samples = int(shift_ms * sr / 1000)
        with tempfile.TemporaryDirectory() as tmp:
            ref_path, target_path = self._write_pair(tmp, sr, 8.0, [1.0, 3.0, 5.0, 7.0], shift_samples)
            with wave.open(target_path, "rb") as f:
                sr2 = f.getframerate()
                data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float64) / 32767.0
            # Punch a brief (2ms) below-threshold dip through the middle of the second click only
            # -- splits it into two disjoint above-threshold runs in target but not in ref.
            click2_center = int(3.0 * sr2) + shift_samples + 72
            data[click2_center - 48:click2_center + 48] = 0.0
            write_mono_wav(target_path, data, sr2)
            measured = a.measure_audio_lag_ms(ref_path, target_path)
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured, shift_ms, delta=1.0)

    def test_refinement_stays_accurate_under_independent_measurement_noise(self):
        # ref and target get independent noise added on top of the shared shift -- simulating
        # each capture path's own encoding/quantization differences -- verifying the
        # correlation-based refinement (which considers the whole click shape) stays accurate
        # rather than being thrown off by noise the way trusting a single loudest sample would be.
        sr = 48000
        shift_ms = 33
        shift_samples = int(shift_ms * sr / 1000)
        rng = np.random.default_rng(7)
        with tempfile.TemporaryDirectory() as tmp:
            ref_path, target_path = self._write_pair(tmp, sr, 8.0, [1.0, 3.0, 5.0, 7.0], shift_samples)
            for path in (ref_path, target_path):
                with wave.open(path, "rb") as f:
                    sr2 = f.getframerate()
                    data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float64) / 32767.0
                data = np.clip(data + rng.normal(0, 0.02, size=data.shape), -1.0, 1.0)
                write_mono_wav(path, data, sr2)
            measured = a.measure_audio_lag_ms(ref_path, target_path)
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured, shift_ms, delta=1.0)


class MeasureWaveformLagMsTests(unittest.TestCase):
    # Unlike MeasureAudioLagMsTests (built for the sparse-click calibration tone), this measures
    # ordinary continuous content -- the clip editor's "Fix audio track sync" correlating a real
    # clip's own audio directly instead of trusting a fixed pre-calibrated figure.
    def _make_signal(self, sr, duration_sec, seed=0):
        rng = np.random.default_rng(seed)
        n = int(sr * duration_sec)
        envelope = 0.5 + 0.5 * np.sin(np.linspace(0, 40, n))
        return rng.normal(0, 1, n) * envelope

    def _write_pair(self, tmp, sr, duration_sec, shift_samples, noise_sd=0.05, seed=0):
        base = self._make_signal(sr, duration_sec, seed=seed)
        rng = np.random.default_rng(seed + 1)
        ref = base + rng.normal(0, noise_sd, base.shape)
        target = np.zeros_like(base)
        if shift_samples >= 0:
            target[shift_samples:] = base[:len(base) - shift_samples]
        else:
            target[:len(base) + shift_samples] = base[-shift_samples:]
        target = target + rng.normal(0, noise_sd, base.shape)
        ref_path = os.path.join(tmp, "ref.wav")
        target_path = os.path.join(tmp, "target.wav")
        write_mono_wav(ref_path, np.clip(ref, -1.0, 1.0), sr)
        write_mono_wav(target_path, np.clip(target, -1.0, 1.0), sr)
        return ref_path, target_path

    def test_measures_a_positive_lag_when_target_is_delayed(self):
        sr = 48000
        shift_ms = 27.0
        with tempfile.TemporaryDirectory() as tmp:
            ref_path, target_path = self._write_pair(tmp, sr, 5.0, int(shift_ms * sr / 1000))
            measured = a.measure_waveform_lag_ms(ref_path, target_path)
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured, shift_ms, delta=1.0)

    def test_measures_a_negative_lag_when_target_leads(self):
        sr = 48000
        shift_ms = -18.0
        with tempfile.TemporaryDirectory() as tmp:
            ref_path, target_path = self._write_pair(tmp, sr, 5.0, int(shift_ms * sr / 1000))
            measured = a.measure_waveform_lag_ms(ref_path, target_path)
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured, shift_ms, delta=1.0)

    def test_robust_to_independent_capture_noise_on_each_side(self):
        sr = 48000
        shift_ms = 27.0
        with tempfile.TemporaryDirectory() as tmp:
            ref_path, target_path = self._write_pair(
                tmp, sr, 5.0, int(shift_ms * sr / 1000), noise_sd=0.5,
            )
            measured = a.measure_waveform_lag_ms(ref_path, target_path)
        self.assertIsNotNone(measured)
        self.assertAlmostEqual(measured, shift_ms, delta=1.0)

    def test_silent_reference_returns_none(self):
        sr = 48000
        with tempfile.TemporaryDirectory() as tmp:
            ref_path = os.path.join(tmp, "ref.wav")
            target_path = os.path.join(tmp, "target.wav")
            write_mono_wav(ref_path, np.zeros(sr * 3), sr)
            write_mono_wav(target_path, self._make_signal(sr, 3.0), sr)
            self.assertIsNone(a.measure_waveform_lag_ms(ref_path, target_path))

    def test_unrelated_signals_return_none(self):
        sr = 48000
        with tempfile.TemporaryDirectory() as tmp:
            ref_path = os.path.join(tmp, "ref.wav")
            target_path = os.path.join(tmp, "target.wav")
            write_mono_wav(ref_path, self._make_signal(sr, 5.0, seed=1), sr)
            write_mono_wav(target_path, self._make_signal(sr, 5.0, seed=99), sr)
            self.assertIsNone(a.measure_waveform_lag_ms(ref_path, target_path))

    def test_mismatched_sample_rates_return_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref_path = os.path.join(tmp, "ref.wav")
            target_path = os.path.join(tmp, "target.wav")
            write_mono_wav(ref_path, self._make_signal(48000, 3.0), 48000)
            write_mono_wav(target_path, self._make_signal(44100, 3.0), 44100)
            self.assertIsNone(a.measure_waveform_lag_ms(ref_path, target_path))


class ExtractAudioTrackWavTests(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = self._tmp_ctx.name
        self.addCleanup(self._tmp_ctx.cleanup)
        self.out_wav = os.path.join(self.tmp, "out.wav")

    def _fake_run(self, returncode=0, write_output=True, stderr=""):
        def run(cmd, capture_output, text, creationflags, timeout, **kwargs):
            self.last_cmd = cmd
            if write_output:
                with open(cmd[-1], "wb") as f:
                    f.write(b"x" * 100)
            return type("Result", (), {"returncode": returncode, "stderr": stderr})()
        return run

    def test_maps_the_requested_track_and_downmixes_to_mono(self):
        with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
            result = a.extract_audio_track_wav("ffmpeg", "in.mp4", 3, self.out_wav)
        self.assertTrue(result)
        self.assertIn("0:a:2", self.last_cmd)
        self.assertIn("-ac", self.last_cmd)
        self.assertEqual(self.last_cmd[self.last_cmd.index("-ac") + 1], "1")

    def test_includes_ss_and_t_when_given(self):
        with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
            a.extract_audio_track_wav("ffmpeg", "in.mp4", 1, self.out_wav, start_seconds=5, duration_seconds=10)
        self.assertIn("-ss", self.last_cmd)
        self.assertIn("-t", self.last_cmd)

    def test_omits_ss_and_t_when_not_given(self):
        with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
            a.extract_audio_track_wav("ffmpeg", "in.mp4", 1, self.out_wav)
        self.assertNotIn("-ss", self.last_cmd)
        self.assertNotIn("-t", self.last_cmd)

    def test_nonzero_exit_code_returns_false(self):
        with patch.object(a.subprocess, "run", side_effect=self._fake_run(returncode=1, stderr="boom")):
            with self.assertLogs(level="WARNING"):
                self.assertFalse(a.extract_audio_track_wav("ffmpeg", "in.mp4", 1, self.out_wav))

    def test_missing_output_returns_false(self):
        with patch.object(a.subprocess, "run", side_effect=self._fake_run(write_output=False)):
            with self.assertLogs(level="WARNING"):
                self.assertFalse(a.extract_audio_track_wav("ffmpeg", "in.mp4", 1, self.out_wav))

    def test_run_failure_returns_false_not_raised(self):
        with patch.object(a.subprocess, "run", side_effect=OSError("boom")):
            with self.assertLogs(level="WARNING"):
                self.assertFalse(a.extract_audio_track_wav("ffmpeg", "in.mp4", 1, self.out_wav))

    def test_timeout_returns_false_not_raised(self):
        with patch.object(a.subprocess, "run", side_effect=a.subprocess.TimeoutExpired(cmd="ffmpeg", timeout=20)):
            with self.assertLogs(level="WARNING"):
                self.assertFalse(a.extract_audio_track_wav("ffmpeg", "in.mp4", 1, self.out_wav))


class MeasureClipAudioSyncShiftsMsTests(unittest.TestCase):
    def test_reference_extraction_failure_returns_empty(self):
        with patch.object(a, "extract_audio_track_wav", return_value=False):
            result = a.measure_clip_audio_sync_shifts_ms("ffmpeg", "in.mp4", 1, [3, 4])
        self.assertEqual(result, {})

    def test_measures_each_target_track_independently(self):
        lag_by_track = {3: 26.6, 4: 12.1}

        def fake_measure(ref_wav, target_wav, **kwargs):
            for track, ms in lag_by_track.items():
                if f"target_{track}" in target_wav:
                    return ms
            return None

        with patch.object(a, "extract_audio_track_wav", return_value=True):
            with patch.object(a, "measure_waveform_lag_ms", side_effect=fake_measure):
                result = a.measure_clip_audio_sync_shifts_ms("ffmpeg", "in.mp4", 1, [3, 4])
        self.assertEqual(result, {3: -27, 4: -12})

    def test_track_missing_from_result_when_extraction_fails(self):
        def fake_extract(ffmpeg_path, input_path, track_number, output_wav_path, start_seconds=0, duration_seconds=None):
            return track_number != 4  # track 4's own extraction fails

        with patch.object(a, "extract_audio_track_wav", side_effect=fake_extract):
            with patch.object(a, "measure_waveform_lag_ms", return_value=10.0):
                result = a.measure_clip_audio_sync_shifts_ms("ffmpeg", "in.mp4", 1, [3, 4])
        self.assertEqual(result, {3: -10})

    def test_track_missing_from_result_when_not_confidently_measured(self):
        with patch.object(a, "extract_audio_track_wav", return_value=True):
            with patch.object(a, "measure_waveform_lag_ms", return_value=None):
                result = a.measure_clip_audio_sync_shifts_ms("ffmpeg", "in.mp4", 1, [3, 4])
        self.assertEqual(result, {})

    def test_progress_callback_invoked_per_track_before_measuring_it(self):
        calls = []

        def on_progress(index, total, track):
            calls.append((index, total, track))

        with patch.object(a, "extract_audio_track_wav", return_value=True):
            with patch.object(a, "measure_waveform_lag_ms", return_value=5.0):
                a.measure_clip_audio_sync_shifts_ms(
                    "ffmpeg", "in.mp4", 1, [3, 4, 5], progress_callback=on_progress,
                )
        self.assertEqual(calls, [(0, 3, 3), (1, 3, 4), (2, 3, 5)])

    def test_progress_callback_exception_does_not_abort_measurement(self):
        def bad_progress(index, total, track):
            raise RuntimeError("boom")

        with patch.object(a, "extract_audio_track_wav", return_value=True):
            with patch.object(a, "measure_waveform_lag_ms", return_value=5.0):
                result = a.measure_clip_audio_sync_shifts_ms(
                    "ffmpeg", "in.mp4", 1, [3], progress_callback=bad_progress,
                )
        self.assertEqual(result, {3: -5})

    def test_stops_early_once_overall_budget_exceeded(self):
        call_count = {"n": 0}

        def fake_extract(ffmpeg_path, input_path, track_number, output_wav_path, start_seconds=0, duration_seconds=None):
            call_count["n"] += 1
            return True

        # Force time.time() to already be past the deadline by the time the loop checks it, by
        # making the deadline computation and the loop's own check both see the same "now".
        real_time = a.time.time
        with patch.object(a, "extract_audio_track_wav", side_effect=fake_extract):
            with patch.object(a.time, "time", side_effect=[real_time(), real_time() + 10_000, real_time() + 10_000]):
                result = a.measure_clip_audio_sync_shifts_ms("ffmpeg", "in.mp4", 1, [3, 4, 5])
        self.assertEqual(result, {})
        self.assertEqual(call_count["n"], 1)  # only the reference extraction ran

    def test_passes_window_through_to_extraction(self):
        calls = []

        def fake_extract(ffmpeg_path, input_path, track_number, output_wav_path, start_seconds=0, duration_seconds=None):
            calls.append((track_number, start_seconds, duration_seconds))
            return True

        with patch.object(a, "extract_audio_track_wav", side_effect=fake_extract):
            with patch.object(a, "measure_waveform_lag_ms", return_value=5.0):
                a.measure_clip_audio_sync_shifts_ms("ffmpeg", "in.mp4", 2, [4], start_seconds=10, duration_seconds=20)
        self.assertEqual(calls, [(2, 10, 20), (4, 10, 20)])


class RunAudioSyncCalibrationSafetyTests(unittest.TestCase):
    def test_refuses_to_run_while_obs_is_already_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            ffmpeg_path = os.path.join(tmp, "ffmpeg.exe")
            open(ffmpeg_path, "w").close()
            open(os.path.join(tmp, "ffplay.exe"), "w").close()
            client = FakeObsClient(inputs={"Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {}}})
            client.recording_active = True
            with self.assertLogs(level="WARNING"):
                result = a.run_audio_sync_calibration(client, ffmpeg_path, "Game Audio")
        self.assertIsNone(result)
        self.assertNotIn("start_record", [c[0] for c in client.calls])

    def test_missing_ffplay_returns_none_without_touching_obs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ffmpeg_path = os.path.join(tmp, "ffmpeg.exe")
            open(ffmpeg_path, "w").close()
            # deliberately no ffplay.exe written alongside it
            client = FakeObsClient(inputs={"Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {}}})
            with self.assertLogs(level="WARNING"):
                result = a.run_audio_sync_calibration(client, ffmpeg_path, "Game Audio")
        self.assertIsNone(result)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
