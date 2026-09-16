"""Linux backend for platform_common.py -- see CROSS_PLATFORM_PLAN.md for the full migration
schedule.

Global hotkeys, window-title detection, and the clip editor's space-bar-while-video-focused
behavior are X11-only for v1 (see CROSS_PLATFORM_PLAN.md §2.3) -- Wayland sessions
(`os.environ.get("XDG_SESSION_TYPE") == "wayland"`) get a clear "not supported" result from the
relevant functions once they land, not a silent no-op or a crash."""
import logging
import os
import re
import shutil
import threading

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


# --- Global hotkeys (X11 XGrabKey) ---
# The X11 counterpart of Windows' RegisterHotKey/WM_HOTKEY listener
# (autostart_script.py's original run_custom_keybind_listener, now platform_windows.py's) --
# same overall shape (register everything up front, then block in an event loop on one dedicated
# thread for the listener's whole lifetime), but XGrabKey/XNextEvent instead of
# RegisterHotKey/GetMessageW. Wayland gets a clear logged "not supported" result (see
# is_wayland_session) rather than a silent no-op, per CROSS_PLATFORM_PLAN.md §2.3/Phase 3.

_XK_F1 = 0xFFBE  # X11 keysymdef.h: XK_F1..XK_F35 are contiguous from here.
_XK_CAPS_LOCK = 0xFFE5
_XK_NUM_LOCK = 0xFF7F
_XK_SCROLL_LOCK = 0xFF14


def keysym_for_key(key):
    """X11 counterpart of autostart_script.py's vk_code_for_key -- maps a
    CUSTOM_KEYBIND_KEY_OPTIONS entry to its X11 keysym (stable values from X11's own
    keysymdef.h). Not yet a keycode: XGrabKey grabs by keycode, which is specific to this
    display's current keyboard mapping and only resolvable once a real Display connection is
    open (see register_global_hotkey)."""
    key = (key or "").strip().upper()
    if len(key) == 1 and key.isdigit():
        return 0x30 + int(key)  # XK_0..XK_9 share ASCII '0'-'9'
    if len(key) == 1 and key.isalpha():
        return 0x41 + (ord(key) - ord("A"))  # XK_A..XK_Z share ASCII 'A'-'Z'
    match = re.fullmatch(r"F(\d{1,2})", key)
    if match and 1 <= int(match.group(1)) <= 12:
        return _XK_F1 + (int(match.group(1)) - 1)
    return None


def _x11_mod_mask_for(modifiers, X):
    masks = {"ctrl": X.ControlMask, "alt": X.Mod1Mask, "shift": X.ShiftMask, "win": X.Mod4Mask}
    mask = 0
    for m in modifiers or []:
        mask |= masks.get(m, 0)
    return mask


def _ignorable_modifier_bits(display):
    """Which of the 8 X11 modifier-mapping slots NumLock/CapsLock/ScrollLock actually landed on
    for this keyboard -- X11 doesn't fix these to specific bits the way it fixes Shift/Control,
    they're whatever XModifierMapping says (in practice almost always Mod2 for NumLock and Lock
    for CapsLock, but reading it rather than assuming keeps this correct on an unusual mapping
    too). Needed because a key grabbed with an exact modifier mask simply never fires while any
    of these happen to be toggled on -- a well-known X11 quirk affecting every global-hotkey
    implementation on the platform, not specific to this app."""
    bits = []
    modifier_mapping = display.get_modifier_mapping()
    for keysym in (_XK_CAPS_LOCK, _XK_NUM_LOCK, _XK_SCROLL_LOCK):
        keycode = display.keysym_to_keycode(keysym)
        if not keycode:
            continue
        for index, keycodes in enumerate(modifier_mapping):
            if keycode in keycodes:
                bits.append(1 << index)
                break
    return bits


def _ignorable_modifier_combinations(display):
    """Every OR-combination of _ignorable_modifier_bits (including 0, the base case with none of
    them active) -- register_global_hotkey grabs the requested combination once per entry here so
    the hotkey still fires no matter which of NumLock/CapsLock/ScrollLock happen to be on."""
    combos = {0}
    for bit in _ignorable_modifier_bits(display):
        combos |= {c | bit for c in combos}
    return sorted(combos)


def register_global_hotkey(display, root, modifiers, key):
    """Grabs a global hotkey via XGrabKey on the given root window -- fires regardless of which
    window has input focus, the X11 counterpart of Windows' RegisterHotKey. Returns a
    (keycode, base_mask) handle to pass to unregister_global_hotkey later, or None if `key` isn't
    a recognized CUSTOM_KEYBIND_KEY_OPTIONS entry. Unlike RegisterHotKey, XGrabKey never reports
    "this combination is already grabbed by another app" synchronously -- it just silently never
    delivers the event -- so there's no equivalent of the Windows backend's retry-and-notify loop
    for an already-in-use combination; a real per-grab conflict check would need a separate
    synchronous round-trip this doesn't attempt (flagged as a known gap, not fixed here)."""
    from Xlib import X

    keysym = keysym_for_key(key)
    if keysym is None:
        return None
    keycode = display.keysym_to_keycode(keysym)
    if not keycode:
        return None
    base_mask = _x11_mod_mask_for(modifiers, X)
    for combo in _ignorable_modifier_combinations(display):
        root.grab_key(keycode, base_mask | combo, True, X.GrabModeAsync, X.GrabModeAsync)
    return (keycode, base_mask)


def unregister_global_hotkey(display, root, handle):
    """Releases a handle register_global_hotkey returned -- every ignorable-modifier combination
    it originally grabbed, mirroring that call exactly. Swallows per-combination ungrab errors
    (best-effort cleanup on shutdown, not worth aborting the rest over one failed release)."""
    if not handle:
        return
    keycode, base_mask = handle
    for combo in _ignorable_modifier_combinations(display):
        try:
            root.ungrab_key(keycode, base_mask | combo)
        except Exception:
            pass


def run_custom_keybind_listener(
    bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
    icon=None, notifications_config=None, status=None,
):
    """X11 counterpart of platform_windows.run_custom_keybind_listener -- registers every enabled
    binding via XGrabKey, then blocks in an XNextEvent loop on this same thread for the
    listener's whole lifetime (X11 grabs, like Win32 hotkey registrations, have thread affinity:
    both the grab and the events it produces belong to the connection/thread that made it).
    fire_keybind/describe_keybind/notify are passed in rather than imported from
    autostart_script.py to avoid backend modules depending on it (the dependency only ever goes
    the other way -- see CROSS_PLATFORM_PLAN.md §3)."""
    if is_wayland_session():
        logging.warning(
            "Custom keybinds need a real global hotkey, which isn't available under Wayland -- "
            "switch to an X11 session to use this feature."
        )
        return

    try:
        import Xlib.X as X
        import Xlib.display
        import Xlib.error
    except ImportError:
        logging.warning("Custom keybinds need python-xlib, which isn't installed -- see requirements.txt.")
        return

    try:
        display = Xlib.display.Display()
    except Xlib.error.DisplayError as exc:
        logging.warning("Custom keybinds: could not open the X display: %s", exc)
        return

    try:
        root = display.screen().root
        root.change_attributes(event_mask=X.KeyPressMask)

        registered = {}
        for binding in bindings:
            if not binding.get("enabled", True):
                continue
            handle = register_global_hotkey(display, root, binding.get("modifiers"), binding.get("key", ""))
            if handle is None:
                logging.warning("Custom keybind has an invalid key %r; skipping.", binding.get("key"))
                continue
            registered[handle] = binding
            logging.info("Registered custom keybind %s -> %s", describe_keybind(binding), binding.get("action"))
        display.flush()

        if not registered:
            return

        ignorable_mask = 0
        for bit in _ignorable_modifier_bits(display):
            ignorable_mask |= bit

        try:
            while True:
                event = display.next_event()
                if event.type != X.KeyPress:
                    continue
                binding = registered.get((event.detail, event.state & ~ignorable_mask))
                if binding:
                    threading.Thread(
                        target=fire_keybind,
                        args=(binding, get_client, get_manual_split_buffer_seconds, icon, notifications_config, status),
                        daemon=True,
                    ).start()
        finally:
            for handle in registered:
                unregister_global_hotkey(display, root, handle)
    except Exception:
        # Mirrors run_clip_editor_space_bar_listener's own top-level guard (see that function's
        # comment on platform_windows.py) -- an uncaught exception on this dedicated daemon
        # thread would otherwise disappear with no trace in a packaged (--noconsole) build,
        # looking exactly like "custom keybinds just don't work" rather than a visible failure.
        logging.exception("Custom keybind listener failed unexpectedly.")
    finally:
        display.close()


# --- Clip editor space bar play/pause (X11, window-scoped XGrabKey) ---
# Unlike the global (root-window) grabs used for custom keybinds above, this grabs the space key
# on the clip editor's OWN top-level window specifically -- X11 only delivers a key grabbed on a
# non-root window W to W while W (or one of its descendants) currently holds the keyboard input
# focus, so this fires while the clip editor is focused, INCLUDING while the embedded VLC video
# surface (a foreign child window outside Tk's own control, once Phase 4 embeds it via
# set_xwindow()) has that focus, the same "space toggles play/pause even though a raw native
# child window has real input focus" behavior Windows' WH_KEYBOARD_LL low-level hook exists for --
# achieved here as a normal, non-exclusive, window-scoped grab instead of a passive system-wide
# observer, so (unlike run_custom_keybind_listener's root-window grabs) it never affects any
# other application's windows, and needs no separate "is my window focused" check the way
# Windows' GetForegroundWindow()-based is_space_bar_toggle_event does -- X11's own grab-delivery
# rule already scopes it that way.


def resolve_editor_top_level_window(tk_window_id):
    """Tk creates its root widget's window directly as a real, window-manager-visible top-level
    X window as soon as it exists -- unlike Windows, where a Tk widget's own HWND isn't
    necessarily the top-level one (see platform_windows.resolve_editor_top_level_window) -- so
    there's no ancestor walk needed here at all, just the identity function."""
    return tk_window_id


def run_clip_editor_space_bar_listener(editor_window_id, on_toggle, stop_event):
    """X11 counterpart of platform_windows.run_clip_editor_space_bar_listener -- see the module
    comment above for why this needs no foreground-window check of its own. editor_window_id is
    the clip editor's own top-level Tk window's winfo_id() (an X11 window is already a real,
    window-manager-visible top-level window as soon as Tk creates it there, unlike Windows where
    a Tk widget's own HWND isn't necessarily the top-level one -- see
    autostart_script.py's start_space_bar_listener for the per-OS resolution logic)."""
    try:
        import Xlib.X as X
        import Xlib.display
        import Xlib.error
    except ImportError:
        logging.warning(
            "Clip editor: space bar needs python-xlib, which isn't installed -- use the "
            "on-screen play/pause button instead."
        )
        return

    try:
        display = Xlib.display.Display()
    except Xlib.error.DisplayError as exc:
        logging.warning("Clip editor: could not open the X display for the space bar hook: %s", exc)
        return

    try:
        editor_window = display.create_resource_object("window", editor_window_id)
        keycode = display.keysym_to_keycode(0x0020)  # XK_space
        if not keycode:
            logging.warning("Clip editor: could not resolve the space bar's keycode on this keyboard.")
            return
        for combo in _ignorable_modifier_combinations(display):
            editor_window.grab_key(keycode, combo, True, X.GrabModeAsync, X.GrabModeAsync)
        display.flush()
        logging.info("Clip editor: space bar play/pause hook installed for window %s.", editor_window_id)

        try:
            while not stop_event.is_set():
                if display.pending_events() == 0:
                    stop_event.wait(0.05)
                    continue
                event = display.next_event()
                if event.type == X.KeyPress and event.detail == keycode:
                    on_toggle()
        finally:
            for combo in _ignorable_modifier_combinations(display):
                try:
                    editor_window.ungrab_key(keycode, combo)
                except Exception:
                    pass
    except Exception:
        logging.exception(
            "Clip editor: space bar play/pause hook failed unexpectedly -- use the on-screen "
            "play/pause button instead."
        )
    finally:
        display.close()
