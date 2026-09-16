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
import os
import shutil
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


def find_obs_executable(configured_path=None, platform_name=None):
    """Best-effort search for an OBS Studio install on this machine. An explicit configured_path
    that still exists always wins as-is; otherwise falls back to this OS's own discovery
    mechanism (see platform_windows/platform_linux/platform_macos's own find_obs_executable).
    Returns None if nothing is found anywhere -- OBS discovery has always been opt-in/best-effort,
    never something this app hard-requires to find on its own."""
    if configured_path and os.path.isfile(configured_path):
        return configured_path
    return _select_backend(platform_name).find_obs_executable()


def find_ffmpeg_executable(configured_path=None, platform_name=None):
    """Resolves an ffmpeg binary. Precedence (identical across every OS -- only the final
    fallback's candidate list differs per platform):
    1. configured_path, if it's an absolute path that exists, wins as-is.
    2. Otherwise a PATH lookup of whatever was configured (so plain "ffmpeg" keeps working the
       normal way once it's on PATH).
    3. Otherwise this OS's own list of common non-PATH install locations (see each backend's
       ffmpeg_candidates()).
    Returns None if ffmpeg isn't findable anywhere -- the features that need it are opt-in."""
    configured_path = configured_path or "ffmpeg"
    if os.path.isabs(configured_path) and os.path.isfile(configured_path):
        return configured_path
    found = shutil.which(configured_path)
    if found:
        return found
    for candidate in _select_backend(platform_name).ffmpeg_candidates():
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def find_ffprobe_executable(ffmpeg_path):
    """Given a resolved ffmpeg binary path, returns the ffprobe binary expected to sit alongside
    it (true of winget/Homebrew/apt/Flatpak packages alike) -- or None if it's not there. Derives
    ffprobe's filename from ffmpeg_path's own name rather than a hardcoded "ffprobe.exe": correct
    on Windows (ffmpeg.exe -> ffprobe.exe) and Linux/macOS (ffmpeg -> ffprobe, no extension)
    alike, and even for an unusually-named ffmpeg binary someone configured by hand."""
    ffmpeg_dir = os.path.dirname(ffmpeg_path)
    ffmpeg_name = os.path.basename(ffmpeg_path)
    if ffmpeg_name.lower().startswith("ffmpeg"):
        ffprobe_name = "ffprobe" + ffmpeg_name[len("ffmpeg"):]
    else:
        ffprobe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    ffprobe_path = os.path.join(ffmpeg_dir, ffprobe_name)
    return ffprobe_path if os.path.isfile(ffprobe_path) else None


def libvlc_filename(platform_name=None):
    """The filename python-vlc's underlying libvlc shared library has on this OS -- "libvlc.dll"
    on Windows, "libvlc.so.5" on Linux, "libvlc.dylib" on macOS. Exposed on its own (not just
    used internally by find_vlc_directory below) since autostart_script.py's own
    resolve_vlc_path also needs it for an explicit-configured-directory presence check -- the
    previous implementation hardcoded "libvlc.dll" there, a real bug on any OS but Windows."""
    return _select_backend(platform_name).LIBVLC_FILENAME


def find_vlc_directory(configured_path=None, platform_name=None):
    """Resolves a VLC install: an explicit configured directory that still contains this OS's own
    libvlc file wins as-is; otherwise falls back to this OS's own discovery. Returns None if VLC
    can't be found anywhere. The returned value is used purely as a presence signal (whether it's
    safe to `import vlc` at all, and what to show in Settings) -- nothing downstream depends on
    the exact directory contents beyond that, which is why the Linux/macOS backends can return a
    directory that merely indicates "VLC is installed" rather than needing to replicate Windows'
    exact "this specific folder is what python-vlc will load" contract; python-vlc's own find_lib()
    already knows how to locate libvlc itself once it's anywhere the OS's normal library search
    covers, which is always true for a package-manager-installed VLC on Linux/macOS."""
    if configured_path and os.path.isfile(os.path.join(configured_path, libvlc_filename(platform_name))):
        return configured_path
    return _select_backend(platform_name).find_vlc_directory()
