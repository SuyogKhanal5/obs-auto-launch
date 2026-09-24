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


def is_autostart_enabled(platform_name=None):
    """True if this app is currently configured to launch automatically at login -- a Startup-
    folder .lnk on Windows, an XDG autostart .desktop file on Linux, a LaunchAgents .plist on
    macOS. All 3 mechanisms are just presence checks (there's only ever one such entry per app),
    so this needs no target path the way enable_autostart below does."""
    return _select_backend(platform_name).is_autostart_enabled()


def enable_autostart(target_path=None, working_dir=None, platform_name=None):
    """Configures this app to launch automatically at login. target_path/working_dir default to
    sys.executable and its own directory (this app enabling autostart for itself, e.g. from its
    own Settings toggle) -- pass them explicitly when a DIFFERENT process needs to enable it for
    an app it just installed elsewhere (e.g. installer.py, a separate running process from the
    app it just wrote to disk). Returns False (logged) rather than raising on any failure --
    autostart has always been opt-in, best-effort, never something this app hard-requires."""
    target_path = target_path or sys.executable
    working_dir = working_dir or os.path.dirname(target_path)
    return _select_backend(platform_name).enable_autostart(target_path, working_dir)


def disable_autostart(platform_name=None):
    """Removes whatever enable_autostart set up, if anything -- a no-op (returns True) if
    autostart wasn't enabled to begin with, on every backend."""
    return _select_backend(platform_name).disable_autostart()


def has_package_manager(platform_name=None):
    """True if this OS's silent-install mechanism (winget on Windows, Homebrew on macOS) is
    available to run at all. Meaningless on Linux, which never does a silent install -- see
    build_linux_install_command/run_linux_install_command instead."""
    return _select_backend(platform_name).has_package_manager()


def install_optional_dependency(package, timeout=600, platform_name=None):
    """Best-effort, silent install of an optional dependency ("ffmpeg", "obs", or "vlc") --
    winget on Windows, Homebrew on macOS. Returns (True, None) on success, (False, reason)
    otherwise; never raises, so a failed/missing package manager never blocks setup. Not
    meaningful on Linux, which never does a silent background install by design (see
    CROSS_PLATFORM_PLAN.md §2.7) -- build_linux_install_command/run_linux_install_command are the
    Linux equivalent, requiring an explicit user click before anything runs."""
    return _select_backend(platform_name).install_optional_dependency(package, timeout=timeout)


def build_linux_install_command(package, platform_name=None):
    """The exact command to install `package` ("ffmpeg" or "obs") on this Linux system, for a
    caller to show verbatim before the user decides whether to run it -- see
    platform_linux.build_linux_install_command's own docstring for the full rationale. Linux-only;
    not defined on the Windows/macOS backends, which use install_optional_dependency instead."""
    return _select_backend(platform_name).build_linux_install_command(package)


def run_linux_install_command(command, timeout=600, platform_name=None):
    """Executes a command build_linux_install_command returned, via pkexec (or directly, for a
    --user-scoped Flatpak install that needs no elevation) -- see
    platform_linux.run_linux_install_command's own docstring. Linux-only."""
    return _select_backend(platform_name).run_linux_install_command(command, timeout=timeout)


def obs_config_dir(platform_name=None):
    """OBS's own per-user config root -- same formula on every OS regardless of where THIS app
    (or its installer) itself lives, since it's just where OBS always stores its own config/
    logs/crash-sentinel files/obs-websocket plugin settings. A plain path-selection helper, not
    dispatched per-backend, since no OS API call is involved -- callers include
    autostart_script.py's clear_obs_crash_sentinel() and installer.py's
    get_obs_websocket_config_path(), which independently need the exact same directory. Returns
    None only for the (unusual) case of APPDATA being unset on Windows."""
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name == "win32":
        appdata = os.environ.get("APPDATA")
        return os.path.join(appdata, "obs-studio") if appdata else None
    if platform_name == "darwin":
        return os.path.expanduser("~/Library/Application Support/obs-studio")
    return os.path.expanduser("~/.config/obs-studio")


def find_gog_installed_games(exclude_keywords, platform_name=None):
    """GOG Galaxy has never shipped a native Linux/macOS client (CROSS_PLATFORM_PLAN.md §2.5) --
    only platform_windows.py implements this for real. Linux/macOS-only in practice: the caller
    (autostart_script.py's own get_gog_installed_games) already checks sys.platform == "win32"
    before ever reaching this, so this is never actually invoked on the wrong OS."""
    return _select_backend(platform_name).find_gog_installed_games(exclude_keywords)


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


def process_audio_capture_kind(platform_name=None):
    """Returns the OBS input kind used for per-process/per-application audio capture on this OS
    (e.g. isolating one game's audio onto its own recording track), or None if this OS has no
    confirmed capability of that kind at all -- Linux is a confirmed negative finding, not an
    unimplemented one (CROSS_PLATFORM_PLAN.md §6.1: a real CI spike against PulseAudio found no
    per-app capture kind exists there). Never guessed from documentation -- every non-None value
    here was confirmed empirically against a real, running OBS instance."""
    return _select_backend(platform_name).PROCESS_AUDIO_CAPTURE_KIND


def process_audio_capture_settings(process_name, exe_path, platform_name=None):
    """Builds the OBS input-settings dict that points process_audio_capture_kind()'s input at
    process_name/exe_path on this OS -- a Windows "window" field formatted "::process_name" (an
    exe-only match, no title/class needed), a macOS sck_audio_capture "application" field (a
    bundle identifier resolved from exe_path -- see platform_macos.resolve_bundle_identifier).
    Returns None if this OS has no such kind at all (process_audio_capture_kind() is None), or if
    process_name/exe_path can't be resolved to whatever that kind's target identifier actually
    needs (e.g. exe_path is None because the process isn't running right now, or -- macOS only --
    it isn't inside a normal .app bundle at all)."""
    return _select_backend(platform_name).process_audio_capture_settings(process_name, exe_path)


def gui_requires_main_thread(platform_name=None):
    """True if this OS's GUI toolkit must run only on the real process main thread -- AppKit's
    hard requirement on macOS. Confirmed live: this app's Tk GUI (the overlay, and everything
    built as a Toplevel of its one tk.Tk() -- Settings, the clip editor) running on a background
    thread while pystray's tray icon pumps the real main thread's Cocoa run loop (this app's
    default architecture, fine on Windows/Linux) crashes the whole process outright the moment
    anything actually triggers that run loop to spin -- an uncaught NSInvalidArgumentException
    deep inside Tk's own color-handling code (GetRGBA/TkpGetColor), not a graceful failure or a
    Python-catchable exception. False on Windows/Linux, where no such constraint exists."""
    platform_name = sys.platform if platform_name is None else platform_name
    return platform_name == "darwin"


def hide_dock_icon(platform_name=None):
    """Hides this process's Dock icon / Cmd-Tab entry, on whichever OS has one to hide at all --
    macOS only (see platform_macos.hide_dock_icon). No-op everywhere else: a pystray tray icon on
    Windows/Linux never puts an equivalent unwanted taskbar/dock entry alongside itself in the
    first place, unlike a plain macOS process, which shows a generic, non-functional Python/
    rocket Dock icon next to the real menu-bar tray icon unless told not to."""
    return _select_backend(platform_name).hide_dock_icon()


def run_on_main_thread(func, platform_name=None):
    """Schedules func to run on the real process main thread and returns immediately, without
    waiting for it to actually run there -- only macOS needs this at all (see
    platform_macos.run_on_main_thread): confirmed live that the game-watcher background thread
    setting the tray icon's image/title directly (icon.icon = .../icon.title = ..., both touching
    AppKit's NSStatusItem under the hood) crashed the whole process with a SIGABRT deep inside
    Tk's own Cocoa event dispatch a few seconds later -- a background thread is never allowed to
    touch AppKit on macOS. Windows/Linux have no such constraint, so func() just runs immediately,
    synchronously, on whichever thread called this."""
    return _select_backend(platform_name).run_on_main_thread(func)


def open_path(path, platform_name=None):
    """Opens a file or folder with the OS's own default handler (Explorer on Windows, Finder on
    macOS, whichever app xdg-open resolves to on Linux) -- the cross-platform equivalent of
    os.startfile(), which only exists on Windows at all. Confirmed live: the tray menu's "Open Log
    File" and "Open Recordings Folder" items called os.startfile() directly and had never been
    exercised on macOS -- clicking either there raised AttributeError (no such function outside
    Windows), caught and logged by the tray menu's own error handling, so from the user's side it
    looked exactly like the menu item silently doing nothing."""
    return _select_backend(platform_name).open_path(path)
