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
import logging
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


def find_steam_install_path(platform_name=None):
    """Best-effort search for Steam's install root on this machine -- the base directory
    steamapps/libraryfolders.vdf (and steamapps/common for whatever's installed directly there)
    lives under. Returns None if Steam isn't found anywhere. Steam ships a native client on every
    target OS, so every backend implements this for real (unlike Epic/Battle.net, which are
    Windows+macOS only, or GOG/Xbox, which are Windows only -- see CROSS_PLATFORM_PLAN.md §2.5)."""
    return _select_backend(platform_name).find_steam_install_path()


def epic_manifest_dir(platform_name=None):
    """Directory Epic Games Launcher writes its .item install manifests to, or None on an OS Epic
    has no native client for (Linux -- see CROSS_PLATFORM_PLAN.md §2.5). The manifest FORMAT
    itself is identical cross-platform (the same .item JSON schema); only this directory differs,
    which is why get_epic_installed_games's own manifest-parsing logic in autostart_script.py
    needs no OS-specific changes beyond calling this instead of a hardcoded Windows path."""
    return _select_backend(platform_name).epic_manifest_dir()


def get_window_titles(platform_name=None):
    """Maps each visible top-level window's owning PID to a list of that window's titles -- used
    for "window title contains X" game-detection rules (e.g. Minecraft's javaw.exe, whose process
    name alone is too generic to watch for on its own). Returns {} (not an error) when this isn't
    supported at all in the current session -- e.g. a Wayland desktop, which has no unprivileged
    cross-compositor API for this; see CROSS_PLATFORM_PLAN.md §2.3. Never raises: a backend
    failing for any other reason (a missing optional dependency, an X server that's unreachable
    for some other reason) should degrade the same way -- one logged warning, not a crash."""
    try:
        return _select_backend(platform_name).get_window_titles()
    except Exception:
        logging.exception("get_window_titles() failed -- window-title-based game detection rules won't match this run.")
        return {}


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


def example_executable_name(base_name="cs2", platform_name=None):
    """A single example process/executable name in this OS's own convention -- "cs2.exe" on
    Windows, plain "cs2" on Linux/macOS (no extension, since neither OS uses one for a native
    binary) -- for Settings UI copy that used to hardcode a Windows-style example regardless of
    the OS actually running it."""
    platform_name = sys.platform if platform_name is None else platform_name
    return f"{base_name}.exe" if platform_name == "win32" else base_name


def executable_filetypes(platform_name=None):
    """The (label, pattern) filetypes tuple to hand a file-browse dialog when picking an
    executable -- an ("Executable", "*.exe") filter is meaningful on Windows but actively
    misleading on Linux/macOS, where native binaries have no extension at all and that filter
    would just hide every real match. Non-Windows gets an unfiltered "All files" list instead."""
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name == "win32":
        return (("Executable", "*.exe"), ("All files", "*.*"))
    return (("All files", "*.*"),)


def global_hotkeys_supported(platform_name=None):
    """False only for a Linux/Wayland session, which has no unprivileged API for a global hotkey
    at all (see CROSS_PLATFORM_PLAN.md §2.3) -- True on Windows and macOS unconditionally, and on
    Linux/X11. Meant for calling UI code (the custom-keybind Settings tab, the clip editor) to
    show a clear "not supported this session" message up front, rather than only finding out when
    run_custom_keybind_listener's own listener thread logs a warning and quietly does nothing."""
    backend = _select_backend(platform_name)
    is_wayland_session = getattr(backend, "is_wayland_session", None)
    return not (is_wayland_session and is_wayland_session())


def run_custom_keybind_listener(
    bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
    icon=None, notifications_config=None, status=None, platform_name=None,
):
    """Dispatches to this OS's own full custom-keybind listener implementation -- unlike most of
    this module's other functions, the entire mechanism (registration AND its event loop) differs
    enough per OS (RegisterHotKey/GetMessageW vs. XGrabKey/XNextEvent vs. a CGEventTap/CFRunLoop)
    that each backend owns its whole implementation rather than sharing one generic shape here;
    see CROSS_PLATFORM_PLAN.md Phase 3. fire_keybind/describe_keybind/notify are passed through
    rather than imported by the backend modules themselves, so backends never depend on
    autostart_script.py (the dependency only ever goes the other way -- see this module's own
    docstring)."""
    return _select_backend(platform_name).run_custom_keybind_listener(
        bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
        icon=icon, notifications_config=notifications_config, status=status,
    )


def embed_video_player(player, tk_widget, platform_name=None):
    """Embeds a python-vlc player's video output into tk_widget -- set_hwnd on Windows,
    set_xwindow on Linux/X11 (both take the plain numeric id tk_widget.winfo_id() already
    returns), set_nsobject on macOS (needs a real NSView object -- see
    platform_macos.embed_video_player for how that's bridged from Tk's own winfo_id())."""
    return _select_backend(platform_name).embed_video_player(player, tk_widget)


def resolve_editor_top_level_window(tk_window_id, platform_name=None):
    """Resolves whatever handle this OS's run_clip_editor_space_bar_listener backend needs to
    scope its check to the clip editor's own window, starting from the Tk root widget's own
    winfo_id() -- an ancestor walk up to the true top-level HWND on Windows, the identity
    function on Linux/macOS (see each backend's own resolve_editor_top_level_window)."""
    return _select_backend(platform_name).resolve_editor_top_level_window(tk_window_id)


def run_clip_editor_space_bar_listener(editor_window_handle, on_toggle, stop_event, platform_name=None):
    """Dispatches to this OS's own clip editor space-bar play/pause listener. editor_window_handle
    is whatever this OS's backend needs to scope the check to the editor window specifically -- an
    HWND on Windows, an X11 window id on Linux; accepted but unused on macOS, which scopes by
    frontmost process instead (see platform_macos.run_clip_editor_space_bar_listener's own
    docstring for why)."""
    return _select_backend(platform_name).run_clip_editor_space_bar_listener(editor_window_handle, on_toggle, stop_event)
