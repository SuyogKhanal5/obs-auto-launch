"""Security properties found in a review: who can read the app's secrets, who can change its
files, and whether another program can borrow its permissions."""
import json
import os
import plistlib
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_common
import platform_macos
import platform_windows

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ci"))
import finalize_macos_app


@unittest.skipUnless(os.name == "posix", "file modes are POSIX")
class PrivateFileTests(unittest.TestCase):
    def test_new_file_is_readable_only_by_its_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            platform_common.write_private_text(path, "{}")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_existing_world_readable_file_is_tightened(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            open(path, "w").close(); os.chmod(path, 0o644)
            platform_common.write_private_text(path, '{"a": 1}')
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(json.load(open(path)), {"a": 1})

    def test_log_file_is_private(self):
        import autostart_script as a
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.log")
            open(path, "w").close(); os.chmod(path, 0o644)
            a.LineCappedFileHandler(path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class WindowsDataDirTests(unittest.TestCase):
    """Program Files stays admin-only; settings and logs go per user instead."""

    def env(self, tmp):
        return {"ProgramFiles": os.path.join(tmp, "Program Files"), "APPDATA": os.path.join(tmp, "AppData")}

    def test_program_files_install_keeps_data_in_appdata(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            install = os.path.join(tmp, "Program Files", "OBSAutoRecorder")
            data = platform_windows.app_data_dir(install)
            self.assertEqual(data, os.path.join(tmp, "AppData", "OBSAutoRecorder"))
            self.assertTrue(os.path.isdir(data))

    def test_portable_copy_keeps_data_beside_it(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            portable = os.path.join(tmp, "USB", "OBSAutoRecorder")
            self.assertEqual(platform_windows.app_data_dir(portable), portable)

    def test_a_lookalike_folder_isnt_mistaken_for_program_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, self.env(tmp), clear=False):
            lookalike = os.path.join(tmp, "Program Files Extra", "OBSAutoRecorder")
            self.assertEqual(platform_windows.app_data_dir(lookalike), lookalike)


class SeedConfigTests(unittest.TestCase):
    def test_first_run_copies_the_installers_config_to_the_data_folder(self):
        import autostart_script as a
        with tempfile.TemporaryDirectory() as install, tempfile.TemporaryDirectory() as data:
            with open(os.path.join(install, "config.json"), "w") as f:
                json.dump({"seed": True}, f)
            target = os.path.join(data, "config.json")
            with patch.object(a, "SCRIPT_DIR", install), patch.object(a, "DATA_DIR", data), \
                    patch.object(a, "CONFIG_PATH", target):
                a.seed_config_from_install_dir()
                self.assertEqual(json.load(open(target)), {"seed": True})
                # An existing per-user config is never overwritten by the seed.
                with open(target, "w") as f:
                    json.dump({"mine": True}, f)
                a.seed_config_from_install_dir()
                self.assertEqual(json.load(open(target)), {"mine": True})


class RemoveUserWriteAccessTests(unittest.TestCase):
    def test_removes_the_users_grant_recursively(self):
        import installer
        with patch.object(installer.sys, "platform", "win32"), \
                patch.object(installer.platform_common, "hide_console_subprocess_kwargs", return_value={}), \
                patch.object(installer.subprocess, "run", return_value=unittest.mock.Mock(returncode=0)) as run:
            self.assertTrue(installer.remove_user_write_access("C:\\\\Program Files\\\\OBSAutoRecorder"))
        command = run.call_args[0][0]
        self.assertEqual(command[:3], ["icacls", "C:\\\\Program Files\\\\OBSAutoRecorder", "/remove:g"])
        self.assertIn("*S-1-5-32-545", command)
        self.assertNotIn("/grant", command)


class ResetPermissionsOnceTests(unittest.TestCase):
    def test_resets_each_permission_once_then_never_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(platform_macos.subprocess, "run") as run:
                self.assertTrue(platform_macos.reset_permissions_once(tmp))
                self.assertFalse(platform_macos.reset_permissions_once(tmp))
            commands = [c[0][0] for c in run.call_args_list]
            self.assertEqual(commands, [
                ["tccutil", "reset", s, "com.obsautorecorder.app"]
                for s in ("Accessibility", "ScreenCapture", "AudioCapture")
            ])


class MacSigningTests(unittest.TestCase):
    """Confirmed live: an identifier-only designated requirement let an impostor app inherit
    Accessibility (keylogging) and Screen Recording. Signing must never produce one."""

    def test_signing_never_sets_an_identifier_only_requirement(self):
        source = open(finalize_macos_app.__file__).read()
        self.assertNotIn("=designated => identifier", source)

    def test_hardened_runtime_always_on_for_app_code(self):
        command = finalize_macos_app.codesign_command("X.app", entitlements="e.plist", deep=True)
        self.assertIn("runtime", command[command.index("--options") + 1])
        self.assertEqual(command[command.index("--sign") + 1], "-")

    def test_certificate_identity_and_keychain_used_when_configured(self):
        command = finalize_macos_app.codesign_command("X.app", identity="ABC123", keychain="/k.keychain-db")
        self.assertEqual(command[command.index("--sign") + 1], "ABC123")
        self.assertEqual(command[command.index("--keychain") + 1], "/k.keychain-db")

    def test_entitlements_never_allow_dyld_injection(self):
        for name in ("app.entitlements", "process_audio_tap.entitlements"):
            path = os.path.join(finalize_macos_app.MACOS_DIR, name)
            entitlements = plistlib.load(open(path, "rb"))
            self.assertNotIn("com.apple.security.cs.allow-dyld-environment-variables", entitlements)


if __name__ == "__main__":
    unittest.main()


class DriverVerificationTests(unittest.TestCase):
    REQ = 'certificate leaf = H"0f91a41ddea6218226fb73c9135bcd5aa9661fd5"'

    def test_install_verifies_the_root_owned_copy_before_core_audio_loads_it(self):
        command = platform_macos.game_audio_driver_install_command("/x/Game Audio.driver", self.REQ)
        target = "'/Library/Audio/Plug-Ins/HAL/OBSAutoRecorderGameAudio.driver'"
        chown, verify, restart = command.index("chown"), command.index("codesign --verify"), command.index("killall")
        self.assertLess(chown, verify); self.assertLess(verify, restart)
        self.assertIn(f"-R='{self.REQ}' {target}", command)
        self.assertIn(f"|| {{ rm -rf {target}; exit 1; }}", command)

    def test_without_a_certificate_nothing_is_verified(self):
        self.assertNotIn("codesign", platform_macos.game_audio_driver_install_command("/x/d.driver"))

    def test_reads_the_certificate_requirement_from_codesign(self):
        output = 'Executable=/x\ndesignated => identifier "com.obsautorecorder.app" and certificate leaf = H"0F91A41DDEA6218226FB73C9135BCD5AA9661FD5"\n'
        with patch.object(platform_macos.subprocess, "run", return_value=unittest.mock.Mock(stdout=output, stderr="")):
            self.assertEqual(platform_macos.signing_certificate_requirement("/x.app"), self.REQ)
        with patch.object(platform_macos.subprocess, "run", return_value=unittest.mock.Mock(stdout='designated => cdhash H"ab"', stderr="")):
            self.assertIsNone(platform_macos.signing_certificate_requirement("/x.app"))
