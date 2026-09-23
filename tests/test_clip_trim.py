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


class ParseTrackNumbersTests(unittest.TestCase):
    def test_parses_comma_separated_list(self):
        self.assertEqual(a.parse_track_numbers("3,4,5"), [3, 4, 5])

    def test_strips_whitespace_around_entries(self):
        self.assertEqual(a.parse_track_numbers(" 3 , 4 ,5 "), [3, 4, 5])

    def test_single_number(self):
        self.assertEqual(a.parse_track_numbers("3"), [3])

    def test_blank_and_none_return_empty_list(self):
        self.assertEqual(a.parse_track_numbers(""), [])
        self.assertEqual(a.parse_track_numbers(None), [])

    def test_drops_non_numeric_and_non_positive_entries(self):
        self.assertEqual(a.parse_track_numbers("3,abc,-1,0,5"), [3, 5])

    def test_trailing_comma_ignored(self):
        self.assertEqual(a.parse_track_numbers("3,4,"), [3, 4])


class BuildAudioTrackFilterArgsTests(unittest.TestCase):
    def test_nothing_requested_returns_empty(self):
        self.assertEqual(a.build_audio_track_filter_args(6), ([], []))
        self.assertEqual(a.build_audio_track_filter_args(6, shift_ms_by_track={3: 0}), ([], []))
        self.assertEqual(a.build_audio_track_filter_args(6, shift_ms_by_track={}), ([], []))
        self.assertEqual(a.build_audio_track_filter_args(0, shift_ms_by_track={3: 27}), ([], []))
        self.assertEqual(a.build_audio_track_filter_args(None, shift_ms_by_track={3: 27}), ([], []))
        self.assertEqual(a.build_audio_track_filter_args(6, muted_tracks=[]), ([], []))

    def test_tracks_entirely_outside_stream_count_return_empty(self):
        self.assertEqual(a.build_audio_track_filter_args(3, shift_ms_by_track={9: 27}), ([], []))
        self.assertEqual(a.build_audio_track_filter_args(3, muted_tracks=[9]), ([], []))

    def test_positive_shift_uses_adelay_on_the_selected_track(self):
        filter_args, map_args = a.build_audio_track_filter_args(3, shift_ms_by_track={2: 27})
        filter_complex = filter_args[1]
        self.assertIn("[0:a:1]adelay=27:all=1[atrack1]", filter_complex)
        self.assertIn("[0:a:0]anull[atrack0]", filter_complex)
        self.assertIn("[0:a:2]anull[atrack2]", filter_complex)
        self.assertEqual(map_args, ["-map", "[atrack0]", "-map", "[atrack1]", "-map", "[atrack2]"])

    def test_negative_shift_uses_atrim_and_resets_timestamps(self):
        filter_args, _map_args = a.build_audio_track_filter_args(2, shift_ms_by_track={1: -27})
        filter_complex = filter_args[1]
        self.assertIn("atrim=start=0.027000", filter_complex)
        self.assertIn("asetpts=PTS-STARTPTS", filter_complex)

    def test_every_stream_gets_mapped_even_when_only_one_is_shifted(self):
        _filter_args, map_args = a.build_audio_track_filter_args(4, shift_ms_by_track={1: 10})
        self.assertEqual(len(map_args), 8)  # 4 "-map" + 4 labels

    def test_multiple_tracks_can_be_shifted_at_once_by_different_amounts(self):
        # Confirmed live: real process-capture tracks in the same recording can each lag Desktop
        # Audio by a genuinely different amount (e.g. Discord's own jitter-buffer pipeline adds
        # latency a locally-rendered game never has) -- a single shared value isn't enough.
        filter_args, _map_args = a.build_audio_track_filter_args(3, shift_ms_by_track={1: 10, 3: -18})
        filter_complex = filter_args[1]
        self.assertIn("[0:a:0]adelay=10:all=1[atrack0]", filter_complex)
        self.assertIn("[0:a:2]atrim=start=0.018000,asetpts=PTS-STARTPTS[atrack2]", filter_complex)
        self.assertIn("[0:a:1]anull[atrack1]", filter_complex)

    def test_filter_complex_flag_present(self):
        filter_args, _map_args = a.build_audio_track_filter_args(2, shift_ms_by_track={1: 10})
        self.assertEqual(filter_args[0], "-filter_complex")

    def test_muted_track_uses_volume_zero(self):
        filter_args, map_args = a.build_audio_track_filter_args(3, muted_tracks=[2])
        filter_complex = filter_args[1]
        self.assertIn("[0:a:1]volume=0[atrack1]", filter_complex)
        self.assertIn("[0:a:0]anull[atrack0]", filter_complex)
        self.assertIn("[0:a:2]anull[atrack2]", filter_complex)
        self.assertEqual(map_args, ["-map", "[atrack0]", "-map", "[atrack1]", "-map", "[atrack2]"])

    def test_multiple_tracks_can_be_muted_at_once(self):
        filter_args, _map_args = a.build_audio_track_filter_args(3, muted_tracks=[1, 3])
        filter_complex = filter_args[1]
        self.assertIn("[0:a:0]volume=0[atrack0]", filter_complex)
        self.assertIn("[0:a:2]volume=0[atrack2]", filter_complex)
        self.assertIn("[0:a:1]anull[atrack1]", filter_complex)

    def test_mute_and_shift_can_apply_to_different_tracks_together(self):
        filter_args, _map_args = a.build_audio_track_filter_args(3, shift_ms_by_track={1: 27}, muted_tracks=[3])
        filter_complex = filter_args[1]
        self.assertIn("[0:a:0]adelay=27:all=1[atrack0]", filter_complex)
        self.assertIn("[0:a:2]volume=0[atrack2]", filter_complex)
        self.assertIn("[0:a:1]anull[atrack1]", filter_complex)

    def test_mute_wins_when_a_track_is_both_shifted_and_muted(self):
        filter_args, _map_args = a.build_audio_track_filter_args(2, shift_ms_by_track={1: 27}, muted_tracks=[1])
        filter_complex = filter_args[1]
        self.assertIn("[0:a:0]volume=0[atrack0]", filter_complex)
        self.assertNotIn("adelay", filter_complex)


class BuildAudioRoutingFilterArgsTests(unittest.TestCase):
    def test_pure_identity_with_nothing_customized_returns_empty(self):
        self.assertEqual(a.build_audio_routing_filter_args(3), ([], []))
        self.assertEqual(
            a.build_audio_routing_filter_args(3, routing={1: [1], 2: [2], 3: [3]}), ([], []),
        )

    def test_zero_stream_count_returns_empty(self):
        self.assertEqual(a.build_audio_routing_filter_args(0, routing={1: [1, 2]}), ([], []))
        self.assertEqual(a.build_audio_routing_filter_args(None, routing={1: [1, 2]}), ([], []))

    def test_consolidating_two_sources_into_one_destination_uses_amix(self):
        # "make output track 1 a combination of tracks 2 and 3"
        filter_args, map_args = a.build_audio_routing_filter_args(3, routing={1: [2, 3]})
        filter_complex = filter_args[1]
        self.assertIn("[0:a:1]anull[asrc2]", filter_complex)
        self.assertIn("[0:a:2]anull[asrc3]", filter_complex)
        self.assertIn("[asrc2][asrc3]amix=inputs=2:duration=longest:dropout_transition=0[amix1]", filter_complex)
        self.assertEqual(map_args, ["-map", "[amix1]"])

    def test_dropping_a_source_by_never_routing_it_anywhere(self):
        # "remove the desktop audio track" -- source 1 simply isn't listed under any destination.
        filter_args, map_args = a.build_audio_routing_filter_args(3, routing={2: [2], 3: [3]})
        filter_complex = filter_args[1]
        self.assertNotIn("0:a:0", filter_complex)
        self.assertEqual(map_args, ["-map", "[asrc2]", "-map", "[asrc3]"])

    def test_single_source_destination_is_a_straight_passthrough_label(self):
        filter_args, map_args = a.build_audio_routing_filter_args(2, routing={1: [1]}, muted_destinations=[2])
        filter_complex = filter_args[1]
        self.assertNotIn("amix", filter_complex)
        self.assertIn("[asrc1]", map_args)

    def test_mute_applies_after_mixing_not_to_individual_sources(self):
        filter_args, map_args = a.build_audio_routing_filter_args(3, routing={1: [2, 3]}, muted_destinations=[1])
        filter_complex = filter_args[1]
        self.assertIn("amix=inputs=2", filter_complex)
        self.assertIn("[amix1]volume=0[adest1]", filter_complex)
        self.assertEqual(map_args, ["-map", "[adest1]"])

    def test_shift_applies_to_source_before_mixing(self):
        filter_args, _map_args = a.build_audio_routing_filter_args(
            3, routing={1: [2, 3]}, shift_ms_by_track={2: 27},
        )
        filter_complex = filter_args[1]
        self.assertIn("[0:a:1]adelay=27:all=1[asrc2]", filter_complex)
        self.assertIn("[asrc2][asrc3]amix", filter_complex)

    def test_destination_with_no_valid_sources_is_omitted(self):
        filter_args, map_args = a.build_audio_routing_filter_args(3, routing={1: [1], 2: []})
        self.assertEqual(map_args, ["-map", "[asrc1]"])

    def test_all_destinations_empty_returns_empty(self):
        self.assertEqual(a.build_audio_routing_filter_args(3, routing={1: [], 2: [], 3: []}), ([], []))

    def test_destinations_emitted_in_ascending_order(self):
        # A routing dict expressed with the SAME content as the identity mapping, just in a
        # different key order, is still identity (Python dicts compare by content, not
        # insertion order) -- muting one destination is what actually forces this out of the
        # trivial "nothing to do" fast path so ascending-order emission is exercised for real.
        _filter_args, map_args = a.build_audio_routing_filter_args(
            3, routing={3: [3], 1: [1], 2: [2]}, muted_destinations=[2],
        )
        self.assertEqual(map_args, ["-map", "[asrc1]", "-map", "[adest2]", "-map", "[asrc3]"])

    def test_a_source_can_feed_multiple_destinations(self):
        filter_args, map_args = a.build_audio_routing_filter_args(2, routing={1: [1], 2: [1, 2]})
        filter_complex = filter_args[1]
        self.assertIn("[asrc1]", map_args)
        self.assertIn("[asrc1][asrc2]amix=inputs=2", filter_complex)

    def test_zero_or_blank_gains_are_a_no_op(self):
        self.assertEqual(
            a.build_audio_routing_filter_args(3, routing={1: [1], 2: [2], 3: [3]}, gains_db={1: {1: 0}}),
            ([], []),
        )

    def test_gain_on_single_source_destination_inserts_volume_filter(self):
        filter_args, map_args = a.build_audio_routing_filter_args(2, routing={1: [1]}, gains_db={1: {1: 12}})
        filter_complex = filter_args[1]
        self.assertIn("[asrc1]volume=12dB[again1_1]", filter_complex)
        self.assertEqual(map_args, ["-map", "[again1_1]"])

    def test_same_source_gets_different_gain_on_different_destinations(self):
        # The example that motivated this feature: boost a mic +12dB on one output track but
        # leave it unboosted (0dB, i.e. no filter at all) on another output it's also routed to.
        filter_args, map_args = a.build_audio_routing_filter_args(
            2, routing={1: [1], 2: [1]}, gains_db={1: {1: 12}},
        )
        filter_complex = filter_args[1]
        self.assertIn("[asrc1]volume=12dB[again1_1]", filter_complex)
        self.assertNotIn("again2_1", filter_complex)
        self.assertEqual(map_args, ["-map", "[again1_1]", "-map", "[asrc1]"])

    def test_gain_applies_before_mixing_with_an_unboosted_source(self):
        filter_args, map_args = a.build_audio_routing_filter_args(
            2, routing={1: [1, 2]}, gains_db={1: {1: -6}},
        )
        filter_complex = filter_args[1]
        self.assertIn("[asrc1]volume=-6dB[again1_1]", filter_complex)
        self.assertIn("[again1_1][asrc2]amix=inputs=2", filter_complex)
        self.assertEqual(map_args, ["-map", "[amix1]"])

    def test_gain_disables_amixs_own_normalize_so_the_boost_actually_lands(self):
        # Confirmed live: amix's own "normalize" defaults ON and silently rescales every input by
        # 1/N regardless of any gain already applied -- e.g. a +12dB boost on one of 2 mixed
        # sources measured a further -6dB from amix itself on top, almost completely canceling
        # the boost out. Once any gain is set for a destination, amix's own auto-balancing has
        # to be turned off there, or the whole feature would silently defeat itself the moment
        # 2+ sources land in the same output.
        filter_args, _map_args = a.build_audio_routing_filter_args(
            3, routing={1: [1, 2, 3]}, gains_db={1: {1: 12}},
        )
        filter_complex = filter_args[1]
        self.assertIn("amix=inputs=3:duration=longest:dropout_transition=0:normalize=0[amix1]", filter_complex)

    def test_no_gain_leaves_amixs_default_normalize_untouched(self):
        # Pure track consolidation (the original, gain-less feature) must keep its prior
        # behavior exactly -- amix's own default auto-balancing across sources being combined
        # with no explicit gain involved is the whole point there, unlike the case above.
        filter_args, _map_args = a.build_audio_routing_filter_args(3, routing={1: [1, 2, 3]})
        filter_complex = filter_args[1]
        self.assertIn("amix=inputs=3:duration=longest:dropout_transition=0[amix1]", filter_complex)
        self.assertNotIn("normalize", filter_complex)

    def test_gain_and_mute_stack_on_the_same_destination(self):
        filter_args, map_args = a.build_audio_routing_filter_args(
            2, routing={1: [1]}, muted_destinations=[1], gains_db={1: {1: 12}},
        )
        filter_complex = filter_args[1]
        self.assertIn("[asrc1]volume=12dB[again1_1]", filter_complex)
        self.assertIn("[again1_1]volume=0[adest1]", filter_complex)
        self.assertEqual(map_args, ["-map", "[adest1]"])

    def test_gain_for_an_unrouted_source_is_ignored(self):
        # Identity routing (default) means destination 1's only source is 1, not 2 -- a stray
        # gain entry filed under the wrong destination/source pair should be silently irrelevant
        # rather than forcing a real filter graph or leaking into the output.
        self.assertEqual(
            a.build_audio_routing_filter_args(2, gains_db={1: {2: 12}}),
            ([], []),
        )


class ProbeAudioStreamCountTests(unittest.TestCase):
    def test_returns_none_when_ffprobe_not_found_next_to_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_ffmpeg = os.path.join(tmp, "ffmpeg.exe")
            open(fake_ffmpeg, "w").close()
            self.assertIsNone(a.probe_audio_stream_count(fake_ffmpeg, "in.mp4"))

    def test_counts_audio_stream_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_ffmpeg = os.path.join(tmp, "ffmpeg.exe")
            open(fake_ffmpeg, "w").close()
            fake_ffprobe = os.path.join(tmp, "ffprobe.exe")
            open(fake_ffprobe, "w").close()

            def run(cmd, capture_output, text, creationflags=0, timeout=None):
                return type("Result", (), {"returncode": 0, "stdout": "1\n2\n3\n"})()

            with patch.object(a.subprocess, "run", side_effect=run):
                self.assertEqual(a.probe_audio_stream_count(fake_ffmpeg, "in.mp4"), 3)

    def test_nonzero_exit_code_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_ffmpeg = os.path.join(tmp, "ffmpeg.exe")
            open(fake_ffmpeg, "w").close()
            fake_ffprobe = os.path.join(tmp, "ffprobe.exe")
            open(fake_ffprobe, "w").close()

            def run(cmd, capture_output, text, creationflags=0, timeout=None):
                return type("Result", (), {"returncode": 1, "stdout": ""})()

            with patch.object(a.subprocess, "run", side_effect=run):
                self.assertIsNone(a.probe_audio_stream_count(fake_ffmpeg, "in.mp4"))

    def test_run_failure_returns_none_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_ffmpeg = os.path.join(tmp, "ffmpeg.exe")
            open(fake_ffmpeg, "w").close()
            fake_ffprobe = os.path.join(tmp, "ffprobe.exe")
            open(fake_ffprobe, "w").close()
            with patch.object(a.subprocess, "run", side_effect=OSError("boom")):
                self.assertIsNone(a.probe_audio_stream_count(fake_ffmpeg, "in.mp4"))


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

    def test_maps_only_video_and_audio_not_every_stream(self):
        # "-map 0" would also stream-copy OBS's own chapter-marker metadata track -- confirmed
        # live that its internal timestamps can't be rebased by a plain copy, producing a wildly
        # wrong duration on that track that's very likely what made VLC misreport the trimmed
        # clip's overall length.
        cmd = a.build_trim_command("ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False)
        map_targets = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
        self.assertEqual(sorted(map_targets), ["0:a", "0:v"])

    def test_precise_mode_also_maps_only_video_and_audio(self):
        cmd = a.build_trim_command("ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=True)
        map_targets = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
        self.assertEqual(sorted(map_targets), ["0:a", "0:v"])

    def test_fast_mode_moves_moov_atom_to_front(self):
        # Needed for reliable playback in browsers/embedded web players (e.g. Discord's inline
        # preview) -- confirmed live that a plain stream-copy trim without this could play fine
        # in VLC but still misbehave there.
        cmd = a.build_trim_command("ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False)
        self.assertEqual(cmd[cmd.index("-movflags") + 1], "+faststart")
        self.assertEqual(cmd[-1], "out.mp4")

    def test_reencode_modes_also_move_moov_atom_to_front(self):
        cmd = a.build_trim_command("ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=True, crf=20)
        self.assertEqual(cmd[cmd.index("-movflags") + 1], "+faststart")
        self.assertEqual(cmd[-1], "out.mp4")

    def test_never_produces_bitrate_targeting_flags(self):
        # A target output size needs a fundamentally different (two-pass) command shape to hit
        # accurately -- build_trim_command itself never takes a size target at all anymore; see
        # BuildTwoPassSizeTargetedCommandsTests below.
        cmd = a.build_trim_command("ffmpeg", "in.mp4", 0, 10, "out.mp4", precise=True)
        self.assertNotIn("-b:v", cmd)
        self.assertNotIn("-maxrate", cmd)

    def test_uses_configured_ffmpeg_path(self):
        cmd = a.build_trim_command(r"C:\custom\ffmpeg.exe", "in.mkv", 0, 5, "out.mkv")
        self.assertEqual(cmd[0], r"C:\custom\ffmpeg.exe")

    def test_explicit_crf_forces_reencode_even_in_fast_mode(self):
        # A stream copy can't change quality at all -- picking a quality forces a real encode
        # even when "Precise" isn't checked.
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 10, "out.mkv", precise=False, crf=23)
        self.assertNotIn("copy", cmd)
        self.assertIn("-crf", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "23")
        i_index = cmd.index("-i")
        ss_index = cmd.index("-ss")
        self.assertLess(ss_index, i_index)  # still fast-seeks before -i despite re-encoding

    def test_explicit_crf_with_precise_seeks_after_input(self):
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 10, "out.mkv", precise=True, crf=28)
        i_index = cmd.index("-i")
        ss_index = cmd.index("-ss")
        self.assertGreater(ss_index, i_index)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "28")

    def test_precise_without_crf_uses_default_crf(self):
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 10, "out.mkv", precise=True, crf=None)
        self.assertEqual(cmd[cmd.index("-crf") + 1], str(a.CLIP_EDITOR_DEFAULT_CRF))

    def test_fast_mode_with_no_crf_still_stream_copies(self):
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 10, "out.mkv", precise=False, crf=None)
        self.assertIn("copy", cmd)
        self.assertNotIn("-crf", cmd)

    def test_scale_height_forces_reencode_even_in_fast_mode(self):
        # A stream copy can't rescale video at all -- picking a resolution forces a real encode
        # even when "Precise" isn't checked.
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 10, "out.mkv", precise=False, scale_height=720)
        self.assertNotIn("copy", cmd)
        self.assertIn("-vf", cmd)
        self.assertEqual(cmd[cmd.index("-vf") + 1], "scale=-2:720")
        self.assertIn("-crf", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], str(a.CLIP_EDITOR_DEFAULT_CRF))
        i_index = cmd.index("-i")
        ss_index = cmd.index("-ss")
        self.assertLess(ss_index, i_index)  # still fast-seeks before -i despite re-encoding

    def test_scale_height_with_precise_seeks_after_input(self):
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 10, "out.mkv", precise=True, scale_height=480)
        i_index = cmd.index("-i")
        ss_index = cmd.index("-ss")
        self.assertGreater(ss_index, i_index)
        self.assertEqual(cmd[cmd.index("-vf") + 1], "scale=-2:480")

    def test_no_scale_height_omits_vf_flag(self):
        cmd = a.build_trim_command("ffmpeg", "in.mkv", 5, 10, "out.mkv", precise=True)
        self.assertNotIn("-vf", cmd)

    def test_audio_shift_in_fast_mode_keeps_video_copy_but_reencodes_audio(self):
        cmd = a.build_trim_command(
            "ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False,
            audio_shift_ms_by_track={2: 27}, audio_stream_count=2,
        )
        self.assertIn("-filter_complex", cmd)
        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertIn("-c:a", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        i_index = cmd.index("-i")
        ss_index = cmd.index("-ss")
        self.assertLess(ss_index, i_index)  # still fast-seeks before -i

    def test_audio_shift_maps_every_track_via_filter_labels(self):
        cmd = a.build_trim_command(
            "ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False,
            audio_shift_ms_by_track={2: 27}, audio_stream_count=2,
        )
        map_targets = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
        self.assertIn("0:v", map_targets)
        self.assertIn("[asrc1]", map_targets)
        self.assertIn("[asrc2]", map_targets)
        self.assertNotIn("0:a", map_targets)

    def test_no_shift_still_maps_plain_audio_wildcard(self):
        cmd = a.build_trim_command("ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False)
        map_targets = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
        self.assertEqual(sorted(map_targets), ["0:a", "0:v"])
        self.assertNotIn("-filter_complex", cmd)

    def test_audio_shift_combined_with_precise_still_reencodes_video(self):
        cmd = a.build_trim_command(
            "ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=True,
            audio_shift_ms_by_track={2: 27}, audio_stream_count=2,
        )
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertIn("-filter_complex", cmd)

    def test_missing_audio_stream_count_falls_back_to_plain_wildcard(self):
        # audio_shift_ms_by_track given but no audio_stream_count -- can't safely build the
        # filter graph without knowing the real track count, so this must behave exactly like no
        # shift was requested at all rather than guessing.
        cmd = a.build_trim_command(
            "ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False,
            audio_shift_ms_by_track={2: 27}, audio_stream_count=None,
        )
        self.assertNotIn("-filter_complex", cmd)
        self.assertIn("copy", cmd)

    def test_mute_tracks_in_fast_mode_keeps_video_copy_but_reencodes_audio(self):
        cmd = a.build_trim_command(
            "ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False,
            muted_destinations=[2], audio_stream_count=3,
        )
        self.assertIn("-filter_complex", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[asrc2]volume=0[adest2]", filter_complex)

    def test_mute_and_shift_combine_in_one_filter_graph(self):
        cmd = a.build_trim_command(
            "ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False,
            audio_shift_ms_by_track={1: 27}, muted_destinations=[3], audio_stream_count=3,
        )
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("adelay=27:all=1", filter_complex)
        self.assertIn("volume=0", filter_complex)

    def test_missing_audio_stream_count_falls_back_to_plain_wildcard_for_mute_too(self):
        cmd = a.build_trim_command(
            "ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False, muted_destinations=[2], audio_stream_count=None,
        )
        self.assertNotIn("-filter_complex", cmd)
        self.assertIn("copy", cmd)

    def test_consolidating_tracks_via_routing(self):
        cmd = a.build_trim_command(
            "ffmpeg", "in.mp4", 5, 10, "out.mp4", precise=False,
            audio_routing={1: [2, 3]}, audio_stream_count=3,
        )
        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("amix=inputs=2", filter_complex)


class BuildTwoPassSizeTargetedCommandsTests(unittest.TestCase):
    def test_pass1_analyzes_video_only_and_discards_output(self):
        pass1, _pass2 = a.build_two_pass_size_targeted_commands(
            "ffmpeg", "in.mp4", 0, 10, "out.mp4", target_size_mb=5, passlog_prefix="prefix",
        )
        self.assertIn("-an", pass1)
        self.assertEqual(pass1[pass1.index("-pass") + 1], "1")
        self.assertNotIn("out.mp4", pass1)
        map_targets = [pass1[i + 1] for i, arg in enumerate(pass1) if arg == "-map"]
        self.assertEqual(map_targets, ["0:v"])

    def test_pass2_encodes_video_and_audio_to_the_real_output(self):
        _pass1, pass2 = a.build_two_pass_size_targeted_commands(
            "ffmpeg", "in.mp4", 0, 10, "out.mp4", target_size_mb=5, passlog_prefix="prefix",
        )
        self.assertEqual(pass2[pass2.index("-pass") + 1], "2")
        self.assertEqual(pass2[-1], "out.mp4")
        # Only the first audio track -- confirmed live that including every track on a
        # multi-track recording blows the size target way past what the bitrate math accounts
        # for (each extra track adds its own ~128 kbps on top of the single-track budget).
        map_targets = [pass2[i + 1] for i, arg in enumerate(pass2) if arg == "-map"]
        self.assertEqual(sorted(map_targets), ["0:a:0", "0:v"])
        self.assertEqual(pass2[pass2.index("-movflags") + 1], "+faststart")

    def test_both_passes_use_the_same_bitrate_and_passlog(self):
        pass1, pass2 = a.build_two_pass_size_targeted_commands(
            "ffmpeg", "in.mp4", 0, 10, "out.mp4", target_size_mb=5, passlog_prefix="myprefix",
        )
        self.assertEqual(pass1[pass1.index("-b:v") + 1], pass2[pass2.index("-b:v") + 1])
        self.assertEqual(pass1[pass1.index("-passlogfile") + 1], "myprefix")
        self.assertEqual(pass2[pass2.index("-passlogfile") + 1], "myprefix")

    def test_both_passes_seek_after_input_for_frame_accuracy(self):
        pass1, pass2 = a.build_two_pass_size_targeted_commands(
            "ffmpeg", "in.mp4", 5, 10, "out.mp4", target_size_mb=5, passlog_prefix="prefix",
        )
        for cmd in (pass1, pass2):
            self.assertGreater(cmd.index("-ss"), cmd.index("-i"))

    def test_scale_height_applies_to_both_passes(self):
        pass1, pass2 = a.build_two_pass_size_targeted_commands(
            "ffmpeg", "in.mp4", 0, 10, "out.mp4", target_size_mb=5, scale_height=720, passlog_prefix="prefix",
        )
        for cmd in (pass1, pass2):
            self.assertEqual(cmd[cmd.index("-vf") + 1], "scale=-2:720")


class ComputeTargetVideoBitrateKbpsTests(unittest.TestCase):
    def test_none_target_returns_none(self):
        self.assertIsNone(a.compute_target_video_bitrate_kbps(60, None))

    def test_zero_or_negative_duration_returns_none(self):
        self.assertIsNone(a.compute_target_video_bitrate_kbps(0, 10))
        self.assertIsNone(a.compute_target_video_bitrate_kbps(-5, 10))

    def test_computes_a_sane_bitrate_for_a_typical_clip(self):
        # 10 MB over 60s, minus the fixed 128 kbps audio share -- should land in a plausible
        # video-bitrate range, comfortably under a pure (no-margin, no-audio) estimate.
        kbps = a.compute_target_video_bitrate_kbps(60, 10)
        naive_estimate = 10 * 8192 / 60
        self.assertLess(kbps, naive_estimate)
        self.assertGreater(kbps, 0)

    def test_returns_none_when_budget_is_smaller_than_audio_alone(self):
        # A tiny target size over a long duration leaves no room for video at all once audio's
        # fixed share is subtracted -- must not return a zero or negative bitrate.
        self.assertIsNone(a.compute_target_video_bitrate_kbps(3600, 1))


class GetQualityScaleHeightTests(unittest.TestCase):
    def test_same_as_source_returns_none(self):
        self.assertIsNone(a.get_quality_scale_height("Same as source"))

    def test_known_presets_map_to_expected_height(self):
        self.assertEqual(a.get_quality_scale_height("1080p"), 1080)
        self.assertEqual(a.get_quality_scale_height("720p"), 720)
        self.assertEqual(a.get_quality_scale_height("480p"), 480)

    def test_unknown_choice_returns_none(self):
        self.assertIsNone(a.get_quality_scale_height("nonsense"))


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
        def run(cmd, capture_output, text, creationflags=0):
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

    def test_target_size_runs_two_passes_and_cleans_up_passlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            calls = []

            def run(cmd, capture_output, text, creationflags=0):
                calls.append(cmd)
                if cmd[-1] != "NUL":
                    with open(cmd[-1], "wb") as f:
                        f.write(b"data")
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with patch.object(a.subprocess, "run", side_effect=run):
                result = a.trim_clip(src, 0, 5, out, target_size_mb=5)
            self.assertTrue(result)
            self.assertEqual(len(calls), 2)
            self.assertIn("-an", calls[0])  # pass 1 is video-only
            self.assertEqual(calls[1][-1], out)  # pass 2 writes the real output
            passlog_prefix = calls[0][calls[0].index("-passlogfile") + 1]
            self.assertFalse(os.path.exists(passlog_prefix + "-0.log"))

    def test_target_size_pass1_failure_stops_before_pass2(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            calls = []

            def run(cmd, capture_output, text, creationflags=0):
                calls.append(cmd)
                return type("Result", (), {"returncode": 1, "stderr": "boom"})()

            with patch.object(a.subprocess, "run", side_effect=run):
                with self.assertLogs(level="ERROR"):
                    result = a.trim_clip(src, 0, 5, out, target_size_mb=5)
            self.assertFalse(result)
            self.assertEqual(len(calls), 1)  # never reached pass 2

    def test_audio_shift_probes_track_count_and_passes_it_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a, "probe_audio_stream_count", return_value=3) as mock_probe:
                with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                    result = a.trim_clip(
                        src, 0, 5, out, audio_shift_ms_by_track={2: 27},
                    )
            self.assertTrue(result)
            mock_probe.assert_called_once()
            self.assertIn("-filter_complex", self.last_cmd)

    def test_audio_shift_not_requested_never_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a, "probe_audio_stream_count") as mock_probe:
                with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                    a.trim_clip(src, 0, 5, out)
            mock_probe.assert_not_called()

    def test_audio_shift_falls_back_gracefully_when_probe_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a, "probe_audio_stream_count", return_value=None):
                with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                    with self.assertLogs(level="WARNING"):
                        result = a.trim_clip(src, 0, 5, out, audio_shift_ms_by_track={2: 27})
            self.assertTrue(result)  # still trims, just without the shift
            self.assertNotIn("-filter_complex", self.last_cmd)

    def test_audio_shift_ignored_together_with_target_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")

            def run(cmd, capture_output, text, creationflags=0):
                if cmd[-1] != "NUL":
                    with open(cmd[-1], "wb") as f:
                        f.write(b"data")
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with patch.object(a, "probe_audio_stream_count") as mock_probe:
                with patch.object(a.subprocess, "run", side_effect=run):
                    with self.assertLogs(level="WARNING"):
                        result = a.trim_clip(
                            src, 0, 5, out, target_size_mb=5, audio_shift_ms_by_track={2: 27},
                        )
            self.assertTrue(result)
            mock_probe.assert_not_called()  # two-pass path never touches the shift at all

    def test_mute_tracks_probes_track_count_and_passes_it_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a, "probe_audio_stream_count", return_value=3) as mock_probe:
                with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                    result = a.trim_clip(src, 0, 5, out, muted_destinations=[2])
            self.assertTrue(result)
            mock_probe.assert_called_once()
            self.assertIn("-filter_complex", self.last_cmd)

    def test_mute_tracks_not_requested_never_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a, "probe_audio_stream_count") as mock_probe:
                with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                    a.trim_clip(src, 0, 5, out, muted_destinations=[])
            mock_probe.assert_not_called()

    def test_mute_tracks_ignored_together_with_target_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")

            def run(cmd, capture_output, text, creationflags=0):
                if cmd[-1] != "NUL":
                    with open(cmd[-1], "wb") as f:
                        f.write(b"data")
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with patch.object(a, "probe_audio_stream_count") as mock_probe:
                with patch.object(a.subprocess, "run", side_effect=run):
                    with self.assertLogs(level="WARNING"):
                        result = a.trim_clip(src, 0, 5, out, target_size_mb=5, muted_destinations=[2])
            self.assertTrue(result)
            mock_probe.assert_not_called()

    def test_audio_routing_probes_track_count_and_consolidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a, "probe_audio_stream_count", return_value=3) as mock_probe:
                with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                    result = a.trim_clip(src, 0, 5, out, audio_routing={1: [2, 3]})
            self.assertTrue(result)
            mock_probe.assert_called_once()
            self.assertIn("amix=inputs=2", self.last_cmd[self.last_cmd.index("-filter_complex") + 1])

    def test_audio_routing_not_requested_never_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")
            with patch.object(a, "probe_audio_stream_count") as mock_probe:
                with patch.object(a.subprocess, "run", side_effect=self._fake_run()):
                    a.trim_clip(src, 0, 5, out, audio_routing=None)
            mock_probe.assert_not_called()


class IsFileBeingRecordedTests(unittest.TestCase):
    def test_true_for_the_exact_active_recording(self):
        recording_state = {"current_path": r"C:\Recordings\Game - clip.mkv"}
        self.assertTrue(a.is_file_being_recorded(r"C:\Recordings\Game - clip.mkv", recording_state))

    def test_true_regardless_of_path_normalization(self):
        # Built with os.path.join/a literal "." component rather than a hardcoded backslash
        # literal -- os.path.normpath only collapses "." segments it recognizes as such via the
        # ambient separator (ntpath on Windows, posixpath elsewhere), so a Windows-style literal
        # wouldn't actually exercise normalization at all on a POSIX os.path.
        recording_state = {"current_path": os.path.join("Recordings", "Game - clip.mkv")}
        unnormalized = os.path.join("Recordings", ".", "Game - clip.mkv")
        self.assertTrue(a.is_file_being_recorded(unnormalized, recording_state))

    def test_false_for_a_different_file(self):
        recording_state = {"current_path": r"C:\Recordings\Game - clip.mkv"}
        self.assertFalse(a.is_file_being_recorded(r"C:\Recordings\Other - clip.mkv", recording_state))

    def test_false_when_nothing_is_recording(self):
        self.assertFalse(a.is_file_being_recorded(r"C:\Recordings\Game - clip.mkv", {}))
        self.assertFalse(a.is_file_being_recorded(r"C:\Recordings\Game - clip.mkv", {"current_path": None}))


class ValidateTrimRangeTests(unittest.TestCase):
    def test_valid_range_returns_none(self):
        self.assertIsNone(a.validate_trim_range(5, 10, 20))

    def test_negative_start_is_rejected(self):
        self.assertIsNotNone(a.validate_trim_range(-1, 10, 20))

    def test_end_before_start_is_rejected(self):
        self.assertIsNotNone(a.validate_trim_range(10, 5, 20))

    def test_end_equal_to_start_is_rejected(self):
        self.assertIsNotNone(a.validate_trim_range(5, 5, 20))

    def test_start_past_duration_is_rejected(self):
        self.assertIsNotNone(a.validate_trim_range(25, 30, 20))

    def test_end_past_duration_is_rejected(self):
        self.assertIsNotNone(a.validate_trim_range(5, 30, 20))

    def test_small_overshoot_past_duration_is_tolerated(self):
        # A little slack for UI/float rounding -- VLC's own reported duration and the user's
        # typed/captured end time won't always agree to the millisecond.
        self.assertIsNone(a.validate_trim_range(5, 20.3, 20))

    def test_unknown_duration_skips_bounds_check(self):
        # duration_seconds is None right after a file loads, before VLC reports its length --
        # the range/ordering checks should still apply, but not the duration bound.
        self.assertIsNone(a.validate_trim_range(5, 10000, None))
        self.assertIsNotNone(a.validate_trim_range(10, 5, None))


class ListRecentRecordingsTests(unittest.TestCase):
    def test_empty_or_missing_folder_returns_empty_list(self):
        self.assertEqual(a.list_recent_recordings(""), [])
        self.assertEqual(a.list_recent_recordings(None), [])
        self.assertEqual(a.list_recent_recordings(r"C:\does\not\exist"), [])

    def test_lists_video_files_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = os.path.join(tmp, "old.mp4")
            new_path = os.path.join(tmp, "new.mkv")
            open(old_path, "w").close()
            os.utime(old_path, (1000, 1000))
            open(new_path, "w").close()
            os.utime(new_path, (2000, 2000))
            self.assertEqual(a.list_recent_recordings(tmp), [new_path, old_path])

    def test_ignores_non_video_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "notes.txt"), "w").close()
            video = os.path.join(tmp, "clip.mp4")
            open(video, "w").close()
            self.assertEqual(a.list_recent_recordings(tmp), [video])

    def test_recurses_into_subfolders(self):
        # organize_into_game_subfolders nests recordings one level deeper, per game.
        with tempfile.TemporaryDirectory() as tmp:
            subfolder = os.path.join(tmp, "SomeGame")
            os.makedirs(subfolder)
            nested = os.path.join(subfolder, "clip.mp4")
            open(nested, "w").close()
            self.assertEqual(a.list_recent_recordings(tmp), [nested])

    def test_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(5):
                path = os.path.join(tmp, f"clip{i}.mp4")
                open(path, "w").close()
                os.utime(path, (1000 + i, 1000 + i))
            self.assertEqual(len(a.list_recent_recordings(tmp, limit=3)), 3)


class RunFfmpegWithProgressTests(unittest.TestCase):
    class _FakeProc:
        def __init__(self, out_lines, stderr_lines=None, returncode=0):
            self.stdout = iter(out_lines)
            self.stderr = iter(stderr_lines or [])
            self.returncode = returncode

        def wait(self):
            pass

    def test_parses_out_time_into_fraction(self):
        lines = ["out_time=00:00:05.000000\n", "out_time=00:00:10.000000\n", "progress=end\n"]
        proc = self._FakeProc(lines)
        fractions = []
        with patch.object(a.subprocess, "Popen", return_value=proc):
            returncode, stderr_text = a._run_ffmpeg_with_progress(["ffmpeg", "-y"], 10.0, fractions.append)
        self.assertEqual(returncode, 0)
        self.assertEqual(stderr_text, "")
        self.assertAlmostEqual(fractions[0], 0.5)
        self.assertAlmostEqual(fractions[1], 1.0)

    def test_zero_duration_never_calls_progress(self):
        proc = self._FakeProc(["out_time=00:00:05.000000\n"])
        calls = []
        with patch.object(a.subprocess, "Popen", return_value=proc):
            a._run_ffmpeg_with_progress(["ffmpeg"], 0, calls.append)
        self.assertEqual(calls, [])

    def test_captures_stderr(self):
        proc = self._FakeProc(["out_time=00:00:01.000000\n"], stderr_lines=["warning line\n"])
        with patch.object(a.subprocess, "Popen", return_value=proc):
            returncode, stderr_text = a._run_ffmpeg_with_progress(["ffmpeg"], 1.0, lambda f: None)
        self.assertIn("warning line", stderr_text)

    def test_inserts_progress_flags_after_executable(self):
        proc = self._FakeProc([])
        with patch.object(a.subprocess, "Popen", return_value=proc) as mock_popen:
            a._run_ffmpeg_with_progress(["ffmpeg", "-y", "-i", "in.mp4"], 1.0, lambda f: None)
        called_cmd = mock_popen.call_args[0][0]
        self.assertEqual(called_cmd[0], "ffmpeg")
        self.assertEqual(called_cmd[1:3], ["-progress", "pipe:1"])


class TrimClipProgressCallbackTests(unittest.TestCase):
    def test_progress_callback_used_for_plain_trim(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")

            def fake_progress_runner(cmd, total_duration, on_progress):
                self.assertAlmostEqual(total_duration, 5.0)
                with open(cmd[-1], "wb") as f:
                    f.write(b"data")
                on_progress(0.5)
                return 0, ""

            calls = []
            with patch.object(a, "_run_ffmpeg_with_progress", side_effect=fake_progress_runner):
                result = a.trim_clip(
                    src, 0, 5, out, progress_callback=lambda phase, frac: calls.append((phase, frac)),
                )
            self.assertTrue(result)
            self.assertIn(("Encoding clip", 0.0), calls)
            self.assertIn(("Encoding clip", 0.5), calls)

    def test_progress_callback_used_for_size_targeted_pass2(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")

            def fake_run(cmd, capture_output, text, creationflags=0):
                # pass 1 (analysis, discarded to NUL) still goes through plain subprocess.run
                self.assertEqual(cmd[-1], "NUL")
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            def fake_progress_runner(cmd, total_duration, on_progress):
                with open(cmd[-1], "wb") as f:
                    f.write(b"data")
                return 0, ""

            calls = []
            with patch.object(a.subprocess, "run", side_effect=fake_run):
                with patch.object(a, "_run_ffmpeg_with_progress", side_effect=fake_progress_runner):
                    result = a.trim_clip(
                        src, 0, 5, out, target_size_mb=5,
                        progress_callback=lambda phase, frac: calls.append((phase, frac)),
                    )
            self.assertTrue(result)
            self.assertTrue(any(phase == "Analyzing (pass 1 of 2)" for phase, _ in calls))
            self.assertTrue(any(phase == "Encoding (pass 2 of 2)" for phase, _ in calls))

    def test_no_progress_callback_still_uses_plain_subprocess_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.mp4")
            open(src, "w").close()
            out = os.path.join(tmp, "out.mp4")

            def fake_run(cmd, capture_output, text, creationflags=0):
                with open(cmd[-1], "wb") as f:
                    f.write(b"data")
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            with patch.object(a.subprocess, "run", side_effect=fake_run) as mock_run:
                with patch.object(a, "_run_ffmpeg_with_progress") as mock_progress_run:
                    result = a.trim_clip(src, 0, 5, out)
            self.assertTrue(result)
            mock_run.assert_called_once()
            mock_progress_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
