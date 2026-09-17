import os
import subprocess
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_common as pc


class HideConsoleSubprocessKwargsTests(unittest.TestCase):
    def test_windows_returns_creationflags(self):
        # subprocess.CREATE_NO_WINDOW only exists as an attribute on Windows -- referencing it
        # via getattr with its documented literal value (0x08000000) keeps this test runnable
        # (not just collectible) on Linux/macOS CI too, rather than raising AttributeError there.
        expected_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        with unittest.mock.patch.object(pc.sys, "platform", "win32"):
            self.assertEqual(pc.hide_console_subprocess_kwargs(), {"creationflags": expected_flag})

    def test_linux_returns_empty_dict(self):
        with unittest.mock.patch.object(pc.sys, "platform", "linux"):
            self.assertEqual(pc.hide_console_subprocess_kwargs(), {})

    def test_macos_returns_empty_dict(self):
        with unittest.mock.patch.object(pc.sys, "platform", "darwin"):
            self.assertEqual(pc.hide_console_subprocess_kwargs(), {})


class SelectBackendTests(unittest.TestCase):
    def test_win32_selects_platform_windows(self):
        backend = pc._select_backend(platform_name="win32")
        self.assertEqual(backend.__name__, "platform_windows")

    def test_darwin_selects_platform_macos(self):
        backend = pc._select_backend(platform_name="darwin")
        self.assertEqual(backend.__name__, "platform_macos")

    def test_linux_selects_platform_linux(self):
        backend = pc._select_backend(platform_name="linux")
        self.assertEqual(backend.__name__, "platform_linux")

    def test_defaults_to_real_sys_platform(self):
        backend = pc._select_backend()
        expected = "platform_windows" if sys.platform == "win32" else (
            "platform_macos" if sys.platform == "darwin" else "platform_linux"
        )
        self.assertEqual(backend.__name__, expected)


class FindObsExecutableTests(unittest.TestCase):
    def test_configured_path_that_exists_wins(self):
        with unittest.mock.patch("os.path.isfile", return_value=True):
            self.assertEqual(pc.find_obs_executable(configured_path="/some/obs"), "/some/obs")

    def test_configured_path_that_does_not_exist_falls_back_to_backend(self):
        with unittest.mock.patch("os.path.isfile", return_value=False):
            with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
                mock_select.return_value.find_obs_executable.return_value = "/backend/obs"
                result = pc.find_obs_executable(configured_path="/missing", platform_name="linux")
        mock_select.assert_called_once_with("linux")
        self.assertEqual(result, "/backend/obs")

    def test_no_configured_path_goes_straight_to_backend(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.find_obs_executable.return_value = "/backend/obs"
            result = pc.find_obs_executable(platform_name="darwin")
        self.assertEqual(result, "/backend/obs")


class FindFfmpegExecutableTests(unittest.TestCase):
    def test_absolute_configured_path_that_exists_wins(self):
        with unittest.mock.patch("os.path.isabs", return_value=True), \
             unittest.mock.patch("os.path.isfile", return_value=True):
            self.assertEqual(pc.find_ffmpeg_executable("/abs/ffmpeg"), "/abs/ffmpeg")

    def test_configured_path_found_via_which_wins(self):
        with unittest.mock.patch("os.path.isabs", return_value=False), \
             unittest.mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(pc.find_ffmpeg_executable("ffmpeg"), "/usr/bin/ffmpeg")

    def test_falls_back_to_backend_candidates_first_existing_wins(self):
        with unittest.mock.patch("shutil.which", return_value=None):
            with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
                mock_select.return_value.ffmpeg_candidates.return_value = ["/opt/a/ffmpeg", "/opt/b/ffmpeg"]
                with unittest.mock.patch("os.path.isfile", side_effect=lambda p: p == "/opt/b/ffmpeg"):
                    result = pc.find_ffmpeg_executable(platform_name="linux")
        self.assertEqual(result, "/opt/b/ffmpeg")

    def test_returns_none_when_nothing_resolves(self):
        with unittest.mock.patch("shutil.which", return_value=None):
            with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
                mock_select.return_value.ffmpeg_candidates.return_value = []
                self.assertIsNone(pc.find_ffmpeg_executable(platform_name="linux"))

    def test_blank_configured_path_defaults_to_bare_ffmpeg(self):
        with unittest.mock.patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/ffmpeg"
            pc.find_ffmpeg_executable(configured_path="")
        mock_which.assert_called_once_with("ffmpeg")


class FindFfprobeExecutableTests(unittest.TestCase):
    # find_ffprobe_executable uses the ambient os.path (ntpath on Windows, posixpath elsewhere)
    # to split/join the given ffmpeg_path -- correct for real production paths (always native to
    # whatever OS is actually running), but it genuinely means a backslash-separated Windows-style
    # path can't be parsed correctly by posixpath (backslash isn't a separator there at all) --
    # this one test is inherently Windows-real-path-semantics-dependent, not a portability bug in
    # the function itself, so it's skipped rather than faked on non-Windows CI.
    @unittest.skipUnless(os.name == "nt", "exercises real Windows (ntpath) path-splitting semantics")
    def test_windows_style_exe_suffix_preserved(self):
        with unittest.mock.patch("os.path.isfile", return_value=True):
            result = pc.find_ffprobe_executable(r"C:\ffmpeg\bin\ffmpeg.exe")
        self.assertEqual(result, r"C:\ffmpeg\bin\ffprobe.exe")

    def test_no_extension_preserved(self):
        # os.path.join uses this OS's own separator regardless of the input path's own slash
        # style (e.g. joins with a backslash even for a "/usr/bin/..."-style input when this
        # particular test happens to run on Windows) -- building the expected value the same way
        # keeps this assertion correct on every OS the suite actually runs on, not just POSIX.
        with unittest.mock.patch("os.path.isfile", return_value=True):
            result = pc.find_ffprobe_executable("/usr/bin/ffmpeg")
        self.assertEqual(result, os.path.join("/usr/bin", "ffprobe"))

    def test_returns_none_when_ffprobe_missing(self):
        with unittest.mock.patch("os.path.isfile", return_value=False):
            self.assertIsNone(pc.find_ffprobe_executable("/usr/bin/ffmpeg"))

    def test_unusual_ffmpeg_name_falls_back_to_os_default_on_linux(self):
        with unittest.mock.patch.object(pc.sys, "platform", "linux"):
            with unittest.mock.patch("os.path.isfile", return_value=True):
                result = pc.find_ffprobe_executable("/usr/bin/my-custom-build")
        self.assertEqual(result, os.path.join("/usr/bin", "ffprobe"))

    @unittest.skipUnless(os.name == "nt", "exercises real Windows (ntpath) path-splitting semantics")
    def test_unusual_ffmpeg_name_falls_back_to_os_default_on_windows(self):
        with unittest.mock.patch.object(pc.sys, "platform", "win32"):
            with unittest.mock.patch("os.path.isfile", return_value=True):
                result = pc.find_ffprobe_executable(r"C:\tools\my-custom-build.exe")
        self.assertEqual(result, r"C:\tools\ffprobe.exe")


class LibvlcFilenameTests(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(pc.libvlc_filename(platform_name="win32"), "libvlc.dll")

    def test_linux(self):
        self.assertEqual(pc.libvlc_filename(platform_name="linux"), "libvlc.so.5")

    def test_macos(self):
        self.assertEqual(pc.libvlc_filename(platform_name="darwin"), "libvlc.dylib")


class FindVlcDirectoryTests(unittest.TestCase):
    def test_configured_dir_with_correct_libvlc_file_wins(self):
        with unittest.mock.patch("os.path.isfile", return_value=True):
            result = pc.find_vlc_directory(configured_path="/some/vlc", platform_name="linux")
        self.assertEqual(result, "/some/vlc")

    def test_configured_dir_without_libvlc_falls_back_to_backend(self):
        with unittest.mock.patch("os.path.isfile", return_value=False):
            with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
                mock_select.return_value.LIBVLC_FILENAME = "libvlc.so.5"
                mock_select.return_value.find_vlc_directory.return_value = "/backend/vlc"
                result = pc.find_vlc_directory(configured_path="/some/vlc", platform_name="linux")
        self.assertEqual(result, "/backend/vlc")

    def test_no_configured_path_goes_to_backend(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.find_vlc_directory.return_value = "/backend/vlc"
            result = pc.find_vlc_directory(platform_name="darwin")
        self.assertEqual(result, "/backend/vlc")


class GetWindowTitlesTests(unittest.TestCase):
    def test_returns_backends_result(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.get_window_titles.return_value = {123: ["Balatro"]}
            result = pc.get_window_titles(platform_name="win32")
        mock_select.assert_called_once_with("win32")
        self.assertEqual(result, {123: ["Balatro"]})

    def test_backend_exception_is_logged_and_returns_empty_dict_not_raised(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.get_window_titles.side_effect = RuntimeError("boom")
            with self.assertLogs(level="ERROR"):
                result = pc.get_window_titles(platform_name="linux")
        self.assertEqual(result, {})


class IsAutostartEnabledTests(unittest.TestCase):
    def test_dispatches_to_backend(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.is_autostart_enabled.return_value = True
            result = pc.is_autostart_enabled(platform_name="linux")
        mock_select.assert_called_once_with("linux")
        self.assertTrue(result)


class EnableAutostartTests(unittest.TestCase):
    def test_defaults_to_sys_executable(self):
        with unittest.mock.patch.object(pc.sys, "executable", "/usr/bin/myapp"):
            with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
                pc.enable_autostart(platform_name="linux")
        mock_select.return_value.enable_autostart.assert_called_once_with("/usr/bin/myapp", "/usr/bin")

    def test_uses_explicit_target_and_working_dir(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            pc.enable_autostart(target_path="/opt/app/app", working_dir="/opt/app", platform_name="linux")
        mock_select.return_value.enable_autostart.assert_called_once_with("/opt/app/app", "/opt/app")

    def test_working_dir_derived_from_target_path_when_omitted(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            pc.enable_autostart(target_path="/opt/app/app", platform_name="linux")
        mock_select.return_value.enable_autostart.assert_called_once_with("/opt/app/app", "/opt/app")


class DisableAutostartTests(unittest.TestCase):
    def test_dispatches_to_backend(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.disable_autostart.return_value = True
            result = pc.disable_autostart(platform_name="darwin")
        mock_select.assert_called_once_with("darwin")
        self.assertTrue(result)


class HasPackageManagerTests(unittest.TestCase):
    def test_dispatches_to_backend(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.has_package_manager.return_value = True
            self.assertTrue(pc.has_package_manager(platform_name="win32"))
        mock_select.assert_called_once_with("win32")


class InstallOptionalDependencyDispatchTests(unittest.TestCase):
    def test_dispatches_with_package_and_timeout(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.install_optional_dependency.return_value = (True, None)
            result = pc.install_optional_dependency("ffmpeg", timeout=30, platform_name="darwin")
        mock_select.assert_called_once_with("darwin")
        mock_select.return_value.install_optional_dependency.assert_called_once_with("ffmpeg", timeout=30)
        self.assertEqual(result, (True, None))


class BuildLinuxInstallCommandDispatchTests(unittest.TestCase):
    def test_dispatches_to_backend(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.build_linux_install_command.return_value = "apt-get install -y ffmpeg"
            result = pc.build_linux_install_command("ffmpeg", platform_name="linux")
        mock_select.assert_called_once_with("linux")
        self.assertEqual(result, "apt-get install -y ffmpeg")


class RunLinuxInstallCommandDispatchTests(unittest.TestCase):
    def test_dispatches_with_command_and_timeout(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.run_linux_install_command.return_value = (True, None)
            result = pc.run_linux_install_command("apt-get install -y ffmpeg", timeout=30, platform_name="linux")
        mock_select.return_value.run_linux_install_command.assert_called_once_with(
            "apt-get install -y ffmpeg", timeout=30
        )
        self.assertEqual(result, (True, None))


class ObsConfigDirTests(unittest.TestCase):
    def test_windows_uses_appdata(self):
        # Built with os.path.join, the same way the real function does -- os.path.join on a
        # backslash-containing *input* string behaves differently depending on which real OS
        # runs the test (ntpath treats "\" as a separator, posixpath treats it as a literal
        # character), so only a same-mechanism comparison is correct on every OS this suite
        # actually runs on, per CROSS_PLATFORM_PLAN.md §3.2.
        appdata = r"C:\Users\Test\AppData\Roaming"
        with unittest.mock.patch.dict(os.environ, {"APPDATA": appdata}, clear=False):
            result = pc.obs_config_dir(platform_name="win32")
        self.assertEqual(result, os.path.join(appdata, "obs-studio"))

    def test_windows_returns_none_when_appdata_unset(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(pc.obs_config_dir(platform_name="win32"))

    def test_macos_uses_application_support(self):
        result = pc.obs_config_dir(platform_name="darwin")
        self.assertEqual(result, os.path.expanduser("~/Library/Application Support/obs-studio"))

    def test_linux_uses_dot_config(self):
        result = pc.obs_config_dir(platform_name="linux")
        self.assertEqual(result, os.path.expanduser("~/.config/obs-studio"))


class FindGogInstalledGamesDispatchTests(unittest.TestCase):
    def test_dispatches_to_backend(self):
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            mock_select.return_value.find_gog_installed_games.return_value = [{"display_name": "HITMAN 3"}]
            result = pc.find_gog_installed_games(["demo"], platform_name="win32")
        mock_select.assert_called_once_with("win32")
        mock_select.return_value.find_gog_installed_games.assert_called_once_with(["demo"])
        self.assertEqual(result, [{"display_name": "HITMAN 3"}])


class EmbedVideoPlayerTests(unittest.TestCase):
    def test_dispatches_to_backend(self):
        player = unittest.mock.Mock()
        widget = unittest.mock.Mock()
        with unittest.mock.patch.object(pc, "_select_backend") as mock_select:
            pc.embed_video_player(player, widget, platform_name="linux")
        mock_select.assert_called_once_with("linux")
        mock_select.return_value.embed_video_player.assert_called_once_with(player, widget)


class ExampleExecutableNameTests(unittest.TestCase):
    def test_windows_adds_exe_suffix(self):
        self.assertEqual(pc.example_executable_name(platform_name="win32"), "cs2.exe")

    def test_linux_has_no_suffix(self):
        self.assertEqual(pc.example_executable_name(platform_name="linux"), "cs2")

    def test_macos_has_no_suffix(self):
        self.assertEqual(pc.example_executable_name(platform_name="darwin"), "cs2")

    def test_custom_base_name(self):
        self.assertEqual(pc.example_executable_name("java", platform_name="win32"), "java.exe")
        self.assertEqual(pc.example_executable_name("java", platform_name="linux"), "java")


class ExecutableFiletypesTests(unittest.TestCase):
    def test_windows_filters_to_exe_by_default(self):
        filetypes = pc.executable_filetypes(platform_name="win32")
        self.assertEqual(filetypes, (("Executable", "*.exe"), ("All files", "*.*")))

    def test_linux_has_no_exe_filter(self):
        filetypes = pc.executable_filetypes(platform_name="linux")
        self.assertEqual(filetypes, (("All files", "*.*"),))

    def test_macos_has_no_exe_filter(self):
        filetypes = pc.executable_filetypes(platform_name="darwin")
        self.assertEqual(filetypes, (("All files", "*.*"),))


class BackendModulesAreSafelyImportableFromAnyOsTests(unittest.TestCase):
    """CROSS_PLATFORM_PLAN.md §3.2's whole premise: a test on ANY one real OS can still exercise
    what another OS's backend module would do, via mocking -- which requires every backend module
    to at least be IMPORTABLE regardless of which real OS is running the test (individual
    functions inside a "foreign" backend may still fail if actually CALLED without the right
    mocks, e.g. platform_windows's real winreg-touching code on real Linux -- that's expected and
    fine, only the import itself must never crash)."""

    def test_all_three_backends_import_cleanly_regardless_of_real_platform(self):
        for platform_name in ("win32", "darwin", "linux"):
            with self.subTest(platform_name=platform_name):
                backend = pc._select_backend(platform_name=platform_name)
                self.assertTrue(hasattr(backend, "find_obs_executable"))
                self.assertTrue(hasattr(backend, "find_vlc_directory"))
                self.assertTrue(hasattr(backend, "ffmpeg_candidates"))
                self.assertTrue(hasattr(backend, "LIBVLC_FILENAME"))


if __name__ == "__main__":
    unittest.main()
