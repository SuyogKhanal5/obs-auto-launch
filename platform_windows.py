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
import ctypes
import glob
import logging
import os
import re
import shutil
import sys
import threading
import time

if sys.platform == "win32":
    import winreg
    from ctypes import wintypes

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


def find_steam_install_path():
    """Moved unchanged from autostart_script.py's old get_steam_install_path(): Steam writes its
    real install root to the registry on every Windows install."""
    for hive, subkey in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value = winreg.QueryValueEx(key, "SteamPath")[0]
                return os.path.normpath(value)
        except OSError:
            continue
    return None


def epic_manifest_dir():
    """Moved unchanged from autostart_script.py's old get_epic_manifest_dir()."""
    root = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return os.path.join(root, "Epic", "EpicGamesLauncher", "Data", "Manifests")


def get_window_titles():
    """Moved unchanged from autostart_script.py's old get_window_titles(): maps each visible
    top-level window's owning PID to its titles via a raw EnumWindows callback."""
    titles = {}
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, lparam):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return 1
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return 1
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        titles.setdefault(pid.value, []).append(buf.value)
        return 1

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(callback), 0)
    return titles


# --- Global hotkeys (Win32 RegisterHotKey) ---
# Moved wholesale from autostart_script.py's old run_custom_keybind_listener (see
# CROSS_PLATFORM_PLAN.md Phase 3) -- fire_keybind/describe_keybind/notify are now passed in as
# parameters rather than referencing autostart_script.py's module-level names directly, so this
# module never depends on it (the dependency only ever goes the other way -- see
# platform_common.py's own docstring). MOD_NOREPEAT (added to every registration below) keeps a
# held-down key from re-firing the action on every auto-repeat tick, which would otherwise queue
# up a burst of duplicate split/mute/etc. calls.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
_CUSTOM_KEYBIND_MODIFIER_FLAGS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}


def vk_code_for_key(key):
    """Maps a CUSTOM_KEYBIND_KEY_OPTIONS entry to its Win32 virtual-key code. Letters and digits
    share their ASCII codes with Windows' VK_0-VK_9/VK_A-VK_Z, so those need no lookup table."""
    key = (key or "").strip().upper()
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        return ord(key)
    match = re.fullmatch(r"F(\d{1,2})", key)
    if match and 1 <= int(match.group(1)) <= 12:
        return 0x6F + int(match.group(1))  # VK_F1 is 0x70
    return None


def mod_flags_for(modifiers):
    flags = 0
    for m in modifiers or []:
        flags |= _CUSTOM_KEYBIND_MODIFIER_FLAGS.get(m, 0)
    return flags


def run_custom_keybind_listener(
    bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
    icon=None, notifications_config=None, status=None,
):
    """Runs for its whole lifetime on one dedicated daemon thread: RegisterHotKey (and the
    WM_HOTKEY messages it produces) has thread affinity, so every binding must be registered from
    -- and received on -- the same thread. Passing hwnd=None posts WM_HOTKEY straight to this
    thread's message queue instead of routing through a window, so no hidden window is needed."""
    user32 = ctypes.windll.user32
    registered = []
    for index, binding in enumerate(bindings):
        if not binding.get("enabled", True):
            continue
        vk = vk_code_for_key(binding.get("key", ""))
        if vk is None:
            logging.warning("Custom keybind has an invalid key %r; skipping.", binding.get("key"))
            continue
        mods = mod_flags_for(binding.get("modifiers")) | MOD_NOREPEAT
        hotkey_id = index + 1
        # A self-restart (e.g. after a Settings save) doesn't post WM_QUIT to this thread, so
        # the previous process's hotkey registrations are only released by Windows noticing that
        # process has actually terminated -- a brief, variable-length teardown window that can
        # still be in progress by the time the new process gets here. Without retrying, losing
        # that race silently and permanently disables the keybind for the rest of this session
        # (confirmed live: an "Add Marker" keybind that lost this race never worked again until
        # the next restart, with no further sign of it beyond one cold WARNING log line).
        registered_ok = False
        for _attempt in range(5):
            if user32.RegisterHotKey(None, hotkey_id, mods, vk):
                registered_ok = True
                break
            time.sleep(0.3)
        if registered_ok:
            registered.append((hotkey_id, binding))
            logging.info("Registered custom keybind %s -> %s", describe_keybind(binding), binding.get("action"))
        else:
            logging.warning(
                "Could not register custom keybind %s (it may already be in use by another app).",
                describe_keybind(binding),
            )
            notify(
                icon, notifications_config, "Keybind not registered",
                f"{describe_keybind(binding)} could not be registered -- it may already be in use by "
                "another app. This keybind won't work until the app is restarted again.",
            )

    if not registered:
        return

    by_id = dict(registered)
    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                binding = by_id.get(msg.wParam)
                if binding:
                    threading.Thread(
                        target=fire_keybind,
                        args=(binding, get_client, get_manual_split_buffer_seconds, icon, notifications_config, status),
                        daemon=True,
                    ).start()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        for hotkey_id, _ in registered:
            user32.UnregisterHotKey(None, hotkey_id)


# --- Clip editor space bar play/pause (global low-level keyboard hook) ---
# Space bar play/pause needs to work even when the embedded VLC video surface (a raw native HWND
# via player.set_hwnd() -- see that call site -- completely outside Tk's own widget/window
# hierarchy) holds real Win32 keyboard focus, since Windows delivers WM_KEYDOWN straight to
# whichever HWND has focus, with no bubbling up to a Tk-bound parent the way mouse events get
# hit-tested. Three earlier attempts at reclaiming Tk-level focus (see reclaim_focus_on_click/
# reclaim_focus_after_play in the clip editor itself, which still exist for other reasons) never
# covered every case a real user hits -- a click landing ON the video surface itself, for one,
# never even reaches Tk as a <Button-1> event to react to, since Tk's event system only ever sees
# clicks on Tk-owned widgets in the first place. A fourth attempt using a low-level (WH_KEYBOARD_LL)
# hook -- which intercepts a keystroke at the OS input-queue level before Windows decides which
# HWND to deliver it to, sidestepping the focus question entirely -- crashed the app instead. Two
# likely causes, both specifically addressed below: (1) nothing kept the ctypes callback trampoline
# referenced for the hook's whole lifetime, so Python's GC was free to collect it while Windows
# still held a raw pointer to call on the very next keystroke; (2) the callback called directly
# into Tk/VLC instead of marshaling that through something thread-safe like root.after(...).
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
VK_SPACE = 0x20
GA_ROOT = 2


def resolve_editor_top_level_window(tk_window_id):
    """Walks from a Tk widget's own HWND up to its true top-level ancestor HWND -- a Tk widget's
    own winfo_id() isn't necessarily the top-level window itself on Windows (unlike X11, where Tk
    creates its root widget's window directly as a real top-level X window -- see
    platform_linux.resolve_editor_top_level_window). Moved unchanged from autostart_script.py's
    old start_space_bar_listener closure."""
    user32 = ctypes.windll.user32
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    return user32.GetAncestor(wintypes.HWND(tk_window_id), GA_ROOT)


# Guarded the same way as the winreg import at the top of this module -- these two ctypes
# definitions reference wintypes at class-definition time (module load, not call time), so they'd
# raise NameError on import on Linux/macOS just like an unguarded winreg usage would, defeating
# this module's own "importable from any real OS" contract (see this module's own docstring).
if sys.platform == "win32":
    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(KBDLLHOOKSTRUCT)
    )


def is_space_bar_toggle_event(n_code, wparam, vk_code, foreground_hwnd, editor_hwnd):
    """Decides whether one WH_KEYBOARD_LL callback invocation should toggle the clip editor's
    play/pause -- a genuine space bar key-down (key-up and everything else is ignored; a held
    space auto-repeating the toggle a few times is an accepted, minor quirk, the same way it would
    be for a plain Tk key binding) while the clip editor window is the one currently active/
    foreground, regardless of which of its own child windows (Tk's or VLC's) actually holds
    keyboard focus underneath that. Pure and hook-free on purpose so it's fully unit-testable
    without a real OS hook ever being installed.

    Deliberately never used to decide whether to SUPPRESS the keystroke -- the hook this feeds
    always calls CallNextHookEx regardless of what this returns, so a space typed into a text
    field elsewhere in the editor still reaches it completely normally either way; a separate,
    Tk-side check (mirroring reclaim_focus_now's own Entry/Combobox check) is what skips the
    actual toggle in that case, since only Tk itself can safely ask which of its own widgets
    currently has focus -- not something this OS-level hook has any way to know on its own."""
    if n_code != HC_ACTION:
        return False
    if wparam not in (WM_KEYDOWN, WM_SYSKEYDOWN):
        return False
    if vk_code != VK_SPACE:
        return False
    return foreground_hwnd == editor_hwnd


def run_clip_editor_space_bar_listener(editor_window_handle, on_toggle, stop_event):
    """editor_window_handle is an HWND here (Windows-specific; other backends interpret this
    parameter differently -- see platform_common.run_clip_editor_space_bar_listener). Runs for
    the clip editor's lifetime on its own dedicated daemon thread: SetWindowsHookExW (like
    RegisterHotKey above) has thread affinity, so installing and later unhooking it must both
    happen from this same thread, and that thread needs a real message loop for the hook to ever
    actually fire (per Microsoft's own docs: a low-level hook callback is delivered by sending a
    message to the installing thread, same as RegisterHotKey's WM_HOTKEY).

    Global in the sense that WH_KEYBOARD_LL always monitors every keystroke system-wide -- there's
    no way to scope the hook itself to one window -- but is_space_bar_toggle_event's own
    foreground-window check means on_toggle only actually fires while the clip editor is the
    active window; a space bar press anywhere else (including this app's OWN other windows, e.g.
    a Track Routing dialog, which is its own separate top-level HWND) behaves as if this hook
    didn't exist. Never blocks/consumes the keystroke either way (always calls CallNextHookEx).

    on_toggle is called directly from the raw hook callback, so it MUST be safe to call from a
    thread that isn't the Tk main thread (e.g. a root.after(0, ...) scheduling call, never a
    direct Tk/VLC call) -- see the module comment above this function for why an earlier attempt
    crashed doing that directly.

    stop_event: a threading.Event -- set it and this thread notices within ~50ms, unhooks, and
    exits on its own. Polls rather than blocking on GetMessage specifically so it can notice a
    stop_event set from another thread without that other thread first needing this thread's OS
    thread id to send it a matching PostThreadMessage.

    The entire body below is wrapped in a top-level try/except -- confirmed elsewhere this same
    session (a numpy packaging gap that crashed a different background thread) that an uncaught
    exception on a thread like this one is otherwise swallowed with NO trace at all in this app's
    packaged (--noconsole) build, disguised as the feature just silently not working rather than
    a visible failure. That is exactly the failure mode this function must not repeat."""
    editor_hwnd = editor_window_handle
    try:
        user32 = ctypes.windll.user32
        # Explicit restype/argtypes here aren't optional correctness nice-to-haves -- ctypes
        # defaults an undeclared return/argument to a 32-bit int, which silently truncates a real
        # (pointer-sized, so potentially 64-bit) HANDLE/HWND value on 64-bit Windows. A truncated
        # hook handle handed back to UnhookWindowsHookEx, or a truncated HWND compared against
        # editor_hwnd above, would both fail in ways that look like nothing is happening rather
        # than raising anything -- exactly the kind of bug that's easy to introduce here and hard
        # to notice without it.
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LowLevelKeyboardProc, wintypes.HINSTANCE, wintypes.DWORD]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        # c_void_p (not wintypes.LPARAM) for the 4th arg -- confirmed live: the value actually
        # forwarded here is the POINTER(KBDLLHOOKSTRUCT) hook_proc itself received (declared that
        # way on LowLevelKeyboardProc so hook_proc doesn't need its own cast), and ctypes refuses
        # to implicitly convert a pointer object to an integer-typed LPARAM argument -- it raised
        # ctypes.ArgumentError from inside the callback every single time, silently discarding the
        # call to CallNextHookEx entirely (ctypes has no way to propagate a callback exception
        # back to the C caller, so this failed completely invisibly without the try/except above
        # ever even seeing it). c_void_p accepts pointer-shaped values like this one directly.
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetForegroundWindow.argtypes = []

        def hook_proc(n_code, wparam, lparam):
            try:
                if n_code == HC_ACTION and lparam:
                    info = lparam.contents
                    if info.vkCode == VK_SPACE and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        # Logged unconditionally (matched or not) ONLY for the space bar itself,
                        # never for other keys -- this hook sees every keystroke system-wide, and
                        # logging all of them would both flood the log and capture keystrokes typed
                        # into completely unrelated apps. There was no evidence at all in the log
                        # of what happened after shipping this once already (not even an install
                        # failure warning), so this is deliberately verbose the first time back.
                        fg = user32.GetForegroundWindow()
                        logging.info(
                            "Clip editor: space bar seen (foreground=%s, editor=%s, match=%s).",
                            fg, editor_hwnd, fg == editor_hwnd,
                        )
                    if is_space_bar_toggle_event(n_code, wparam, info.vkCode, user32.GetForegroundWindow(), editor_hwnd):
                        on_toggle()
            except Exception:
                logging.exception("Clip editor: space bar hook callback failed.")
            # A second, separate try/except around this specific call -- confirmed live that an
            # exception raised HERE (a real one hit during development: a wrong argtypes
            # declaration) propagates straight out of this whole ctypes callback uncaught, since
            # it's not inside the try block above. ctypes has no way to relay that back to the C
            # caller that invoked this trampoline, so it's simply printed as "Exception ignored on
            # calling ctypes callback function" and otherwise dropped -- invisible in this app's
            # packaged (--noconsole) build the exact same way the try/except above exists to avoid.
            try:
                return user32.CallNextHookEx(None, n_code, wparam, lparam)
            except Exception:
                logging.exception("Clip editor: space bar hook's CallNextHookEx call failed.")
                return 0

        # Kept referenced for the hook's entire lifetime (this function's own stack frame, alive
        # for as long as the loop below runs) -- see the module comment above for why this matters.
        callback = LowLevelKeyboardProc(hook_proc)
        hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, callback, None, 0)
        if not hook_handle:
            logging.warning(
                "Clip editor: could not install the space bar play/pause hook (error %s) -- use "
                "the on-screen play/pause button instead.", ctypes.get_last_error(),
            )
            return
        logging.info("Clip editor: space bar play/pause hook installed for editor window %s.", editor_hwnd)

        msg = wintypes.MSG()
        try:
            while not stop_event.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    stop_event.wait(0.05)
        finally:
            user32.UnhookWindowsHookEx(hook_handle)
    except Exception:
        logging.exception(
            "Clip editor: space bar play/pause hook failed unexpectedly -- use the on-screen "
            "play/pause button instead."
        )
