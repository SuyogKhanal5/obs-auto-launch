"""macOS backend for platform_common.py -- see CROSS_PLATFORM_PLAN.md for the full migration
schedule.

Minimum target: macOS 14.4 (Sonoma) -- see CROSS_PLATFORM_PLAN.md §2.6."""
import ctypes
import ctypes.util
import glob
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import tkinter

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


def find_steam_install_path():
    """Steam on macOS has no registry to ask -- its real, well-known install location under
    Application Support."""
    candidate = os.path.expanduser("~/Library/Application Support/Steam")
    return candidate if os.path.isdir(candidate) else None


def epic_manifest_dir():
    """Epic Games Launcher ships a native macOS client (unlike Linux -- see
    CROSS_PLATFORM_PLAN.md §2.5), writing its .item install manifests to the standard macOS
    per-app Application Support convention -- the same manifest FORMAT as Windows, just a
    different root. Not independently verified against a real Epic macOS install yet; confirm
    this exact path during Phase 2's own testing against real hardware/CI, per the plan's own
    note on this assumption."""
    return os.path.expanduser("~/Library/Application Support/Epic/EpicGamesLauncher/Data/Manifests")


def get_window_titles():
    """Maps each visible top-level window's owning PID to its titles via Quartz's
    CGWindowListCopyWindowInfo -- returns metadata (including title and owning PID) for every
    window currently on screen; unlike some other macOS window-introspection APIs, this
    particular call needs no special Accessibility/Screen-Recording permission grant from the
    user. Returns {} (never raises) if pyobjc isn't installed, or for any other reason this can't
    complete -- window-title-based game detection is one optional input to a bigger detection
    pipeline (see autostart_script.py's find_target_process), not something worth this app
    crashing over.

    pyobjc is imported lazily, inside this function, rather than at module level --
    deliberately, so this whole module stays importable (for the cross-OS backend-dispatch
    testing CROSS_PLATFORM_PLAN.md §3.2 describes) even in an environment that doesn't have
    pyobjc installed at all, which is every non-macOS CI runner given it's a macOS-only
    requirements.txt dependency (see that file's own sys_platform marker)."""
    try:
        import Quartz
    except ImportError:
        logging.warning(
            "pyobjc-framework-Quartz isn't installed -- window-title-based game detection rules "
            "won't match."
        )
        return {}

    try:
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    except Exception as exc:
        logging.warning("Could not enumerate windows for window-title detection: %s", exc)
        return {}

    titles = {}
    for window in window_list or []:
        title = window.get("kCGWindowName")
        pid = window.get("kCGWindowOwnerPID")
        if not title or pid is None:
            continue
        titles.setdefault(pid, []).append(title)
    return titles


# --- Global hotkeys (Quartz CGEventTap) ---
# macOS has no API for a true exclusive OS-level hotkey reservation the way Win32's RegisterHotKey
# or X11's XGrabKey have -- the standard approach (used by essentially every third-party macOS
# hotkey utility) is a session-level CGEventTap: a passive, non-consuming observer of every
# keystroke system-wide, matched against the configured bindings in the callback itself. This is
# actually the SAME shape Windows' WH_KEYBOARD_LL space-bar hook already uses in this app (see
# platform_windows.py's run_clip_editor_space_bar_listener) -- here it does double duty for BOTH
# custom keybinds and the space bar, unlike Windows/X11 which use two different mechanisms
# (RegisterHotKey/XGrabKey for custom keybinds, a separate passive hook for the space bar) because
# neither of those OSes' exclusive-grab APIs can be scoped to "only while my own window is
# focused" the way a plain window-local grab can.
#
# Requires the user to grant this app Accessibility permission (System Settings > Privacy &
# Security > Accessibility) -- CGEventTapCreate returns None without it. CI cannot grant real
# Accessibility permission (there's no interactive user session on macos-latest runners), so only
# the permission-NOT-granted path is exercised for real there; the "happy path" (a tap that
# actually receives events) needs real-hardware verification, flagged per
# CROSS_PLATFORM_PLAN.md Phase 3's own exit criteria.

# Carbon/HIToolbox's stable kVK_* virtual keycodes (from the public Events.h enum, unchanged
# across macOS versions) for the CUSTOM_KEYBIND_KEY_OPTIONS key space -- unlike X11 keysyms,
# these are physical ANSI-US key positions, not derived from the key's ASCII value, so (unlike
# platform_linux.keysym_for_key) this has to be a literal lookup table rather than an arithmetic
# offset. Not independently verified against real Mac hardware in this session -- flag any
# mismatch found during real-hardware testing per CROSS_PLATFORM_PLAN.md's open macOS unknowns.
_MACOS_KEYCODES = {
    "A": 0x00, "S": 0x01, "D": 0x02, "F": 0x03, "H": 0x04, "G": 0x05, "Z": 0x06, "X": 0x07,
    "C": 0x08, "V": 0x09, "B": 0x0B, "Q": 0x0C, "W": 0x0D, "E": 0x0E, "R": 0x0F, "Y": 0x10,
    "T": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19,
    "7": 0x1A, "8": 0x1C, "0": 0x1D, "O": 0x1F, "U": 0x20, "I": 0x22, "P": 0x23, "L": 0x25,
    "J": 0x26, "K": 0x28, "N": 0x2D, "M": 0x2E,
    "F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61, "F7": 0x62,
    "F8": 0x64, "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F,
}
MACOS_SPACE_KEYCODE = 0x31  # kVK_Space


def macos_keycode_for_key(key):
    """macOS counterpart of autostart_script.py's vk_code_for_key -- maps a
    CUSTOM_KEYBIND_KEY_OPTIONS entry to its ANSI-US virtual keycode."""
    return _MACOS_KEYCODES.get((key or "").strip().upper())


def accessibility_permission_granted():
    """True if this process currently holds Accessibility permission -- required before
    CGEventTapCreate will do anything at all (it returns None silently otherwise, which is why
    this is checked and reported explicitly up front rather than left to look like an unexplained
    dead hotkey)."""
    try:
        import Quartz
    except ImportError:
        return False
    try:
        return bool(Quartz.AXIsProcessTrusted())
    except Exception:
        logging.exception("Could not check macOS Accessibility permission status.")
        return False


def request_accessibility_permission():
    """Shows macOS's own native "<App> would like to control this computer using accessibility
    features" prompt, which deep-links to the right System Settings pane. Safe to call more than
    once -- macOS only actually shows the dialog the first time it's asked for a given app; after
    a decision has been recorded, granting it later requires the user to do so manually in System
    Settings (there's no way for this app to re-trigger the dialog itself)."""
    try:
        import Quartz
    except ImportError:
        return
    try:
        Quartz.AXIsProcessTrustedWithOptions({Quartz.kAXTrustedCheckOptionPrompt: True})
    except Exception:
        logging.exception("Could not show the macOS Accessibility permission prompt.")


def _macos_mod_flags_for(modifiers, Quartz):
    flags = {
        "ctrl": Quartz.kCGEventFlagMaskControl, "alt": Quartz.kCGEventFlagMaskAlternate,
        "shift": Quartz.kCGEventFlagMaskShift, "win": Quartz.kCGEventFlagMaskCommand,
    }
    mask = 0
    for m in modifiers or []:
        mask |= flags.get(m, 0)
    return mask


def _create_and_run_event_tap(Quartz, tap_callback, on_permission_denied, stop_event=None):
    """Shared by run_custom_keybind_listener and run_clip_editor_space_bar_listener below --
    both need the exact same CGEventTapCreate/CFRunLoop wiring, differing only in which keydowns
    the callback itself cares about. Blocks in CFRunLoopRun() for this thread's whole lifetime (or
    until stop_event fires, if given), the same "thread-affinity-bound, blocks until told to stop"
    shape the Windows/X11 backends' own message loops have. Returns without ever calling
    tap_callback if Accessibility permission isn't granted (after requesting it and calling
    on_permission_denied so the caller can surface that to the user its own way) or if tap
    creation fails for any other reason.

    CFRunLoopRun() has no built-in "stop after this event" polling hook the way Win32's
    GetMessageW or X11's pending_events() do -- stopping it early needs an explicit
    CFRunLoopStop(run_loop) call, which IS safe to make from another thread (that's its documented
    purpose). When stop_event is given, a small watcher daemon thread blocks on stop_event.wait()
    and stops this run loop the moment it fires, so a stop_event.set() from elsewhere (e.g. the
    clip editor window closing) actually tears this listener down instead of leaking a
    permanently-running tap thread until process exit."""
    if not accessibility_permission_granted():
        logging.warning(
            "This feature needs Accessibility permission, which hasn't been granted yet -- "
            "requesting it now. Grant it in System Settings > Privacy & Security > Accessibility, "
            "then restart the app."
        )
        request_accessibility_permission()
        on_permission_denied()
        return

    try:
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap, Quartz.kCGEventTapOptionListenOnly,
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown), tap_callback, None,
        )
        if not tap:
            logging.warning(
                "Could not create the macOS event tap -- Accessibility permission may not have "
                "taken effect yet; try restarting the app."
            )
            return
        run_loop_source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        run_loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(run_loop, run_loop_source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)

        if stop_event is not None:
            def watch_for_stop():
                stop_event.wait()
                Quartz.CFRunLoopStop(run_loop)

            threading.Thread(target=watch_for_stop, daemon=True).start()

        Quartz.CFRunLoopRun()
    except Exception:
        logging.exception("macOS event tap failed unexpectedly.")


def run_custom_keybind_listener(
    bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
    icon=None, notifications_config=None, status=None,
):
    """macOS counterpart of platform_windows.run_custom_keybind_listener -- see the module
    comment above for why this uses a passive CGEventTap rather than an exclusive-grab API.
    fire_keybind/describe_keybind/notify are passed in rather than imported from
    autostart_script.py to avoid backend modules depending on it (the dependency only ever goes
    the other way -- see CROSS_PLATFORM_PLAN.md §3)."""
    try:
        import Quartz
    except ImportError:
        logging.warning("Custom keybinds need pyobjc-framework-Quartz, which isn't installed -- see requirements.txt.")
        return

    enabled_bindings = [b for b in bindings if b.get("enabled", True)]
    lookup = {}
    for binding in enabled_bindings:
        keycode = macos_keycode_for_key(binding.get("key", ""))
        if keycode is None:
            logging.warning("Custom keybind has an invalid key %r; skipping.", binding.get("key"))
            continue
        mask = _macos_mod_flags_for(binding.get("modifiers"), Quartz)
        lookup[(keycode, mask)] = binding
        logging.info("Registered custom keybind %s -> %s", describe_keybind(binding), binding.get("action"))

    if not lookup:
        return

    relevant_flags_mask = (
        Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate
        | Quartz.kCGEventFlagMaskShift | Quartz.kCGEventFlagMaskCommand
    )

    def tap_callback(proxy, event_type, event, refcon):
        try:
            if event_type == Quartz.kCGEventKeyDown:
                keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                mask = Quartz.CGEventGetFlags(event) & relevant_flags_mask
                binding = lookup.get((keycode, mask))
                if binding:
                    threading.Thread(
                        target=fire_keybind,
                        args=(binding, get_client, get_manual_split_buffer_seconds, icon, notifications_config, status),
                        daemon=True,
                    ).start()
        except Exception:
            logging.exception("Custom keybind macOS event tap callback failed.")
        return event

    def on_permission_denied():
        notify(
            icon, notifications_config, "Accessibility permission needed",
            "Custom keybinds need Accessibility permission. Grant it in System Settings > Privacy "
            "& Security > Accessibility, then restart the app.",
        )

    _create_and_run_event_tap(Quartz, tap_callback, on_permission_denied)


# --- Autostart (LaunchAgents .plist) ---
_LAUNCH_AGENT_LABEL = "com.obsautorecorder.autostart"


def _launch_agent_plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{_LAUNCH_AGENT_LABEL}.plist")


def is_autostart_enabled():
    return os.path.isfile(_launch_agent_plist_path())


def enable_autostart(target_path, working_dir):
    """Writes a per-user LaunchAgent .plist and loads it via launchctl -- no elevation needed,
    unlike a system-level LaunchDaemon, since this only ever needs to run in the logged-in user's
    own session. RunAtLoad makes launchd start it once immediately at login (matching "start at
    login" as understood on Windows/Linux); KeepAlive is deliberately NOT set, since this should
    run once at login like a normal app, not be relaunched by launchd every time it exits."""
    import plistlib

    path = _launch_agent_plist_path()
    plist = {
        "Label": _LAUNCH_AGENT_LABEL,
        "ProgramArguments": [target_path],
        "WorkingDirectory": working_dir,
        "RunAtLoad": True,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
    except OSError as exc:
        logging.error("Failed to write LaunchAgent plist at %s: %s", path, exc)
        return False

    try:
        subprocess.run(["launchctl", "load", "-w", path], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.error("Failed to load the LaunchAgent via launchctl: %s", exc)
        return False
    logging.info("Created and loaded LaunchAgent at %s", path)
    return True


def disable_autostart():
    path = _launch_agent_plist_path()
    if not os.path.isfile(path):
        return True
    try:
        subprocess.run(["launchctl", "unload", path], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Not fatal on its own -- still try to remove the plist file below so a stale/already-
        # unloaded agent doesn't keep launchd re-running it at every future login regardless.
        logging.warning("launchctl unload failed (continuing to remove the plist anyway): %s", exc)
    try:
        os.remove(path)
        logging.info("Removed LaunchAgent plist.")
    except OSError as exc:
        logging.error("Failed to remove LaunchAgent plist: %s", exc)
        return False
    return True


# --- Optional dependency install (Homebrew) ---
# macOS counterpart of platform_windows.install_optional_dependency -- same silent, best-effort
# philosophy as Windows' winget, via Homebrew instead. CROSS_PLATFORM_PLAN.md Phase 7 / §3.3.
_BREW_PACKAGES = {
    "ffmpeg": {"formula": "ffmpeg"},
    "obs": {"cask": "obs"},
    "vlc": {"cask": "vlc"},
}


def has_package_manager():
    return shutil.which("brew") is not None


def install_optional_dependency(package, timeout=600):
    """Best-effort silent install via Homebrew. Returns (True, None) on success, (False, reason)
    otherwise -- never raises, so a missing/failed brew never blocks the rest of setup."""
    spec = _BREW_PACKAGES.get(package)
    if not spec:
        return False, f"No Homebrew package known for '{package}'."
    cmd = ["brew", "install"]
    cmd += ["--cask", spec["cask"]] if "cask" in spec else [spec["formula"]]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, None
    return False, (result.stdout or result.stderr or f"brew exited with code {result.returncode}")[-500:].strip()


def _find_libtk_dylib():
    """Locates the libtkX.Y.dylib actually bundled with THIS Python interpreter -- must match
    tkinter.TkVersion exactly, since a different build's exported symbols could differ or not
    exist at all. Mirrors the search order in python-vlc's own official tkvlc.py example
    (https://github.com/oaubert/python-vlc/blob/master/examples/tkvlc.py), which is the
    confirmed-correct source for this whole mechanism (see embed_video_player's own docstring for
    why that matters here). Returns None if it can't be found anywhere searched."""
    lib_name = f"libtk{tkinter.TkVersion}.dylib"
    candidates = [
        os.path.join(getattr(sys, "base_prefix", ""), "lib", lib_name),
        os.path.join(sys.prefix, "lib", lib_name),
    ]
    found = ctypes.util.find_library(lib_name)
    if found:
        candidates.append(found)
    for cellar_root in ("/opt/homebrew/Cellar", "/usr/local/Cellar", "/opt/local/Cellar"):
        candidates.extend(glob.glob(os.path.join(cellar_root, "tcl-tk", "*", "lib", lib_name)))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _resolve_root_ns_view(tk_widget):
    """Returns the raw NSView* pointer (a plain int, not a PyObjC object) of tk_widget's
    TOPLEVEL's own Cocoa content view, or None if it can't be resolved. See embed_video_player's
    docstring for why this goes through Tk's own TkMacOSXGetRootControl rather than winfo_id()
    directly."""
    libtk_path = _find_libtk_dylib()
    if not libtk_path:
        logging.warning("Could not find libtk%s.dylib for video embedding.", tkinter.TkVersion)
        return None
    try:
        lib = ctypes.cdll.LoadLibrary(libtk_path)
        get_root_view = lib.TkMacOSXGetRootControl
        get_root_view.restype = ctypes.c_void_p
        get_root_view.argtypes = (ctypes.c_void_p,)
        return get_root_view(tk_widget.winfo_id())
    except Exception as exc:
        logging.warning("Could not resolve a real NSView for video embedding: %s", exc)
        return None


def _tk_widget_rect_in_appkit_coords(tk_widget, toplevel):
    """tk_widget's on-screen rectangle, in the coordinate system of toplevel's own root content
    view -- Tk's y grows downward from the top-left corner; AppKit's non-flipped views (which is
    what a plain NSView, like the one created below, defaults to) grow upward from the
    bottom-left, so the y axis has to be flipped relative to the toplevel's current height."""
    x = tk_widget.winfo_rootx() - toplevel.winfo_rootx()
    y_tk = tk_widget.winfo_rooty() - toplevel.winfo_rooty()
    w = tk_widget.winfo_width()
    h = tk_widget.winfo_height()
    y_appkit = toplevel.winfo_height() - y_tk - h
    return x, y_appkit, w, h


def embed_video_player(player, tk_widget):
    """Embeds a python-vlc player's output into tk_widget -- unlike Windows/Linux (which both
    hand set_hwnd/set_xwindow the plain numeric id winfo_id() already returns), python-vlc's
    macOS set_nsobject() needs a real NSView pointer.

    Confirmed live (a real SIGSEGV, reproduced with a minimal script) that winfo_id()'s own
    return value is NOT that pointer at all -- it's an opaque Tk-internal "drawable" handle, and
    passing it to set_nsobject() directly (or wrapping it as a PyObjC object first, which is what
    this function used to do) crashes the instant anything tries to use it as a real Objective-C
    object, since it isn't one. The actual, correct conversion -- confirmed against python-vlc's
    own official tkvlc.py example, not guessed -- is Tk's own (non-public, but exported) C
    function TkMacOSXGetRootControl, called via ctypes against the real libtk dylib this
    interpreter loaded.

    That resolves a real NSView* -- but confirmed live (screenshots of a minimal repro, both
    with and without the fix below) that it is specifically the TOPLEVEL WINDOW's own single
    shared Cocoa content view, the same one no matter which descendant widget's winfo_id() is
    passed in -- Tk's Cocoa port draws every child widget of a toplevel into that one NSView
    itself, rather than giving each child widget its own. Handing that raw pointer straight to
    set_nsobject() (as this function used to) therefore doesn't confine VLC's video to
    tk_widget's own rectangle at all: VLC paints over the *entire* window, hiding every other Tk
    widget sharing it (confirmed live: a clip editor's transport controls and timeline visually
    vanished the instant a video actually started playing, once the prior SIGSEGV above was
    fixed and this became reachable for the first time). python-vlc's own official tkvlc.py
    example never hits this, because it never puts the video in the same window as any other
    widget in the first place -- it uses one Toplevel exclusively for video and a second,
    completely separate Toplevel for all the playback controls.

    Rather than restructure this app's clip editor into two windows to match, this creates a
    second, real NSView scoped to tk_widget's own on-screen rectangle and adds it as a subview
    of the toplevel's root view -- confirmed live (same repro, screenshotted again after this
    fix) that this keeps the video correctly confined to tk_widget's own bounds while every
    sibling widget in the same window stays visible. A <Configure> binding on tk_widget keeps
    the subview's frame in sync if the window is later resized (the clip editor's window is
    resizable, so tk_widget's own size/position aren't fixed for the widget's lifetime)."""
    toplevel = tk_widget.winfo_toplevel()
    root_view_ptr = _resolve_root_ns_view(tk_widget)
    if not root_view_ptr:
        logging.warning("Falling back to audio-only playback (no video preview) for this clip.")
        player.set_xwindow(tk_widget.winfo_id())
        return

    try:
        import objc
        import AppKit

        root_view = objc.objc_object(c_void_p=root_view_ptr)
        x, y, w, h = _tk_widget_rect_in_appkit_coords(tk_widget, toplevel)
        child_view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
        root_view.addSubview_(child_view)

        def _sync_frame(_event=None):
            try:
                x, y, w, h = _tk_widget_rect_in_appkit_coords(tk_widget, toplevel)
                child_view.setFrame_(AppKit.NSMakeRect(x, y, w, h))
            except Exception:
                logging.exception("Could not resync the video subview's frame after a resize.")

        tk_widget.bind("<Configure>", _sync_frame, add="+")
        player.set_nsobject(objc.pyobjc_id(child_view))
    except Exception as exc:
        logging.warning(
            "Could not create a properly-bounded video subview (%s); falling back to the "
            "whole window's view -- video will show, but may cover other controls.", exc,
        )
        player.set_nsobject(root_view_ptr)


def resolve_editor_top_level_window(tk_window_id):
    """Unused on macOS -- run_clip_editor_space_bar_listener below scopes by frontmost process,
    not a specific window handle (see that function's own docstring for why). Identity function
    only so the call site (autostart_script.py's start_space_bar_listener) doesn't need an
    OS-specific branch of its own."""
    return tk_window_id


def run_clip_editor_space_bar_listener(editor_window_handle, on_toggle, stop_event):
    """macOS counterpart of platform_windows.run_clip_editor_space_bar_listener -- same
    CGEventTap mechanism as run_custom_keybind_listener above (see the module comment for why
    macOS uses one passive tap for both, unlike Windows/X11's two separate mechanisms), filtered
    to the space bar and gated on this app's own process currently being frontmost.

    That frontmost-PROCESS check (rather than "is the clip editor's specific NSWindow key",
    Windows' precise per-HWND scoping) is a real, deliberate simplification: getting a specific
    NSWindow's identity from Tkinter's own separate event loop needs deeper AppKit bridging this
    phase doesn't attempt yet, so if this app has more than one top-level window open at once
    (e.g. the clip editor AND the main Settings window), space bar toggles play/pause while
    EITHER is frontmost, not only the clip editor specifically -- flagged as a known gap to
    tighten during real-hardware follow-up, not the precise Windows-equivalent behavior.

    editor_window_handle is accepted (for a uniform cross-backend call signature -- see
    platform_common.run_clip_editor_space_bar_listener) but unused: there's nothing to resolve
    here since the frontmost check works process-wide, not per-window, unlike Windows/X11's
    HWND-/X11-window-id-scoped equivalents."""
    del editor_window_handle
    try:
        import Quartz
    except ImportError:
        logging.warning(
            "Clip editor: space bar needs pyobjc-framework-Quartz, which isn't installed -- use "
            "the on-screen play/pause button instead."
        )
        return

    this_pid = os.getpid()

    def is_this_app_frontmost():
        try:
            frontmost = Quartz.NSWorkspace.sharedWorkspace().frontmostApplication()
            return frontmost is not None and frontmost.processIdentifier() == this_pid
        except Exception:
            return False

    def tap_callback(proxy, event_type, event, refcon):
        try:
            if event_type == Quartz.kCGEventKeyDown:
                keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                if keycode == MACOS_SPACE_KEYCODE and is_this_app_frontmost():
                    on_toggle()
        except Exception:
            logging.exception("Clip editor: space bar event tap callback failed.")
        return event

    def on_permission_denied():
        logging.warning(
            "Clip editor: space bar needs Accessibility permission -- use the on-screen "
            "play/pause button instead until it's granted."
        )

    _create_and_run_event_tap(Quartz, tap_callback, on_permission_denied, stop_event=stop_event)


# Confirmed empirically against a real OBS 30.2.3 on macOS 15.5 (Apple Silicon), obs-websocket
# 5.5.2 -- CROSS_PLATFORM_PLAN.md §6.1. GetInputKindList surfaced "sck_audio_capture" (built on
# ScreenCaptureKit's per-app audio taps, the macOS 14.4+ capability this app's macOS floor was
# raised for); GetInputDefaultSettings gave {"application": "", "type": 0}. Its "type" property
# is an enum with exactly two confirmed-safe values -- querying
# GetInputPropertiesListPropertyItems(name, "type") on a real created input returned:
#   0 -> "Desktop Audio Capture"
#   1 -> "Application Audio Capture"
# Only with type=1 does the "application" property's own live item list populate (confirmed with
# real running apps, e.g. {"itemName": "Discord", "itemValue": "com.hnc.Discord"} -- a bundle
# identifier, not a process/executable name); at type=0 it's an empty list (desktop audio needs
# no target). A third value (2) was tried once to see whether a third meaningful mode existed --
# OBS's whole process disappeared instantly, no crash report, no error surfaced over the
# websocket first. Never pass "type" anything but 0 or 1.
PROCESS_AUDIO_CAPTURE_KIND = "sck_audio_capture"
SCK_AUDIO_CAPTURE_TYPE_APPLICATION = 1


def resolve_bundle_identifier(exe_path):
    """Walks up from a running process's own executable path (e.g.
    /Applications/Balatro.app/Contents/MacOS/Balatro, the "exe" psutil reports) to find the
    owning .app bundle's Info.plist and reads CFBundleIdentifier out of it -- the value
    sck_audio_capture's "application" setting actually needs (confirmed live, see
    PROCESS_AUDIO_CAPTURE_KIND above), not the process/executable name itself. Returns None if
    exe_path isn't inside a .app bundle at all (a bare command-line binary, for instance -- rare
    for an actual game, but not impossible), or if the plist can't be read/parsed for any reason
    (a damaged bundle, unreadable permissions, etc.) -- this is a best-effort resolution, not
    something callers should ever treat as guaranteed to succeed."""
    if not exe_path:
        return None
    path = os.path.normpath(exe_path)
    while True:
        parent = os.path.dirname(path)
        if path.endswith(".app"):
            plist_path = os.path.join(path, "Contents", "Info.plist")
            try:
                with open(plist_path, "rb") as f:
                    plist = plistlib.load(f)
                return plist.get("CFBundleIdentifier")
            except Exception:
                return None
        if parent == path:
            return None
        path = parent


def process_audio_capture_settings(process_name, exe_path):
    """Builds sck_audio_capture's settings dict for capturing one application's own audio -- the
    macOS counterpart to Windows's wasapi_process_output_capture "window" field. process_name is
    accepted for signature parity with the Windows backend but unused: macOS's "application"
    field needs a bundle identifier, which only exe_path can resolve (see
    resolve_bundle_identifier). Returns None if that resolution fails (the process isn't
    currently running -- exe_path is None -- or its executable isn't inside a normal .app bundle
    at all), so callers can leave whatever's already configured alone rather than writing a
    setting guaranteed not to match anything real."""
    bundle_id = resolve_bundle_identifier(exe_path)
    if not bundle_id:
        return None
    return {"type": SCK_AUDIO_CAPTURE_TYPE_APPLICATION, "application": bundle_id}


def hide_dock_icon():
    """Hides this process's Dock icon and Cmd-Tab/App-Switcher entry, making it behave as a
    proper macOS menu-bar-only "accessory" app instead of showing a generic, non-functional
    Python/rocket Dock icon alongside the real tray icon in the menu bar. Confirmed live: without
    this, the app (whether run from source or from a --windowed PyInstaller build with no custom
    Info.plist LSUIElement key) shows an unusable Dock icon next to its real, working menu-bar
    icon -- clicking it does nothing meaningful, since this app has no main window of its own.

    Must be called AFTER Tk has already initialized its own Cocoa integration (see main()'s own
    throwaway-Tk() priming step and its docstring) -- this also touches the shared NSApplication,
    and doing so before Tk gets to register itself reproduces the exact startup crash that
    priming step exists to avoid. Best-effort: logs and continues on any failure (pyobjc missing,
    or anything else) since running with a Dock icon present is a cosmetic annoyance, not
    something worth crashing over."""
    try:
        import AppKit
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        )
    except Exception as exc:
        logging.warning("Could not hide the Dock icon: %s", exc)


def run_on_main_thread(func):
    """Schedules func to run (asynchronously) on the real process main thread, from any thread.

    Confirmed live: this app's game-watcher loop runs on its own background thread (started from
    pystray's setup callback, which pystray itself documents as running "in a separate thread"),
    and it directly sets the tray icon's image/title (icon.icon = .../icon.title = ...) whenever
    recording state changes. Those property setters end up calling pystray's macOS backend's
    _update_icon()/_update_title(), which touch AppKit's NSStatusItem/NSImage directly -- and
    AppKit forbids that from any thread but the main one. A real crash report from this exact
    sequence (detecting a watched game, then failing to reach OBS, then updating the tray icon to
    show the error) showed a SIGABRT a few seconds later, deep inside Tk's own Cocoa event
    dispatch (PythonCmd -> PyEval_RestoreThread -> fatal_error) -- the same class of "any AppKit
    misuse anywhere in the process can corrupt Tk's own fragile macOS backend" finding as the
    tray-menu-crash bug (see CROSS_PLATFORM_PLAN.md).

    PyObjCTools.AppHelper.callAfter is PyObjC's own documented mechanism for handing a callable to
    the main thread's run loop from any other thread, without going through Tkinter at all (Tk's
    own .after() is itself a Tcl/Tk call, and making *that* safe to call cross-thread is exactly
    the kind of misuse this function exists to avoid). If already on the main thread, func() just
    runs immediately -- callAfter would still work, but there's no reason to round-trip through
    the run loop when nothing needs marshaling."""
    if threading.current_thread() is threading.main_thread():
        func()
        return
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(func)
    except Exception:
        logging.exception("Could not marshal a callback onto the macOS main thread.")
