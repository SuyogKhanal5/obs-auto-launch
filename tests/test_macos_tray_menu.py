import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import autostart_script as a


class ShowMacosTrayMenuTests(unittest.TestCase):
    # show_macos_tray_menu builds real Tk widgets (a Toplevel popup) -- like this app's other
    # window-opening functions (open_clip_editor_window, the Settings editor), that's exercised
    # live rather than via unit tests, which would need a real Tk event loop to mean anything.
    # This covers the one pure-logic branch: the guard against being called before the overlay's
    # Tk root exists at all, mirroring open_clip_editor_window's own "overlay isn't ready yet"
    # check.
    def test_logs_and_returns_when_overlay_root_is_not_ready(self):
        with self.assertLogs(level="WARNING"):
            a.show_macos_tray_menu(None, menu=None, icon=None)  # must not raise


if __name__ == "__main__":
    unittest.main()
