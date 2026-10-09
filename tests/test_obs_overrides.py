import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
from tests.fakes import FakeObsClient


class ApplyOutputFolderTests(unittest.TestCase):
    def test_none_is_a_noop(self):
        client = FakeObsClient()
        client.set_record_directory(r"C:\Original")
        a.apply_output_folder(client, None)
        self.assertEqual(client.get_record_directory().record_directory, r"C:\Original")

    def test_sets_new_folder_and_creates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "Recordings")
            client = FakeObsClient()
            a.apply_output_folder(client, target)
            self.assertEqual(client.get_record_directory().record_directory, target)
            self.assertTrue(os.path.isdir(target))

    def test_already_matching_folder_is_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeObsClient()
            client.set_record_directory(tmp)
            client.calls.clear()
            a.apply_output_folder(client, tmp)
            self.assertNotIn(("set_record_directory", tmp), client.calls)


class ApplyRecordingFormatTests(unittest.TestCase):
    def test_none_is_a_noop(self):
        client = FakeObsClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "mp4")
        a.apply_recording_format(client, None)
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecFormat2"), "mp4")

    def test_sets_new_format(self):
        client = FakeObsClient()
        a.apply_recording_format(client, "mkv")
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecFormat2"), "mkv")

    def test_already_matching_format_is_not_reset(self):
        client = FakeObsClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        client.calls.clear()
        a.apply_recording_format(client, "hybrid_mp4")
        self.assertEqual(client.calls, [])

    def test_accepts_a_format_outside_the_curated_options_list(self):
        # The field is deliberately editable, not a locked dropdown -- must not reject/validate
        # against RECORDING_FORMAT_OPTIONS.
        client = FakeObsClient()
        a.apply_recording_format(client, "some_future_format_code")
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecFormat2"), "some_future_format_code")


class GetReplayBufferModeTests(unittest.TestCase):
    def test_explicit_mode_wins(self):
        self.assertEqual(a.get_replay_buffer_mode({"mode": "only", "enabled": False}), "only")

    def test_unknown_mode_falls_back_to_legacy_enabled(self):
        self.assertEqual(a.get_replay_buffer_mode({"mode": "bogus", "enabled": True}), "with_recording")

    def test_legacy_enabled_true_maps_to_with_recording(self):
        self.assertEqual(a.get_replay_buffer_mode({"enabled": True}), "with_recording")

    def test_legacy_enabled_false_or_missing_maps_to_off(self):
        self.assertEqual(a.get_replay_buffer_mode({"enabled": False}), "off")
        self.assertEqual(a.get_replay_buffer_mode({}), "off")


class AddRecordingMarkerTests(unittest.TestCase):
    # add_recording_marker's primary mechanism is its own recording_state["markers"] list (read
    # from OBS's own GetRecordStatus elapsed time, not tied to recording format at all) --
    # CreateRecordChapter is only ever a best-effort bonus now, never required for success. See
    # CROSS_PLATFORM_PLAN.md §5.21 for why that forcing was removed.
    def test_success_appends_elapsed_seconds_to_recording_state(self):
        client = FakeObsClient()
        client.record_duration_ms = 12500
        recording_state = {}
        result = a.add_recording_marker(client, recording_state=recording_state)
        self.assertTrue(result)
        self.assertEqual(recording_state["markers"], [12.5])

    def test_multiple_markers_accumulate_in_order(self):
        client = FakeObsClient()
        recording_state = {}
        client.record_duration_ms = 1000
        a.add_recording_marker(client, recording_state=recording_state)
        client.record_duration_ms = 5000
        a.add_recording_marker(client, recording_state=recording_state)
        self.assertEqual(recording_state["markers"], [1.0, 5.0])

    def test_none_recording_state_does_not_raise(self):
        client = FakeObsClient()
        result = a.add_recording_marker(client, recording_state=None)
        self.assertTrue(result)

    def test_also_adds_a_native_obs_chapter_on_hybrid_mp4(self):
        client = FakeObsClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        a.add_recording_marker(client, recording_state={})
        self.assertIn(("create_record_chapter", None), client.calls)

    def test_skips_the_native_chapter_on_other_formats(self):
        # OBS rejects it anywhere else, and obsws_python logs that rejection as an ERROR with a
        # traceback on every marker before it can be caught.
        client = FakeObsClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "mkv")
        recording_state = {}
        self.assertTrue(a.add_recording_marker(client, recording_state=recording_state))
        self.assertNotIn(("create_record_chapter", None), client.calls)
        self.assertEqual(recording_state["markers"], [0.0])

    def test_unsupported_format_is_non_fatal(self):
        # A non-Hybrid-MP4 recording is the common case now that markers don't need that format
        # at all -- CreateRecordChapter failing this way must not affect the overall result.
        class RejectingClient(FakeObsClient):
            def create_record_chapter(self, chapter_name=None):
                raise a.obsws.error.OBSSDKRequestError("CreateRecordChapter", a.OBS_CHAPTER_NOT_SUPPORTED_CODE, "")

        client = RejectingClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        recording_state = {}
        result = a.add_recording_marker(client, recording_state=recording_state)
        self.assertTrue(result)
        self.assertEqual(recording_state["markers"], [0.0])

    def test_other_create_record_chapter_error_is_also_non_fatal(self):
        class RejectingClient(FakeObsClient):
            def create_record_chapter(self, chapter_name=None):
                raise a.obsws.error.OBSSDKRequestError("CreateRecordChapter", 500, "")

        client = RejectingClient()
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        result = a.add_recording_marker(client, recording_state={})
        self.assertTrue(result)

    def test_cannot_read_record_status_returns_false_without_recording_a_marker(self):
        class BrokenClient(FakeObsClient):
            def get_record_status(self):
                raise RuntimeError("boom")

        client = BrokenClient()
        recording_state = {}
        result = a.add_recording_marker(client, recording_state=recording_state)
        self.assertFalse(result)
        self.assertNotIn("markers", recording_state)

    def test_success_flashes_the_tray_icon_when_icon_and_status_given(self):
        # Same "something just happened" flash as a manual split or a replay-buffer save --
        # there's no OBS event for a chapter being created, so this only fires from here.
        client = FakeObsClient()
        icon = unittest.mock.Mock()
        status = {"recording": True}
        before = time.time()
        result = a.add_recording_marker(client, icon=icon, status=status)
        self.assertTrue(result)
        self.assertGreater(status.get("flash_until", 0), before)
        self.assertIsInstance(icon.icon, a.Image.Image)  # build_tray_image() actually ran

    def test_success_does_not_flash_without_icon_or_status(self):
        client = FakeObsClient()
        result = a.add_recording_marker(client)  # icon=None, status=None -- must not raise
        self.assertTrue(result)


class ApplyReplayBufferSettingsTests(unittest.TestCase):
    def test_off_mode_disables_in_simple_output(self):
        client = FakeObsClient()
        client.set_profile_parameter("SimpleOutput", "RecRB", "true")
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "off"})
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRB"), "false")
        # Turning it off never needs a restart -- nothing has to restart just to stop calling
        # StartReplayBuffer.
        self.assertFalse(needs_restart)

    def test_fresh_unset_value_with_off_mode_does_not_need_restart(self):
        # A brand-new profile that has never touched this setting reads back None, not "false" --
        # None != "false" must not be treated as a real change requiring a pointless restart on
        # every fresh install's first game launch.
        client = FakeObsClient()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "off"})
        self.assertFalse(needs_restart)

    def test_with_recording_mode_enables_and_sets_length(self):
        client = FakeObsClient()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording", "max_seconds": 45})
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRB"), "true")
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRBTime"), "45")
        # Enabling it for the first time is exactly the case OBS won't pick up without a restart.
        self.assertTrue(needs_restart)

    def test_only_mode_also_enables_the_obs_setting(self):
        client = FakeObsClient()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "only", "max_seconds": 30})
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRB"), "true")
        self.assertTrue(needs_restart)

    def test_default_length_used_when_not_configured(self):
        client = FakeObsClient()
        a.apply_replay_buffer_settings(client, {"mode": "with_recording"})
        self.assertEqual(
            a.get_profile_parameter_value(client, "SimpleOutput", "RecRBTime"),
            str(a.DEFAULT_REPLAY_BUFFER_SECONDS),
        )

    def test_uses_advout_category_when_output_mode_is_advanced(self):
        client = FakeObsClient()
        client.set_profile_parameter("Output", "Mode", "Advanced")
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording", "max_seconds": 60})
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecRB"), "true")
        self.assertEqual(a.get_profile_parameter_value(client, "AdvOut", "RecRBTime"), "60")
        self.assertTrue(needs_restart)

    def test_already_matching_settings_are_not_rewritten(self):
        client = FakeObsClient()
        client.set_profile_parameter("SimpleOutput", "RecRB", "true")
        client.set_profile_parameter("SimpleOutput", "RecRBTime", "30")
        client.calls.clear()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording", "max_seconds": 30})
        self.assertEqual(client.calls, [])
        # Nothing changed, so no restart is needed either.
        self.assertFalse(needs_restart)

    def test_changing_only_the_length_still_needs_restart(self):
        # RecRB was already "true" -- only RecRBTime differs. Must still report needs_restart,
        # since OBS also needs a restart to pick up a changed buffer length.
        client = FakeObsClient()
        client.set_profile_parameter("SimpleOutput", "RecRB", "true")
        client.set_profile_parameter("SimpleOutput", "RecRBTime", "30")
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording", "max_seconds": 60})
        self.assertEqual(a.get_profile_parameter_value(client, "SimpleOutput", "RecRBTime"), "60")
        self.assertTrue(needs_restart)

    def test_never_raises_when_client_errors(self):
        class RaisingClient(FakeObsClient):
            def set_profile_parameter(self, category, name, value):
                raise RuntimeError("boom")

        client = RaisingClient()
        needs_restart = a.apply_replay_buffer_settings(client, {"mode": "with_recording"})  # must not raise
        self.assertFalse(needs_restart)


class IsEventClientConnectedTests(unittest.TestCase):
    class FakeSocket:
        def __init__(self, connected):
            self.connected = connected

    class FakeBaseClient:
        def __init__(self, connected):
            self.ws = IsEventClientConnectedTests.FakeSocket(connected)

    class FakeEventClient:
        def __init__(self, connected):
            self.base_client = IsEventClientConnectedTests.FakeBaseClient(connected)

    def test_true_when_socket_reports_connected(self):
        self.assertTrue(a.is_event_client_connected(self.FakeEventClient(True)))

    def test_false_when_socket_reports_disconnected(self):
        self.assertFalse(a.is_event_client_connected(self.FakeEventClient(False)))

    def test_assumes_connected_when_attribute_is_unavailable(self):
        # A version/API mismatch shouldn't itself force unnecessary reconnect churn.
        self.assertTrue(a.is_event_client_connected(object()))


class SetGameAudioCaptureTargetTests(unittest.TestCase):
    # set_game_audio_capture_target's own orchestration (repoint-vs-create, error handling) is
    # OS-agnostic -- mocking platform_common's two dispatch functions with Windows-shaped return
    # values makes these tests exercise that same orchestration regardless of which real OS runs
    # them, per platform_common.py's own testing philosophy (CROSS_PLATFORM_PLAN.md §3.2). The
    # platform backends themselves (Windows/macOS/Linux) have their own dedicated tests.
    def setUp(self):
        patcher1 = unittest.mock.patch.object(
            a.platform_common, "process_audio_capture_kind", return_value="wasapi_process_output_capture",
        )
        patcher2 = unittest.mock.patch.object(
            a.platform_common, "process_audio_capture_settings",
            side_effect=lambda process_name, exe_path: {
                "window": f"::{process_name}", "priority": a.WINDOW_MATCH_PRIORITY_EXE_FALLBACK,
            },
        )
        patcher1.start()
        patcher2.start()
        self.addCleanup(patcher1.stop)
        self.addCleanup(patcher2.stop)

    def test_repoints_an_existing_input(self):
        client = FakeObsClient(inputs={
            "Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {}, "settings": {}},
        })
        a.set_game_audio_capture_target(client, "Game Audio", "Balatro.exe")
        self.assertEqual(
            client.inputs["Game Audio"]["settings"],
            {"window": "::Balatro.exe", "priority": a.WINDOW_MATCH_PRIORITY_EXE_FALLBACK},
        )
        self.assertNotIn("Game Audio", [c[2] for c in client.calls if c[0] == "create_input"])

    def test_creates_the_input_if_it_does_not_exist_yet(self):
        client = FakeObsClient()  # no "Game Audio" input at all
        a.set_game_audio_capture_target(client, "Game Audio", "Balatro.exe")
        self.assertIn("Game Audio", client.inputs)
        self.assertEqual(client.inputs["Game Audio"]["kind"], "wasapi_process_output_capture")
        self.assertEqual(
            client.inputs["Game Audio"]["settings"],
            {"window": "::Balatro.exe", "priority": a.WINDOW_MATCH_PRIORITY_EXE_FALLBACK},
        )

    def test_created_input_is_added_to_the_current_scene(self):
        client = FakeObsClient(current_scene="My Scene")
        a.set_game_audio_capture_target(client, "Game Audio", "Balatro.exe")
        create_calls = [c for c in client.calls if c[0] == "create_input"]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0][1], "My Scene")

    def test_does_not_create_on_an_unrelated_error(self):
        # Only a "resource not found" error should trigger the create-fallback -- anything else
        # (e.g. a connection hiccup) should just be logged, not misread as "input is missing".
        client = FakeObsClient()

        def raise_unrelated(name, settings, overlay):
            raise a.obsws.error.OBSSDKRequestError("SetInputSettings", 500, "internal error")

        client.set_input_settings = raise_unrelated
        with self.assertLogs(level="WARNING"):
            a.set_game_audio_capture_target(client, "Game Audio", "Balatro.exe")  # must not raise
        self.assertNotIn("Game Audio", client.inputs)


class ResolveProcessAudioCaptureTests(unittest.TestCase):
    def test_returns_none_kind_when_os_has_no_capture_kind(self):
        with unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value=None):
            kind, settings = a.resolve_process_audio_capture("Balatro.exe")
        self.assertIsNone(kind)
        self.assertIsNone(settings)

    def test_returns_none_settings_when_process_cannot_be_resolved(self):
        with unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value="sck_audio_capture"), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_settings", return_value=None):
            kind, settings = a.resolve_process_audio_capture("Balatro")
        self.assertEqual(kind, "sck_audio_capture")
        self.assertIsNone(settings)

    def test_looks_up_the_live_exe_path_for_a_running_process(self):
        # Only macOS's real backend actually uses exe_path (to resolve a bundle identifier), but
        # this orchestration function should always try to find it regardless of OS, rather than
        # assuming it's never needed.
        fake_processes = [("Balatro", "/Applications/Balatro.app/Contents/MacOS/Balatro", 123)]
        captured = {}

        def fake_settings(process_name, exe_path):
            captured["process_name"] = process_name
            captured["exe_path"] = exe_path
            return {"type": 1, "application": "com.playstack.balatro"}

        with unittest.mock.patch.object(a, "get_running_processes", return_value=fake_processes), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value="sck_audio_capture"), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_settings", side_effect=fake_settings):
            kind, settings = a.resolve_process_audio_capture("Balatro")
        self.assertEqual(kind, "sck_audio_capture")
        self.assertEqual(settings, {"type": 1, "application": "com.playstack.balatro"})
        self.assertEqual(captured["exe_path"], "/Applications/Balatro.app/Contents/MacOS/Balatro")

    def test_exe_path_is_none_when_process_is_not_running(self):
        captured = {}

        def fake_settings(process_name, exe_path):
            captured["exe_path"] = exe_path
            return None

        with unittest.mock.patch.object(a, "get_running_processes", return_value=[]), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value="sck_audio_capture"), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_settings", side_effect=fake_settings):
            a.resolve_process_audio_capture("Balatro")
        self.assertIsNone(captured["exe_path"])

    def test_logs_and_leaves_input_unchanged_when_os_has_no_capture_kind(self):
        client = FakeObsClient()
        with unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value=None):
            with self.assertLogs(level="WARNING"):
                a.set_game_audio_capture_target(client, "Game Audio", "Balatro")  # must not raise
        self.assertEqual(client.calls, [])

    def test_logs_and_leaves_input_unchanged_when_process_cannot_be_resolved(self):
        client = FakeObsClient()
        with unittest.mock.patch.object(a.platform_common, "process_audio_capture_kind", return_value="sck_audio_capture"), \
             unittest.mock.patch.object(a.platform_common, "process_audio_capture_settings", return_value=None):
            with self.assertLogs(level="WARNING"):
                a.set_game_audio_capture_target(client, "Game Audio", "Balatro")  # must not raise
        self.assertEqual(client.calls, [])


class ActiveSessionMarkerTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.mkdtemp()
        self.marker_path = os.path.join(tmp_dir, ".active_recording_session.json")
        self.patcher = unittest.mock.patch.object(a, "ACTIVE_SESSION_MARKER_PATH", self.marker_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_read_returns_none_when_no_marker_written(self):
        self.assertIsNone(a.read_active_session_marker())

    def test_write_then_read_round_trips_the_display_name(self):
        a.write_active_session_marker("Street Fighter 6")
        self.assertEqual(a.read_active_session_marker(), {"display_name": "Street Fighter 6"})

    def test_clear_removes_the_marker(self):
        a.write_active_session_marker("Street Fighter 6")
        a.clear_active_session_marker()
        self.assertIsNone(a.read_active_session_marker())

    def test_clear_does_not_raise_when_no_marker_exists(self):
        a.clear_active_session_marker()  # must not raise


class RecordingMarkersSidecarTests(unittest.TestCase):
    # write_recording_markers/read_recording_markers back the clip editor's own marker display
    # (see refresh_markers) independently of OBS's recording format -- see
    # CROSS_PLATFORM_PLAN.md §5.21.
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.recording_path = os.path.join(self._tmp_ctx.name, "Balatro - 2026-10-06 09-53-29.mov")
        self.addCleanup(self._tmp_ctx.cleanup)

    def test_sidecar_path_appends_suffix(self):
        self.assertEqual(a.marker_sidecar_path(self.recording_path), self.recording_path + ".markers.json")

    def test_read_returns_none_when_no_sidecar_written(self):
        self.assertIsNone(a.read_recording_markers(self.recording_path))

    def test_write_then_read_round_trips_sorted_markers(self):
        a.write_recording_markers(self.recording_path, [47.0, 12.5, 103.2])
        self.assertEqual(a.read_recording_markers(self.recording_path), [12.5, 47.0, 103.2])

    def test_empty_markers_list_writes_no_sidecar_at_all(self):
        a.write_recording_markers(self.recording_path, [])
        self.assertFalse(os.path.isfile(a.marker_sidecar_path(self.recording_path)))
        self.assertIsNone(a.read_recording_markers(self.recording_path))

    def test_none_markers_does_not_raise(self):
        a.write_recording_markers(self.recording_path, None)  # must not raise
        self.assertIsNone(a.read_recording_markers(self.recording_path))

    def test_none_recording_path_does_not_raise(self):
        a.write_recording_markers(None, [1.0])  # must not raise

    def test_corrupt_sidecar_returns_none_instead_of_raising(self):
        with open(a.marker_sidecar_path(self.recording_path), "w", encoding="utf-8") as f:
            f.write("not valid json")
        self.assertIsNone(a.read_recording_markers(self.recording_path))

    def test_non_numeric_markers_returns_none_instead_of_raising(self):
        with open(a.marker_sidecar_path(self.recording_path), "w", encoding="utf-8") as f:
            json.dump({"markers": ["not", "numbers"]}, f)
        self.assertIsNone(a.read_recording_markers(self.recording_path))


class MarkersFolderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.recordings = os.path.join(self._tmp.name, "Movies")
        self.markers = os.path.join(self._tmp.name, "Markers")
        os.makedirs(self.recordings)
        self.recording = os.path.join(self.recordings, "Balatro - 2026-10-08 13-27-41.mkv")

    def test_configured_folder_holds_the_file_named_after_the_recording(self):
        self.assertEqual(
            a.marker_sidecar_path(self.recording, self.markers),
            os.path.join(self.markers, "Balatro - 2026-10-08 13-27-41.mkv.markers.json"),
        )

    def test_writes_into_the_configured_folder_creating_it(self):
        a.write_recording_markers(self.recording, [5.0], self.markers)
        self.assertTrue(os.path.isfile(a.marker_sidecar_path(self.recording, self.markers)))
        self.assertFalse(os.path.isfile(a.marker_sidecar_path(self.recording)))
        self.assertEqual(a.read_recording_markers(self.recording, self.markers), [5.0])

    def test_reads_markers_saved_next_to_the_recording_before_the_folder_was_set(self):
        a.write_recording_markers(self.recording, [3.0, 1.0])
        self.assertEqual(a.read_recording_markers(self.recording, self.markers), [1.0, 3.0])

    def test_configured_folder_wins_over_an_older_file_next_to_the_recording(self):
        a.write_recording_markers(self.recording, [1.0])
        a.write_recording_markers(self.recording, [9.0], self.markers)
        self.assertEqual(a.read_recording_markers(self.recording, self.markers), [9.0])

    def test_none_when_neither_exists(self):
        self.assertIsNone(a.read_recording_markers(self.recording, self.markers))


class ResolveRecordingResolutionTests(unittest.TestCase):
    def test_match_canvas_returns_base_resolution_unchanged(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "Match canvas (no scaling)"), (1920, 1080))

    def test_unrecognized_choice_falls_back_to_base_resolution(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "nonsense"), (1920, 1080))

    def test_1080p_preset_keeps_canvas_aspect_ratio(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "1080p"), (1920, 1080))

    def test_720p_preset_scales_width_from_canvas_aspect_ratio(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "720p"), (1280, 720))

    def test_ultrawide_canvas_still_rounds_width_to_even(self):
        # 3440x1440 at height 720 -> width 1720.0 exactly, but exercised with a canvas whose
        # ratio doesn't divide evenly to confirm the even-rounding actually does something.
        width, height = a.resolve_recording_resolution(3441, 1440, "720p")
        self.assertEqual(height, 720)
        self.assertEqual(width % 2, 0)


class ApplyRecordingResolutionTests(unittest.TestCase):
    def test_no_configured_choice_does_not_touch_video_settings(self):
        client = FakeObsClient()
        a.apply_recording_resolution(client, {})
        self.assertNotIn("set_video_settings", [c[0] for c in client.calls])

    def test_changes_output_resolution_when_different_from_target(self):
        client = FakeObsClient()  # base/output default to 1920x1080
        a.apply_recording_resolution(client, {"recording_resolution": "720p"})
        calls = [c for c in client.calls if c[0] == "set_video_settings"]
        self.assertEqual(len(calls), 1)
        video = client.get_video_settings()
        self.assertEqual((video.output_width, video.output_height), (1280, 720))
        # base resolution and fps must be left untouched
        self.assertEqual((video.base_width, video.base_height), (1920, 1080))

    def test_no_write_when_already_at_the_target_resolution(self):
        client = FakeObsClient()
        client.set_video_settings(60, 1, 1920, 1080, 1280, 720)
        client.calls.clear()
        a.apply_recording_resolution(client, {"recording_resolution": "720p"})
        self.assertNotIn("set_video_settings", [c[0] for c in client.calls])

    def test_match_canvas_actively_undoes_an_existing_downscale(self):
        # This is the whole point of the "Match canvas" option -- fixing exactly the "my
        # recording came out smaller than the canvas" problem it was added for, not just being
        # a synonym for "leave whatever OBS already has".
        client = FakeObsClient()
        client.set_video_settings(60, 1, 1920, 1080, 1280, 720)
        client.calls.clear()
        a.apply_recording_resolution(client, {"recording_resolution": "Match canvas (no scaling)"})
        video = client.get_video_settings()
        self.assertEqual((video.output_width, video.output_height), (1920, 1080))

    def test_client_error_is_logged_not_raised(self):
        client = FakeObsClient()

        def raise_error(*_args):
            raise RuntimeError("boom")

        client.set_video_settings = raise_error
        with self.assertLogs(level="ERROR"):
            a.apply_recording_resolution(client, {"recording_resolution": "720p"})  # must not raise


class ApplyProcessCaptureSyncOffsetTests(unittest.TestCase):
    def test_sets_offset_when_different(self):
        client = FakeObsClient(inputs={"Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {}}})
        a.apply_process_capture_sync_offset(client, "Game Audio", -55)
        self.assertIn(("set_input_audio_sync_offset", "Game Audio", -55), client.calls)
        self.assertEqual(client.get_input_audio_sync_offset("Game Audio").input_audio_sync_offset, -55)

    def test_no_write_when_already_correct(self):
        client = FakeObsClient(inputs={"Game Audio": {"kind": "wasapi_process_output_capture", "tracks": {}}})
        client.inputs["Game Audio"]["sync_offset"] = -55
        a.apply_process_capture_sync_offset(client, "Game Audio", -55)
        self.assertNotIn("set_input_audio_sync_offset", [c[0] for c in client.calls])

    def test_missing_input_is_logged_not_raised(self):
        client = FakeObsClient()
        with self.assertLogs(level="WARNING"):
            a.apply_process_capture_sync_offset(client, "Nonexistent", -55)  # must not raise


if __name__ == "__main__":
    unittest.main()


class NotifyTests(unittest.TestCase):
    def test_disabled_notifications_do_nothing(self):
        icon = unittest.mock.Mock()
        a.notify(icon, {"enabled": False}, "t", "m")
        icon.notify.assert_not_called()

    def test_shown_immediately_by_default(self):
        icon = unittest.mock.Mock()
        a.notify(icon, {"enabled": True}, "Recording started", "Balatro")
        icon.notify.assert_called_once_with("Balatro", "Recording started")

    def test_post_game_notification_is_delayed_on_macos(self):
        # Confirmed live: macOS hides banners while a fullscreen game's Space is closing.
        icon = unittest.mock.Mock()
        with unittest.mock.patch.object(a.sys, "platform", "darwin"), \
                unittest.mock.patch.object(a.threading, "Timer") as mock_timer:
            a.notify(icon, {"enabled": True}, "Recording stopped", "Balatro", after_game_exit=True)
        icon.notify.assert_not_called()
        self.assertEqual(mock_timer.call_args[0][0], a.MACOS_POST_GAME_NOTIFICATION_DELAY_SECONDS)
        mock_timer.call_args[0][1]()
        icon.notify.assert_called_once_with("Balatro", "Recording stopped")

    def test_post_game_notification_is_immediate_elsewhere(self):
        icon = unittest.mock.Mock()
        with unittest.mock.patch.object(a.sys, "platform", "win32"):
            a.notify(icon, {"enabled": True}, "Recording stopped", "Balatro", after_game_exit=True)
        icon.notify.assert_called_once()

    def test_failures_are_logged_as_warnings(self):
        icon = unittest.mock.Mock()
        icon.notify.side_effect = RuntimeError("boom")
        with self.assertLogs(level="WARNING"):
            a.notify(icon, {"enabled": True}, "t", "m")
