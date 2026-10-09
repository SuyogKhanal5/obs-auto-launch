import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a
import platform_macos as pmac
import platform_windows as pw
import platform_linux as pl
from tests.fakes import FakeObsClient


class ObsLaunchCommandTests(unittest.TestCase):
    # Confirmed live: OBS started as this app's child gets its screen capture attributed to this
    # app ("Python" in the menu-bar sharing indicator; its Stop Sharing crashed this app).
    def test_macos_launches_the_bundle_through_launchservices_in_the_background(self):
        cmd = pmac.obs_launch_command("/Applications/OBS.app/Contents/MacOS/OBS", ["--minimize-to-tray"])
        self.assertEqual(cmd, ["open", "-g", "-a", "/Applications/OBS.app", "--args", "--minimize-to-tray"])

    def test_macos_falls_back_to_a_direct_launch_outside_a_bundle(self):
        self.assertEqual(pmac.obs_launch_command("/opt/obs/bin/obs", ["-x"]), ["/opt/obs/bin/obs", "-x"])

    def test_other_oses_launch_directly(self):
        self.assertEqual(pw.obs_launch_command(r"C:\obs\obs64.exe", ["-m"]), [r"C:\obs\obs64.exe", "-m"])
        self.assertEqual(pl.obs_launch_command("/usr/bin/obs", []), ["/usr/bin/obs"])

    def test_dispatcher_copies_args(self):
        args = ["--minimize-to-tray"]
        cmd = a.platform_common.obs_launch_command("/usr/bin/obs", args, platform_name="linux")
        cmd.append("x")
        self.assertEqual(args, ["--minimize-to-tray"])


class QuitObsGracefullyTests(unittest.TestCase):
    def test_macos_asks_obs_to_quit_by_bundle_id_never_kills(self):
        with unittest.mock.patch.object(pmac.subprocess, "run") as mock_run:
            self.assertTrue(pmac.quit_obs_gracefully())
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "osascript")
        self.assertIn('application id "com.obsproject.obs-studio" to quit', cmd[-1])

    def test_other_oses_are_no_ops(self):
        self.assertFalse(pw.quit_obs_gracefully())
        self.assertFalse(pl.quit_obs_gracefully())


class CloseObsBetweenSessionsTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.object(a.platform_common, "quit_obs_gracefully", return_value=True)
        self.mock_quit = patcher.start()
        self.addCleanup(patcher.stop)

    def test_closes_idle_obs(self):
        client = FakeObsClient()
        self.assertTrue(a.close_obs_between_sessions(client, overlay_enabled=False))
        self.mock_quit.assert_called_once()
        self.assertIn(("disconnect",), client.calls)

    def test_kept_open_while_the_mixer_overlay_needs_it(self):
        self.assertFalse(a.close_obs_between_sessions(FakeObsClient(), overlay_enabled=True))
        self.mock_quit.assert_not_called()

    def test_never_closed_while_streaming_recording_or_buffering(self):
        for attr in ("streaming_active", "recording_active", "replay_buffer_active"):
            client = FakeObsClient()
            setattr(client, attr, True)
            self.assertFalse(a.close_obs_between_sessions(client, overlay_enabled=False), attr)
        self.mock_quit.assert_not_called()

    def test_missing_replay_buffer_counts_as_idle(self):
        client = FakeObsClient()
        client.replay_buffer_available = False
        self.assertTrue(a.close_obs_between_sessions(client, overlay_enabled=False))

    def test_unreachable_status_counts_as_busy(self):
        class Broken(FakeObsClient):
            def get_record_status(self):
                raise RuntimeError("boom")
        self.assertFalse(a.close_obs_between_sessions(Broken(), overlay_enabled=False))
        self.mock_quit.assert_not_called()

    def test_clears_the_crash_marker_once_obs_has_exited(self):
        with unittest.mock.patch.object(a, "get_running_processes", return_value=[]), \
                unittest.mock.patch.object(a, "clear_obs_crash_sentinel") as mock_clear:
            self.assertTrue(a.close_obs_between_sessions(FakeObsClient(), False, "OBS"))
        mock_clear.assert_called_once()

    def test_leaves_the_marker_if_obs_never_exits(self):
        with unittest.mock.patch.object(a, "get_running_processes", return_value=[("OBS", "", 1)]), \
                unittest.mock.patch.object(a, "OBS_QUIT_WAIT_SECONDS", 0), \
                unittest.mock.patch.object(a, "clear_obs_crash_sentinel") as mock_clear:
            a.close_obs_between_sessions(FakeObsClient(), False, "OBS")
        mock_clear.assert_not_called()

    def test_client_kept_when_the_os_does_not_close_obs(self):
        self.mock_quit.return_value = False
        client = FakeObsClient()
        self.assertFalse(a.close_obs_between_sessions(client, overlay_enabled=False))
        self.assertNotIn(("disconnect",), client.calls)


if __name__ == "__main__":
    unittest.main()


class ConnectObsForTaskTests(unittest.TestCase):
    # Confirmed live: with OBS closed between games, "Calibrate Audio Sync" just failed to connect.
    def test_uses_a_running_obs_as_is(self):
        client = FakeObsClient()
        with unittest.mock.patch.object(a, "connect_obs", return_value=client), \
                unittest.mock.patch.object(a, "launch_obs") as mock_launch:
            self.assertEqual(a.connect_obs_for_task({}, {}), (client, False))
        mock_launch.assert_not_called()

    def test_starts_obs_when_it_is_not_running(self):
        client = FakeObsClient()
        with unittest.mock.patch.object(a, "connect_obs", side_effect=[None, client]), \
                unittest.mock.patch.object(a, "get_running_processes", return_value=[]), \
                unittest.mock.patch.object(a, "obs_process_name", return_value="OBS"), \
                unittest.mock.patch.object(a, "launch_obs", return_value=True) as mock_launch:
            self.assertEqual(a.connect_obs_for_task({}, {}), (client, True))
        mock_launch.assert_called_once()

    def test_never_launches_a_second_obs(self):
        with unittest.mock.patch.object(a, "connect_obs", return_value=None), \
                unittest.mock.patch.object(a, "get_running_processes", return_value=[("OBS", "", 1)]), \
                unittest.mock.patch.object(a, "obs_process_name", return_value="OBS"), \
                unittest.mock.patch.object(a, "launch_obs") as mock_launch:
            self.assertEqual(a.connect_obs_for_task({}, {}), (None, False))
        mock_launch.assert_not_called()

    def test_closes_obs_again_only_if_this_task_started_it(self):
        with unittest.mock.patch.object(a, "close_obs_between_sessions", return_value=True) as mock_close, \
                unittest.mock.patch.object(a, "obs_process_name", return_value="OBS"):
            a.release_obs_after_task(FakeObsClient(), {}, started_here=False)
            mock_close.assert_not_called()
            a.release_obs_after_task(FakeObsClient(), {}, started_here=True)
            mock_close.assert_called_once()

    def test_keeps_obs_open_when_set_to(self):
        client = FakeObsClient()
        with unittest.mock.patch.object(a, "close_obs_between_sessions") as mock_close:
            a.release_obs_after_task(client, {"keep_running_between_sessions": True}, started_here=True)
        mock_close.assert_not_called()
        self.assertIn(("disconnect",), client.calls)
