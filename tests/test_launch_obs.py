import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a


class LaunchObsTests(unittest.TestCase):
    def test_launches_from_configured_path_when_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            obs_path = os.path.join(tmp, "obs64.exe")
            open(obs_path, "w").close()
            obs_config = {"path": obs_path, "startup_wait_seconds": 0}
            with unittest.mock.patch.object(a.subprocess, "Popen") as mock_popen, \
                 unittest.mock.patch.object(a, "clear_obs_crash_sentinel"), \
                 unittest.mock.patch.object(a.platform_common, "find_obs_executable") as mock_find:
                result = a.launch_obs(obs_config)
            self.assertTrue(result)
            mock_find.assert_not_called()
            mock_popen.assert_called_once()
            self.assertEqual(obs_config["path"], obs_path)

    def test_falls_back_to_discovery_when_configured_path_is_stale(self):
        # Mirrors a real config.json carried over from another OS -- e.g. a literal Windows path
        # opened as-is on macOS/Linux, which will never exist there.
        with tempfile.TemporaryDirectory() as tmp:
            real_obs_path = os.path.join(tmp, "OBS")
            open(real_obs_path, "w").close()
            obs_config = {
                "path": r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
                "startup_wait_seconds": 0,
            }
            with unittest.mock.patch.object(a.subprocess, "Popen") as mock_popen, \
                 unittest.mock.patch.object(a, "clear_obs_crash_sentinel"), \
                 unittest.mock.patch.object(
                     a.platform_common, "find_obs_executable", return_value=real_obs_path
                 ) as mock_find:
                result = a.launch_obs(obs_config)
            self.assertTrue(result)
            mock_find.assert_called_once_with()
            mock_popen.assert_called_once()
            self.assertEqual(obs_config["path"], real_obs_path)

    def test_returns_false_when_nothing_can_be_found_anywhere(self):
        obs_config = {"path": r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"}
        with unittest.mock.patch.object(a.subprocess, "Popen") as mock_popen, \
             unittest.mock.patch.object(
                 a.platform_common, "find_obs_executable", return_value=None
             ):
            result = a.launch_obs(obs_config)
        self.assertFalse(result)
        mock_popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
