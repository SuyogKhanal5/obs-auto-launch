import os
import sys
import tempfile
import wave
import unittest

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
