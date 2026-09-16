"""macOS backend for platform_common.py -- see CROSS_PLATFORM_PLAN.md for the full migration
schedule.

Minimum target: macOS 13 (Ventura) -- see CROSS_PLATFORM_PLAN.md §2.6."""
import glob
import os
import shutil

# The canonical libvlc shared-library name on macOS. Only used as a presence signal by
# platform_common.find_vlc_directory() -- see that function's own docstring for why the exact
# directory contents don't matter the way they do on Windows.
LIBVLC_FILENAME = "libvlc.dylib"


def ffmpeg_candidates():
    """Common places ffmpeg ends up on macOS without being on PATH -- Homebrew's two install
    roots (Apple Silicon vs. Intel) and MacPorts. shutil.which("ffmpeg") (tried first by
    platform_common.find_ffmpeg_executable) already covers a normal Homebrew-linked install,
    since `brew link` puts it on PATH -- these are for an unlinked/keg-only situation."""
    return [
        "/opt/homebrew/bin/ffmpeg",  # Homebrew on Apple Silicon
        "/usr/local/bin/ffmpeg",  # Homebrew on Intel
        "/opt/local/bin/ffmpeg",  # MacPorts
    ]


def find_obs_executable():
    """OBS on macOS ships as a .app bundle, not a bare PATH-findable binary -- shutil.which
    (tried first by platform_common.find_obs_executable) won't find it at all. The actual
    executable lives inside the bundle at Contents/MacOS/OBS."""
    for candidate in (
        "/Applications/OBS.app/Contents/MacOS/OBS",
        os.path.expanduser("~/Applications/OBS.app/Contents/MacOS/OBS"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def find_vlc_directory():
    """VLC on macOS also ships as a .app bundle. Checks the standard Applications locations for
    the bundle's embedded libvlc first (most reliable, doesn't depend on a PATH-findable `vlc`
    binary existing at all, which VLC.app doesn't provide by default), then falls back to a PATH
    lookup the same way the Linux backend does, for anyone who installed a bare VLC binary via
    Homebrew instead of the .app bundle."""
    for app_dir in ("/Applications/VLC.app", os.path.expanduser("~/Applications/VLC.app")):
        lib_dir = os.path.join(app_dir, "Contents", "MacOS", "lib")
        if os.path.isfile(os.path.join(lib_dir, LIBVLC_FILENAME)):
            return lib_dir
        # Some VLC.app releases ship the dylib directly under Contents/MacOS/ instead of a lib/
        # subfolder -- checked as a fallback rather than assumed, since this has changed across
        # VLC versions.
        if os.path.isfile(os.path.join(app_dir, "Contents", "MacOS", LIBVLC_FILENAME)):
            return os.path.join(app_dir, "Contents", "MacOS")

    vlc_exe = shutil.which("vlc")
    if vlc_exe:
        return os.path.dirname(vlc_exe)

    # Homebrew's VLC cask/formula, if not on PATH for some reason.
    for pattern in ("/opt/homebrew/Cellar/vlc/*/lib", "/usr/local/Cellar/vlc/*/lib"):
        for candidate in glob.glob(pattern):
            if os.path.isfile(os.path.join(candidate, LIBVLC_FILENAME)):
                return candidate

    return None
