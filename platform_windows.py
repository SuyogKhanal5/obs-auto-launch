"""Windows backend for platform_common.py. Functions land here phase by phase as they're moved
out of autostart_script.py/installer.py -- see CROSS_PLATFORM_PLAN.md for the full migration
schedule.

In *normal* operation this module is only ever imported when sys.platform == "win32" (via
platform_common._select_backend's default dispatch), so its own winreg import could safely be
unconditional the same reasoning that guards autostart_script.py's own winreg import wouldn't
apply here. But CROSS_PLATFORM_PLAN.md §3.2's whole premise is that _select_backend can be forced
to return this module's dispatch *from a test running on real Linux/macOS CI* (to exercise
platform-selection logic without needing real Windows) -- an unconditional `import winreg` here
would defeat that by crashing at import time on those runners even before any function of this
module's actually gets called. Guarded the same way for the same reason."""
import glob
import os
import shutil
import sys

if sys.platform == "win32":
    import winreg

LIBVLC_FILENAME = "libvlc.dll"


def ffmpeg_candidates():
    """Common places ffmpeg ends up on Windows without being on PATH -- the package managers
    people actually use to get ffmpeg there (winget, Chocolatey, Scoop) plus a couple of common
    manual-install folders. Moved unchanged from autostart_script.py's old find_ffmpeg()."""
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        os.path.expandvars(r"%USERPROFILE%\scoop\shims\ffmpeg.exe"),
    ]
    try:
        candidates += glob.glob(
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\**\ffmpeg.exe"),
            recursive=True,
        )
    except OSError:
        pass
    return candidates


DEFAULT_OBS_PATHS = [
    r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
]


def find_obs_executable():
    """Moved unchanged from installer.py's old find_obs_exe()."""
    for path in DEFAULT_OBS_PATHS:
        if os.path.isfile(path):
            return path

    for hive, subkey in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OBS Studio"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                install_location = winreg.QueryValueEx(key, "InstallLocation")[0]
        except OSError:
            continue
        candidate = os.path.join(install_location, "bin", "64bit", "obs64.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def find_vlc_directory():
    """Moved unchanged from autostart_script.py's old find_vlc(): checks the registry key VLC
    itself writes on install (what python-vlc actually loads libvlc.dll relative to), then common
    Program Files locations, then a PATH lookup of the vlc.exe binary itself."""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"Software\VideoLAN\VLC") as key:
                install_dir = winreg.QueryValueEx(key, "InstallDir")[0]
        except OSError:
            continue
        if install_dir and os.path.isfile(os.path.join(install_dir, LIBVLC_FILENAME)):
            return install_dir

    for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_var)
        if not base:
            continue
        candidate = os.path.join(base, "VideoLAN", "VLC")
        if os.path.isfile(os.path.join(candidate, LIBVLC_FILENAME)):
            return candidate

    vlc_exe = shutil.which("vlc")
    if vlc_exe:
        candidate = os.path.dirname(vlc_exe)
        if os.path.isfile(os.path.join(candidate, LIBVLC_FILENAME)):
            return candidate

    return None
