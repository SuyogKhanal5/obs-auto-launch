"""Platform abstraction layer -- the one place autostart_script.py and installer.py go to reach
OS-specific behavior, rather than checking sys.platform (or importing ctypes.windll/winreg
directly) themselves. See CROSS_PLATFORM_PLAN.md for the full rationale, the complete Windows-
dependency catalog this is replacing piece by piece, and the phased rollout -- most of the
functions this module will eventually forward to don't exist yet; they land phase by phase.

Every dispatch decision here is overridable via an explicit `platform_name` parameter (defaulting
to sys.platform) specifically so a single test run, on a single OS, can exercise and assert on the
full behavior of every backend via mocking -- see CROSS_PLATFORM_PLAN.md §3.2. Write new backend
functions in platform_windows.py/platform_linux.py/platform_macos.py the same way: accept whatever
inputs they need as plain parameters (a path, a config dict, a fake filesystem via
unittest.mock.patch) rather than reaching for sys.platform or the real filesystem/registry
themselves wherever a test might reasonably want to substitute something.
"""
import subprocess
import sys


def _select_backend(platform_name=None):
    """Returns the platform_windows/platform_linux/platform_macos module matching platform_name
    (defaulting to sys.platform) -- the one place this module ever branches on OS."""
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name == "win32":
        import platform_windows as backend
    elif platform_name == "darwin":
        import platform_macos as backend
    else:
        import platform_linux as backend
    return backend


def hide_console_subprocess_kwargs():
    """kwargs to splat into a subprocess.run/Popen call to stop a console window from flashing up
    behind this app -- only meaningful on Windows (subprocess.CREATE_NO_WINDOW doesn't exist on
    other platforms, and there's no console-subsystem concept to suppress there anyway). A no-op
    empty dict everywhere else, so callers can always write
    `subprocess.run(cmd, **platform_common.hide_console_subprocess_kwargs())` unconditionally."""
    if sys.platform == "win32":
        # getattr rather than a direct attribute reference -- CREATE_NO_WINDOW only exists on a
        # real Windows Python build; this keeps the function itself from raising if it's ever
        # reached with sys.platform reporting "win32" in an environment where that attribute
        # genuinely isn't there (e.g. exercised by a test on a different real OS), the same
        # defensive spirit as every other platform_common function being safely callable
        # regardless of which real OS is running the test.
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}
