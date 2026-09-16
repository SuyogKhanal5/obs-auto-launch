"""Linux backend for platform_common.py -- see CROSS_PLATFORM_PLAN.md for the full migration
schedule.

Global hotkeys, window-title detection, and the clip editor's space-bar-while-video-focused
behavior are X11-only for v1 (see CROSS_PLATFORM_PLAN.md §2.3) -- Wayland sessions
(`os.environ.get("XDG_SESSION_TYPE") == "wayland"`) get a clear "not supported" result from the
relevant functions once they land, not a silent no-op or a crash."""
import logging
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


def find_steam_install_path():
    """Steam on Linux has no registry to ask -- checks its real, well-known install locations in
    the order Valve's own tooling tends to prefer: the classic ~/.steam/steam symlink Steam
    itself maintains, the newer ~/.local/share/Steam data dir it points at, and the Flatpak
    sandboxed location for anyone who installed it that way instead of a native package."""
    for candidate in (
        os.path.expanduser("~/.steam/steam"),
        os.path.expanduser("~/.local/share/Steam"),
        os.path.expanduser("~/.var/app/com.valvesoftware.Steam/.steam/steam"),
    ):
        if os.path.isdir(candidate):
            return candidate
    return None


def epic_manifest_dir():
    """Epic Games Launcher has no native Linux client at all -- only unofficial/community tools
    like Heroic Games Launcher, which is a different application with its own, different manifest
    format (not the .item JSON schema this function's Windows/macOS counterparts point at), so
    there's nothing this app's existing Epic-manifest-parsing code could point at here. See
    CROSS_PLATFORM_PLAN.md §2.5."""
    return None


def is_wayland_session():
    """True if the current desktop session is Wayland, per the same env var every major
    compositor (GNOME, KDE, Sway, ...) sets for exactly this detection purpose. Exposed on its
    own so get_window_titles (and, once they land, the Phase 3 hotkey functions) can all check
    the same thing the same way."""
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def get_window_titles():
    """Maps each visible top-level window's owning PID to its titles via X11's EWMH convention
    (_NET_CLIENT_LIST / _NET_WM_NAME / _NET_WM_PID) -- the standard every mainstream X11 window
    manager implements. Returns {} (never raises) under Wayland, which has no unprivileged
    cross-compositor equivalent at all (see CROSS_PLATFORM_PLAN.md §2.3), or if python-xlib isn't
    installed, or if connecting to the X server fails for any other reason -- window-title-based
    game detection is one optional input to a bigger detection pipeline (see
    autostart_script.py's find_target_process), not something worth this app crashing over.

    python-xlib is imported lazily, inside this function, rather than at module level --
    deliberately, so this whole module stays importable (for the cross-OS backend-dispatch
    testing CROSS_PLATFORM_PLAN.md §3.2 describes) even in an environment that doesn't have
    python-xlib installed at all, which is every non-Linux CI runner given it's a Linux-only
    requirements.txt dependency (see that file's own sys_platform marker)."""
    if is_wayland_session():
        logging.info(
            "Window-title-based game detection isn't supported under Wayland (no cross-compositor "
            "API exists for it) -- any watched-window rules that rely on a title match won't fire "
            "this session."
        )
        return {}

    try:
        import Xlib.X
        import Xlib.display
        import Xlib.error
    except ImportError:
        logging.warning(
            "python-xlib isn't installed -- window-title-based game detection rules won't match."
        )
        return {}

    try:
        display = Xlib.display.Display()
    except Xlib.error.DisplayError as exc:
        logging.warning("Could not connect to the X server for window-title detection: %s", exc)
        return {}

    try:
        root = display.screen().root
        net_client_list = display.intern_atom("_NET_CLIENT_LIST")
        net_wm_name = display.intern_atom("_NET_WM_NAME")
        net_wm_pid = display.intern_atom("_NET_WM_PID")
        utf8_string = display.intern_atom("UTF8_STRING")

        client_list = root.get_full_property(net_client_list, Xlib.X.AnyPropertyType)
        titles = {}
        if not client_list or not client_list.value:
            return titles

        for window_id in client_list.value:
            try:
                window = display.create_resource_object("window", window_id)
                name_prop = window.get_full_property(net_wm_name, utf8_string)
                if not name_prop or not name_prop.value:
                    continue
                title = name_prop.value
                if isinstance(title, bytes):
                    title = title.decode("utf-8", errors="replace")
                pid_prop = window.get_full_property(net_wm_pid, Xlib.X.AnyPropertyType)
                if not pid_prop or not pid_prop.value:
                    continue
                pid = pid_prop.value[0]
                titles.setdefault(pid, []).append(title)
            except Xlib.error.XError:
                # A window can close between listing it in _NET_CLIENT_LIST and querying its
                # properties -- skip it rather than letting one stale window id abort the whole
                # scan, the same "best effort, not all-or-nothing" spirit as the Windows backend's
                # own EnumWindows callback (which just skips a window it can't read too).
                continue
        return titles
    finally:
        display.close()
