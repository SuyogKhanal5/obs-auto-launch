import os
import sys
import unittest
import unittest.mock
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
import platform_linux as pl
import platform_windows as pw
from tests.fakes import FakeObsClient


class ComputeScreenFitTests(unittest.TestCase):
    # Real numbers from a 14" MacBook Pro: 3024x1964 display, 76 notch rows (38pt at 2x).
    def test_match_screen_takes_the_cropped_screens_shape(self):
        canvas, t = a.compute_screen_fit(3024, 1964, 76, "match_screen")
        self.assertEqual(canvas, (1920, 1198))
        self.assertEqual(t["cropTop"], 76)
        self.assertEqual(t["boundsType"], "OBS_BOUNDS_SCALE_INNER")
        self.assertEqual((t["boundsWidth"], t["boundsHeight"]), (1920.0, 1198.0))

    def test_letterbox_keeps_16_9_and_fits_inside(self):
        canvas, t = a.compute_screen_fit(3024, 1964, 76, "letterbox")
        self.assertEqual(canvas, (1920, 1080))
        self.assertEqual(t["boundsType"], "OBS_BOUNDS_SCALE_INNER")
        self.assertEqual(t["boundsAlignment"], a.OBS_ALIGN_CENTER)

    def test_fill_keeps_16_9_and_covers(self):
        canvas, t = a.compute_screen_fit(3024, 1964, 76, "fill")
        self.assertEqual(canvas, (1920, 1080))
        self.assertEqual(t["boundsType"], "OBS_BOUNDS_SCALE_OUTER")

    def test_no_notch_no_crop(self):
        canvas, t = a.compute_screen_fit(2560, 1440, 0, "match_screen")
        self.assertEqual(canvas, (1920, 1080))
        self.assertEqual(t["cropTop"], 0)


class SceneClient(FakeObsClient):
    def __init__(self, items, transform, input_settings=None, base=(1920, 1080)):
        super().__init__()
        self.items = items
        self.transform = dict(transform)
        self.input_settings_by_name = input_settings or {}
        self.video = SimpleNamespace(
            fps_numerator=60, fps_denominator=1, base_width=base[0], base_height=base[1],
            output_width=base[0], output_height=base[1],
        )

    def get_current_program_scene(self):
        return SimpleNamespace(current_program_scene_name="Scene")

    def get_scene_item_list(self, name):
        return SimpleNamespace(scene_items=self.items)

    def get_input_settings(self, name):
        return SimpleNamespace(input_settings=self.input_settings_by_name.get(name, {}))

    def get_scene_item_transform(self, scene, item_id):
        return SimpleNamespace(scene_item_transform=dict(self.transform))

    def set_scene_item_transform(self, scene, item_id, transform):
        self.transform.update(transform)
        self.calls.append(("set_scene_item_transform", item_id))

    def get_video_settings(self):
        return self.video

    def set_video_settings(self, num, den, bw, bh, ow, oh):
        self.video = SimpleNamespace(
            fps_numerator=num, fps_denominator=den, base_width=bw, base_height=bh, output_width=ow, output_height=oh,
        )
        self.calls.append(("set_video_settings", bw, bh))


DISPLAY = {"sceneItemId": 19, "sourceName": "macOS Screen Capture", "inputKind": "screen_capture"}
AUDIO = {"sceneItemId": 20, "sourceName": "Discord", "inputKind": "sck_audio_capture"}
SOURCE = {"sourceWidth": 3024.0, "sourceHeight": 1964.0, "cropTop": 0}


class ApplyScreenFitTests(unittest.TestCase):
    def setUp(self):
        p = unittest.mock.patch.object(a.platform_common, "display_top_inset_pixels", return_value=76)
        p.start()
        self.addCleanup(p.stop)

    def test_default_matches_screen_and_crops_notch(self):
        client = SceneClient([DISPLAY, AUDIO], SOURCE)
        a.apply_screen_fit(client, {})
        self.assertIn(("set_video_settings", 1920, 1198), client.calls)
        self.assertEqual(client.transform["cropTop"], 76)
        self.assertEqual(client.transform["boundsHeight"], 1198.0)

    def test_second_run_writes_nothing(self):
        client = SceneClient([DISPLAY], SOURCE)
        a.apply_screen_fit(client, {})
        client.calls.clear()
        a.apply_screen_fit(client, {})
        self.assertEqual(client.calls, [])

    def test_off_leaves_obs_alone(self):
        client = SceneClient([DISPLAY], SOURCE)
        a.apply_screen_fit(client, {"screen_fit": "off"})
        self.assertEqual(client.calls, [])

    def test_window_capture_is_not_touched(self):
        client = SceneClient([DISPLAY], SOURCE, input_settings={"macOS Screen Capture": {"type": 1}})
        a.apply_screen_fit(client, {})
        self.assertEqual(client.calls, [])

    def test_two_display_captures_are_ambiguous_and_left_alone(self):
        other = dict(DISPLAY, sceneItemId=30, sourceName="Second Screen")
        client = SceneClient([DISPLAY, other], SOURCE)
        a.apply_screen_fit(client, {})
        self.assertEqual(client.calls, [])

    def test_capture_not_producing_frames_is_skipped_with_a_warning(self):
        client = SceneClient([DISPLAY], {"sourceWidth": 0.0, "sourceHeight": 0.0})
        with unittest.mock.patch.object(a, "SCREEN_FIT_SOURCE_WAIT_SECONDS", 0), self.assertLogs(level="WARNING"):
            a.apply_screen_fit(client, {})
        self.assertEqual(client.calls, [])

    def test_waits_for_a_just_started_capture_to_report_its_size(self):
        class Warming(SceneClient):
            polls = 0
            def get_scene_item_transform(self, scene, item_id):
                Warming.polls += 1
                if Warming.polls < 3:
                    return SimpleNamespace(scene_item_transform={"sourceWidth": 0.0, "sourceHeight": 0.0})
                return super().get_scene_item_transform(scene, item_id)
        client = Warming([DISPLAY], SOURCE)
        with unittest.mock.patch.object(a.time, "sleep"):
            a.apply_screen_fit(client, {})
        self.assertIn(("set_video_settings", 1920, 1198), client.calls)

    def test_errors_are_logged_not_raised(self):
        class Broken(SceneClient):
            def get_scene_item_list(self, name):
                raise RuntimeError("boom")
        with self.assertLogs(level="WARNING"):
            a.apply_screen_fit(Broken([DISPLAY], SOURCE), {})


class CustomRecordingResolutionTests(unittest.TestCase):
    def test_typed_size_is_used_as_is(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1198, "2560x1600"), (2560, 1600))

    def test_typed_size_is_rounded_down_to_a_multiple_of_4_like_obs(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1198, " 1283 X 803 "), (1280, 800))

    def test_presets_keep_the_canvas_shape(self):
        self.assertEqual(a.resolve_recording_resolution(1920, 1198, "480p"), (768, 480))

    def test_preset_matches_what_obs_actually_records(self):
        # Confirmed live: 1920x1080 canvas at 480p recorded 852x480, 1920x1198 recorded 768x480.
        self.assertEqual(a.resolve_recording_resolution(1920, 1080, "480p"), (852, 480))


class DisplayTopInsetOtherOsesTests(unittest.TestCase):
    def test_zero_off_macos(self):
        self.assertEqual(pw.display_top_inset_pixels(3024, 1964), 0)
        self.assertEqual(pl.display_top_inset_pixels(3024, 1964), 0)


if __name__ == "__main__":
    unittest.main()
