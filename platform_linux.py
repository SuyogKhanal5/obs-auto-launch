"""Linux backend for platform_common.py -- see CROSS_PLATFORM_PLAN.md for the full migration
schedule.

Global hotkeys, window-title detection, and the clip editor's space-bar-while-video-focused
behavior are X11-only for v1 (see CROSS_PLATFORM_PLAN.md §2.3) -- Wayland sessions
(`os.environ.get("XDG_SESSION_TYPE") == "wayland"`) get a clear "not supported" result from the
relevant functions once they land, not a silent no-op or a crash."""
import os
import shutil

# The canonical libvlc shared-object name on Linux (SONAME-versioned, unlike Windows/macOS).
# Only used as a presence signal by platform_common.find_vlc_directory() -- see that function's
# own docstring for why the exact directory contents don't matter the way they do on Windows.
LIBVLC_FILENAME = "libvlc.so.5"


def ffmpeg_candidates():
    """Common places ffmpeg ends up on Linux without being on PATH -- most distros package it at
    one of these standard locations even in an environment with an unusual PATH (e.g. a minimal
    container). shutil.which("ffmpeg") (tried first by platform_common.find_ffmpeg_executable)
    already covers the common case of a normally-installed package."""
    return [
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/snap/bin/ffmpeg",
        os.path.expanduser("~/.local/bin/ffmpeg"),
    ]


# OBS's Flatpak app id -- Flatpak deliberately doesn't put its own launcher scripts on PATH by
# default, so shutil.which("obs") (already tried first by platform_common.find_obs_executable)
# won't see a Flatpak-installed OBS; these are Flatpak's own fixed export paths for it.
_FLATPAK_OBS_PATHS = [
    "/var/lib/flatpak/exports/bin/com.obsproject.Studio",
    os.path.expanduser("~/.local/share/flatpak/exports/bin/com.obsproject.Studio"),
]


def find_obs_executable():
    """OBS on Linux is normally either a native package (obs on PATH, already covered by
    platform_common's shutil.which check before this ever runs) or a Flatpak -- Flatpak is
    actually the more reliably up-to-date option on many distros, since OBS often isn't in a
    distro's own default repos (see CROSS_PLATFORM_PLAN.md §2.7's note on Fedora specifically)."""
    for path in _FLATPAK_OBS_PATHS:
        if os.path.isfile(path):
            return path
    return None


def find_vlc_directory():
    """Confirms a VLC install exists via a PATH lookup of the `vlc` binary -- unlike Windows,
    libvlc on Linux is a shared library resolved through the system's normal dynamic linker
    search path (ldconfig) once installed via a package manager, not necessarily sitting next to
    the `vlc` binary itself. python-vlc's own find_lib() (used internally by `import vlc`)
    already knows how to load it from there without needing an explicit directory hint the way
    the Windows backend's DLL-search-path-style contract does -- so this only needs to confirm
    VLC is present at all, not locate libvlc's exact directory."""
    vlc_exe = shutil.which("vlc")
    if not vlc_exe:
        return None
    return os.path.dirname(vlc_exe)
