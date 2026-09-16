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


if __name__ == "__main__":
    unittest.main()
