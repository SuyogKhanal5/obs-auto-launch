import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_linux as pl


class FfmpegCandidatesTests(unittest.TestCase):
    def test_includes_common_linux_install_locations(self):
        candidates = pl.ffmpeg_candidates()
        self.assertIn("/usr/bin/ffmpeg", candidates)
        self.assertIn("/usr/local/bin/ffmpeg", candidates)


class FindObsExecutableTests(unittest.TestCase):
    def test_finds_system_flatpak_export(self):
        def isfile(path):
            return path == "/var/lib/flatpak/exports/bin/com.obsproject.Studio"

        with unittest.mock.patch.object(pl.os.path, "isfile", side_effect=isfile):
            self.assertEqual(pl.find_obs_executable(), "/var/lib/flatpak/exports/bin/com.obsproject.Studio")

    def test_finds_user_flatpak_export(self):
        user_path = os.path.expanduser("~/.local/share/flatpak/exports/bin/com.obsproject.Studio")

        def isfile(path):
            return path == user_path

        with unittest.mock.patch.object(pl.os.path, "isfile", side_effect=isfile):
            self.assertEqual(pl.find_obs_executable(), user_path)

    def test_returns_none_when_no_flatpak_export_exists(self):
        # A native-package OBS (plain `obs` on PATH) is handled by platform_common's own
        # shutil.which check before this function is ever called -- this function only needs to
        # cover the Flatpak case, so "nothing found" here correctly means "no Flatpak install",
        # not "OBS isn't installed at all".
        with unittest.mock.patch.object(pl.os.path, "isfile", return_value=False):
            self.assertIsNone(pl.find_obs_executable())


class FindVlcDirectoryTests(unittest.TestCase):
    def test_returns_directory_of_vlc_on_path(self):
        with unittest.mock.patch.object(pl.shutil, "which", return_value="/usr/bin/vlc"):
            self.assertEqual(pl.find_vlc_directory(), "/usr/bin")

    def test_returns_none_when_vlc_not_on_path(self):
        with unittest.mock.patch.object(pl.shutil, "which", return_value=None):
            self.assertIsNone(pl.find_vlc_directory())


class IsWaylandSessionTests(unittest.TestCase):
    def test_wayland(self):
        with unittest.mock.patch.dict(pl.os.environ, {"XDG_SESSION_TYPE": "wayland"}, clear=True):
            self.assertTrue(pl.is_wayland_session())

    def test_x11(self):
        with unittest.mock.patch.dict(pl.os.environ, {"XDG_SESSION_TYPE": "x11"}, clear=True):
            self.assertFalse(pl.is_wayland_session())

    def test_unset(self):
        with unittest.mock.patch.dict(pl.os.environ, {}, clear=True):
            self.assertFalse(pl.is_wayland_session())


class GetWindowTitlesTests(unittest.TestCase):
    def test_wayland_session_returns_empty_without_touching_xlib(self):
        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=True):
            self.assertEqual(pl.get_window_titles(), {})

    def test_missing_python_xlib_returns_empty_not_raises(self):
        # A None entry in sys.modules makes any `import Xlib...` raise ImportError immediately,
        # regardless of whether python-xlib is actually installed in the environment running this
        # test -- needed because it genuinely IS installed on real Linux CI (a requirements.txt
        # dependency for sys_platform == "linux"), so this can't rely on a real absence to
        # exercise the ImportError path the way it could when the dependency wasn't installed yet.
        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=False):
            with unittest.mock.patch.dict(sys.modules, {"Xlib": None, "Xlib.X": None, "Xlib.display": None, "Xlib.error": None}):
                with self.assertLogs(level="WARNING"):
                    self.assertEqual(pl.get_window_titles(), {})

    def test_parses_client_list_into_pid_keyed_titles(self):
        # Builds a minimal fake Xlib module tree and injects it into sys.modules -- lets this
        # test exercise get_window_titles' real EWMH-property-parsing logic end to end without
        # python-xlib actually being installed, the same cross-OS-testability spirit as
        # CROSS_PLATFORM_PLAN.md §3.2 (just via sys.modules injection instead of
        # patch.object(..., create=True), since the real code does `import Xlib.display` etc.
        # rather than referencing a pre-existing module attribute).
        fake_xlib = unittest.mock.MagicMock()
        fake_xlib.X.AnyPropertyType = 0
        fake_xlib.error.DisplayError = type("DisplayError", (Exception,), {})
        fake_xlib.error.XError = type("XError", (Exception,), {})

        atoms = {"_NET_CLIENT_LIST": "atom-client-list", "_NET_WM_NAME": "atom-wm-name",
                 "_NET_WM_PID": "atom-wm-pid", "UTF8_STRING": "atom-utf8"}

        fake_display = unittest.mock.MagicMock()
        fake_display.intern_atom.side_effect = lambda name: atoms[name]

        fake_root = unittest.mock.MagicMock()
        fake_display.screen.return_value.root = fake_root
        fake_root.get_full_property.return_value = unittest.mock.MagicMock(value=[101, 102])

        def make_window(title, pid):
            window = unittest.mock.MagicMock()

            def get_full_property(atom, prop_type):
                if atom == atoms["_NET_WM_NAME"]:
                    return unittest.mock.MagicMock(value=title)
                if atom == atoms["_NET_WM_PID"]:
                    return unittest.mock.MagicMock(value=[pid])
                return None

            window.get_full_property.side_effect = get_full_property
            return window

        windows_by_id = {101: make_window(b"Balatro", 4321), 102: make_window("Discord", 5555)}
        fake_display.create_resource_object.side_effect = lambda kind, window_id: windows_by_id[window_id]
        fake_xlib.display.Display.return_value = fake_display

        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=False):
            with unittest.mock.patch.dict(
                sys.modules,
                {"Xlib": fake_xlib, "Xlib.X": fake_xlib.X, "Xlib.display": fake_xlib.display, "Xlib.error": fake_xlib.error},
            ):
                result = pl.get_window_titles()

        self.assertEqual(result, {4321: ["Balatro"], 5555: ["Discord"]})
        fake_display.close.assert_called_once()


class BuildLinuxInstallCommandTests(unittest.TestCase):
    def test_prefers_user_scoped_flatpak_for_obs(self):
        with unittest.mock.patch.object(pl.shutil, "which", side_effect=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None):
            command = pl.build_linux_install_command("obs")
        self.assertEqual(command, "flatpak install --user -y flathub com.obsproject.Studio")

    def test_falls_back_to_apt_for_obs_when_no_flatpak(self):
        with unittest.mock.patch.object(pl.shutil, "which", side_effect=lambda name: "/usr/bin/apt-get" if name == "apt-get" else None):
            command = pl.build_linux_install_command("obs")
        self.assertEqual(command, "apt-get install -y obs-studio")

    def test_ffmpeg_never_prefers_flatpak_even_when_available(self):
        with unittest.mock.patch.object(pl.shutil, "which", side_effect=lambda name: "/usr/bin/" + name if name in ("flatpak", "apt-get") else None):
            command = pl.build_linux_install_command("ffmpeg")
        self.assertEqual(command, "apt-get install -y ffmpeg")

    def test_dnf_command(self):
        with unittest.mock.patch.object(pl.shutil, "which", side_effect=lambda name: "/usr/bin/dnf" if name == "dnf" else None):
            self.assertEqual(pl.build_linux_install_command("ffmpeg"), "dnf install -y ffmpeg")

    def test_pacman_command(self):
        with unittest.mock.patch.object(pl.shutil, "which", side_effect=lambda name: "/usr/bin/pacman" if name == "pacman" else None):
            self.assertEqual(pl.build_linux_install_command("ffmpeg"), "pacman -S --noconfirm ffmpeg")

    def test_zypper_command(self):
        with unittest.mock.patch.object(pl.shutil, "which", side_effect=lambda name: "/usr/bin/zypper" if name == "zypper" else None):
            self.assertEqual(pl.build_linux_install_command("ffmpeg"), "zypper install -y ffmpeg")

    def test_returns_none_when_nothing_detected(self):
        with unittest.mock.patch.object(pl.shutil, "which", return_value=None):
            self.assertIsNone(pl.build_linux_install_command("ffmpeg"))
            self.assertIsNone(pl.build_linux_install_command("obs"))


class RunLinuxInstallCommandTests(unittest.TestCase):
    def test_user_scoped_flatpak_runs_directly_without_pkexec(self):
        with unittest.mock.patch.object(pl.subprocess, "run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
            success, reason = pl.run_linux_install_command("flatpak install --user -y flathub com.obsproject.Studio")
        self.assertTrue(success)
        self.assertIsNone(reason)
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "flatpak")

    def test_package_manager_command_runs_via_pkexec(self):
        with unittest.mock.patch.object(pl.subprocess, "run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
            success, reason = pl.run_linux_install_command("apt-get install -y ffmpeg")
        self.assertTrue(success)
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["pkexec", "apt-get", "install", "-y", "ffmpeg"])

    def test_nonzero_exit_code_reports_failure_with_reason(self):
        with unittest.mock.patch.object(pl.subprocess, "run") as mock_run:
            mock_run.return_value = unittest.mock.Mock(returncode=1, stdout="", stderr="permission denied")
            success, reason = pl.run_linux_install_command("apt-get install -y ffmpeg")
        self.assertFalse(success)
        self.assertIn("permission denied", reason)

    def test_missing_pkexec_reports_failure_not_raise(self):
        with unittest.mock.patch.object(pl.subprocess, "run", side_effect=OSError("not found")):
            success, reason = pl.run_linux_install_command("apt-get install -y ffmpeg")  # must not raise
        self.assertFalse(success)
        self.assertIsNotNone(reason)


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="obsautorec_test_xdg_config_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.env_patcher = unittest.mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp_dir}, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def test_disabled_by_default(self):
        self.assertFalse(pl.is_autostart_enabled())

    def test_enable_then_disable_round_trips(self):
        self.assertTrue(pl.enable_autostart("/opt/obsautorecorder/OBSAutoRecorder", "/opt/obsautorecorder"))
        self.assertTrue(pl.is_autostart_enabled())

        desktop_path = os.path.join(self.tmp_dir, "autostart", "OBSAutoRecorder.desktop")
        with open(desktop_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('Exec="/opt/obsautorecorder/OBSAutoRecorder"', content)
        self.assertIn("Path=/opt/obsautorecorder", content)
        self.assertIn("[Desktop Entry]", content)

        self.assertTrue(pl.disable_autostart())
        self.assertFalse(pl.is_autostart_enabled())

    def test_disable_when_never_enabled_is_a_no_op(self):
        self.assertTrue(pl.disable_autostart())

    def test_enable_failure_returns_false_not_raised(self):
        with unittest.mock.patch.object(pl.os, "makedirs", side_effect=OSError("boom")):
            self.assertFalse(pl.enable_autostart("/opt/app/app", "/opt/app"))


class EmbedVideoPlayerTests(unittest.TestCase):
    def test_calls_set_xwindow_with_widgets_winfo_id(self):
        player = unittest.mock.Mock()
        widget = unittest.mock.Mock()
        widget.winfo_id.return_value = 4242
        pl.embed_video_player(player, widget)
        player.set_xwindow.assert_called_once_with(4242)


class KeysymForKeyTests(unittest.TestCase):
    def test_letter_keys(self):
        self.assertEqual(pl.keysym_for_key("a"), 0x41)
        self.assertEqual(pl.keysym_for_key("Z"), 0x5A)

    def test_digit_keys(self):
        self.assertEqual(pl.keysym_for_key("5"), 0x35)

    def test_function_keys(self):
        self.assertEqual(pl.keysym_for_key("F1"), 0xFFBE)
        self.assertEqual(pl.keysym_for_key("F12"), 0xFFC9)

    def test_invalid_key_returns_none(self):
        self.assertIsNone(pl.keysym_for_key("F13"))
        self.assertIsNone(pl.keysym_for_key("Enter"))
        self.assertIsNone(pl.keysym_for_key(""))
        self.assertIsNone(pl.keysym_for_key(None))


class X11ModMaskForTests(unittest.TestCase):
    FAKE_X = types.SimpleNamespace(ControlMask=0x4, Mod1Mask=0x8, ShiftMask=0x1, Mod4Mask=0x40)

    def test_combines_flags(self):
        self.assertEqual(pl._x11_mod_mask_for(["ctrl", "alt"], self.FAKE_X), 0x4 | 0x8)

    def test_empty_or_none_is_zero(self):
        self.assertEqual(pl._x11_mod_mask_for([], self.FAKE_X), 0)
        self.assertEqual(pl._x11_mod_mask_for(None, self.FAKE_X), 0)

    def test_unknown_modifier_is_ignored(self):
        self.assertEqual(pl._x11_mod_mask_for(["ctrl", "bogus"], self.FAKE_X), 0x4)


class IgnorableModifierTests(unittest.TestCase):
    def make_fake_display(self, numlock_keycode=77, capslock_keycode=66, scrolllock_keycode=None):
        # 8 modifier slots in X's own defined order (Shift, Lock, Control, Mod1..Mod5) -- CapsLock
        # lands in the "Lock" slot (index 1) and NumLock in "Mod2" (index 4), the near-universal
        # real-world mapping, without hardcoding that assumption into the function under test
        # itself (it reads get_modifier_mapping() directly, not these indices).
        display = unittest.mock.MagicMock()
        mapping = [[] for _ in range(8)]
        mapping[1] = [capslock_keycode]
        mapping[4] = [numlock_keycode]
        display.get_modifier_mapping.return_value = mapping

        def keysym_to_keycode(keysym):
            return {
                pl._XK_CAPS_LOCK: capslock_keycode,
                pl._XK_NUM_LOCK: numlock_keycode,
                pl._XK_SCROLL_LOCK: scrolllock_keycode or 0,
            }.get(keysym, 0)

        display.keysym_to_keycode.side_effect = keysym_to_keycode
        return display

    def test_bits_found_for_capslock_and_numlock(self):
        display = self.make_fake_display()
        self.assertEqual(sorted(pl._ignorable_modifier_bits(display)), sorted([1 << 1, 1 << 4]))

    def test_combinations_cover_every_subset(self):
        display = self.make_fake_display()
        combos = pl._ignorable_modifier_combinations(display)
        self.assertEqual(sorted(combos), sorted([0, 1 << 1, 1 << 4, (1 << 1) | (1 << 4)]))

    def test_missing_lock_key_on_this_keyboard_is_skipped(self):
        # keysym_to_keycode returning 0 for a keysym this keyboard doesn't have (ScrollLock here)
        # must not add a bogus 0-keycode entry -- 0 isn't a valid keycode, and "in []" against an
        # empty modifier slot would silently misattribute it to slot 0 (Shift) otherwise.
        display = self.make_fake_display(scrolllock_keycode=None)
        self.assertEqual(len(pl._ignorable_modifier_bits(display)), 2)


class RegisterGlobalHotkeyTests(unittest.TestCase):
    FAKE_X = types.SimpleNamespace(ControlMask=0x4, Mod1Mask=0x8, ShiftMask=0x1, Mod4Mask=0x40, GrabModeAsync=0x1)

    def make_no_lock_keys_display(self, keycode=39):
        display = unittest.mock.MagicMock()
        display.keysym_to_keycode.return_value = keycode
        display.get_modifier_mapping.return_value = [[] for _ in range(8)]
        return display

    def test_registers_and_returns_a_handle(self):
        display = self.make_no_lock_keys_display()
        root = unittest.mock.MagicMock()
        with unittest.mock.patch.dict(sys.modules, {"Xlib": unittest.mock.MagicMock(X=self.FAKE_X), "Xlib.X": self.FAKE_X}):
            handle = pl.register_global_hotkey(display, root, ["ctrl"], "S")
        self.assertEqual(handle, (39, 0x4))
        root.grab_key.assert_called_once_with(39, 0x4, True, 0x1, 0x1)

    def test_invalid_key_returns_none_without_grabbing(self):
        display = self.make_no_lock_keys_display()
        root = unittest.mock.MagicMock()
        with unittest.mock.patch.dict(sys.modules, {"Xlib": unittest.mock.MagicMock(X=self.FAKE_X), "Xlib.X": self.FAKE_X}):
            handle = pl.register_global_hotkey(display, root, [], "NotAKey")
        self.assertIsNone(handle)
        root.grab_key.assert_not_called()

    def test_unresolvable_keycode_returns_none(self):
        display = self.make_no_lock_keys_display(keycode=0)
        root = unittest.mock.MagicMock()
        with unittest.mock.patch.dict(sys.modules, {"Xlib": unittest.mock.MagicMock(X=self.FAKE_X), "Xlib.X": self.FAKE_X}):
            handle = pl.register_global_hotkey(display, root, [], "S")
        self.assertIsNone(handle)
        root.grab_key.assert_not_called()


class UnregisterGlobalHotkeyTests(unittest.TestCase):
    def test_ungrabs_every_ignorable_combination(self):
        display = unittest.mock.MagicMock()
        display.get_modifier_mapping.return_value = [[] for _ in range(8)]
        root = unittest.mock.MagicMock()
        pl.unregister_global_hotkey(display, root, (39, 0x4))
        root.ungrab_key.assert_called_once_with(39, 0x4)

    def test_none_handle_is_a_no_op(self):
        root = unittest.mock.MagicMock()
        pl.unregister_global_hotkey(unittest.mock.MagicMock(), root, None)
        root.ungrab_key.assert_not_called()

    def test_ungrab_failure_is_swallowed(self):
        display = unittest.mock.MagicMock()
        display.get_modifier_mapping.return_value = [[] for _ in range(8)]
        root = unittest.mock.MagicMock()
        root.ungrab_key.side_effect = Exception("boom")
        pl.unregister_global_hotkey(display, root, (39, 0x4))  # must not raise


class RunCustomKeybindListenerTests(unittest.TestCase):
    def make_fake_xlib(self, next_event_side_effect, keycode=39):
        fake_x = types.SimpleNamespace(
            ControlMask=0x4, Mod1Mask=0x8, ShiftMask=0x1, Mod4Mask=0x40, GrabModeAsync=0x1,
            KeyPressMask=0x1, KeyPress=2,
        )
        fake_root = unittest.mock.MagicMock()
        fake_screen = unittest.mock.MagicMock()
        fake_screen.root = fake_root

        fake_display = unittest.mock.MagicMock()
        fake_display.screen.return_value = fake_screen
        fake_display.keysym_to_keycode.return_value = keycode
        fake_display.get_modifier_mapping.return_value = [[] for _ in range(8)]
        fake_display.next_event.side_effect = next_event_side_effect

        fake_display_module = unittest.mock.MagicMock()
        fake_display_module.Display.return_value = fake_display

        fake_error_module = unittest.mock.MagicMock()
        fake_error_module.DisplayError = type("DisplayError", (Exception,), {})

        fake_xlib = unittest.mock.MagicMock()
        fake_xlib.X = fake_x
        fake_xlib.display = fake_display_module
        fake_xlib.error = fake_error_module

        modules = {
            "Xlib": fake_xlib, "Xlib.X": fake_x, "Xlib.display": fake_display_module, "Xlib.error": fake_error_module,
        }
        return modules, fake_root, fake_display

    def test_fires_matching_binding_on_a_dedicated_thread(self):
        key_event = unittest.mock.MagicMock(type=2, detail=39, state=0x4)
        modules, fake_root, fake_display = self.make_fake_xlib(
            next_event_side_effect=[key_event, RuntimeError("stop the test loop")],
        )
        binding = {"enabled": True, "modifiers": ["ctrl"], "key": "S", "action": "save_replay_buffer"}
        fire_keybind = unittest.mock.Mock()
        describe_keybind = unittest.mock.Mock(return_value="Ctrl+S")
        notify = unittest.mock.Mock()
        fake_thread = unittest.mock.MagicMock()

        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=False):
            with unittest.mock.patch.object(pl.threading, "Thread", return_value=fake_thread) as mock_thread_cls:
                with unittest.mock.patch.dict(sys.modules, modules):
                    with self.assertLogs(level="ERROR"):  # the injected RuntimeError gets logged, not raised
                        pl.run_custom_keybind_listener(
                            [binding], get_client="get_client", get_manual_split_buffer_seconds="get_seconds",
                            fire_keybind=fire_keybind, describe_keybind=describe_keybind, notify=notify,
                            icon="icon", notifications_config="notif_cfg", status="status",
                        )

        fake_root.grab_key.assert_called_once_with(39, 0x4, True, 0x1, 0x1)
        mock_thread_cls.assert_called_once_with(
            target=fire_keybind,
            args=(binding, "get_client", "get_seconds", "icon", "notif_cfg", "status"),
            daemon=True,
        )
        fake_thread.start.assert_called_once()
        fake_root.ungrab_key.assert_called_once_with(39, 0x4)
        fake_display.close.assert_called_once()

    def test_non_matching_key_press_does_not_fire(self):
        other_key_event = unittest.mock.MagicMock(type=2, detail=999, state=0x4)
        modules, fake_root, fake_display = self.make_fake_xlib(
            next_event_side_effect=[other_key_event, RuntimeError("stop the test loop")],
        )
        binding = {"enabled": True, "modifiers": ["ctrl"], "key": "S", "action": "save_replay_buffer"}
        fire_keybind = unittest.mock.Mock()

        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=False):
            with unittest.mock.patch.object(pl.threading, "Thread") as mock_thread_cls:
                with unittest.mock.patch.dict(sys.modules, modules):
                    with self.assertLogs(level="ERROR"):
                        pl.run_custom_keybind_listener(
                            [binding], get_client=lambda: None, get_manual_split_buffer_seconds=lambda: 0,
                            fire_keybind=fire_keybind, describe_keybind=unittest.mock.Mock(), notify=unittest.mock.Mock(),
                        )

        mock_thread_cls.assert_not_called()

    def test_disabled_binding_is_not_registered(self):
        modules, fake_root, fake_display = self.make_fake_xlib(next_event_side_effect=[])
        binding = {"enabled": False, "modifiers": [], "key": "S", "action": "save_replay_buffer"}

        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=False):
            with unittest.mock.patch.dict(sys.modules, modules):
                pl.run_custom_keybind_listener(
                    [binding], get_client=lambda: None, get_manual_split_buffer_seconds=lambda: 0,
                    fire_keybind=unittest.mock.Mock(), describe_keybind=unittest.mock.Mock(), notify=unittest.mock.Mock(),
                )

        fake_root.grab_key.assert_not_called()
        fake_display.next_event.assert_not_called()  # nothing registered -> returns before the event loop

    def test_invalid_key_is_logged_and_skipped(self):
        modules, fake_root, fake_display = self.make_fake_xlib(next_event_side_effect=[])
        binding = {"enabled": True, "modifiers": [], "key": "NotAKey", "action": "save_replay_buffer"}

        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=False):
            with unittest.mock.patch.dict(sys.modules, modules):
                with self.assertLogs(level="WARNING"):
                    pl.run_custom_keybind_listener(
                        [binding], get_client=lambda: None, get_manual_split_buffer_seconds=lambda: 0,
                        fire_keybind=unittest.mock.Mock(), describe_keybind=unittest.mock.Mock(), notify=unittest.mock.Mock(),
                    )

        fake_root.grab_key.assert_not_called()

    def test_wayland_session_returns_without_opening_display(self):
        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=True):
            with self.assertLogs(level="WARNING"):
                pl.run_custom_keybind_listener(
                    [{"enabled": True, "modifiers": [], "key": "S"}], get_client=lambda: None,
                    get_manual_split_buffer_seconds=lambda: 0, fire_keybind=unittest.mock.Mock(),
                    describe_keybind=unittest.mock.Mock(), notify=unittest.mock.Mock(),
                )  # must not raise, and must never touch Xlib at all

    def test_missing_python_xlib_is_logged_not_raised(self):
        with unittest.mock.patch.object(pl, "is_wayland_session", return_value=False):
            with unittest.mock.patch.dict(
                sys.modules, {"Xlib": None, "Xlib.X": None, "Xlib.display": None, "Xlib.error": None},
            ):
                with self.assertLogs(level="WARNING"):
                    pl.run_custom_keybind_listener(
                        [{"enabled": True, "modifiers": [], "key": "S"}], get_client=lambda: None,
                        get_manual_split_buffer_seconds=lambda: 0, fire_keybind=unittest.mock.Mock(),
                        describe_keybind=unittest.mock.Mock(), notify=unittest.mock.Mock(),
                    )  # must not raise


class ProcessAudioCaptureSettingsTests(unittest.TestCase):
    # Confirmed negative finding, not an unimplemented feature -- CROSS_PLATFORM_PLAN.md §6.1: a
    # real CI spike found no per-application audio capture kind exists under PulseAudio.
    def test_kind_is_none(self):
        self.assertIsNone(pl.PROCESS_AUDIO_CAPTURE_KIND)

    def test_settings_is_always_none(self):
        self.assertIsNone(pl.process_audio_capture_settings("Balatro", "/usr/bin/balatro"))
        self.assertIsNone(pl.process_audio_capture_settings("Balatro", None))


if __name__ == "__main__":
    unittest.main()
