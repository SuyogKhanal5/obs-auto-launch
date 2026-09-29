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
import subprocess
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


def find_gog_installed_games(exclude_keywords):
    """Moved from autostart_script.py's old get_gog_installed_games() -- GOG Galaxy itself has
    never shipped a Mac or Linux client (GOG's DRM-free Mac/Linux installers for individual games
    are a separate, unrelated distribution path with no registry/manifest this could hook into),
    so this only ever needs a real implementation here; see CROSS_PLATFORM_PLAN.md §2.5. Only
    ever reached (via platform_common.find_gog_installed_games) after the caller's own
    sys.platform == "win32" check, so Linux/macOS never need their own version of this at all."""
    games = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games") as games_key:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(games_key, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(games_key, subkey_name) as subkey:
                        install_dir = winreg.QueryValueEx(subkey, "path")[0]
                        display_name = winreg.QueryValueEx(subkey, "gameName")[0]
                except OSError:
                    continue
                if not install_dir or not display_name:
                    continue
                if any(kw in display_name.lower() for kw in exclude_keywords):
                    continue
                games.append({"install_dir": os.path.normpath(install_dir).lower(), "display_name": display_name})
    except OSError:
        return []
    return games


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


# --- Global hotkeys (two selectable mechanisms) ---
# Moved wholesale from autostart_script.py's old run_custom_keybind_listener (see
# CROSS_PLATFORM_PLAN.md Phase 3) -- fire_keybind/describe_keybind/notify are now passed in as
# parameters rather than referencing autostart_script.py's module-level names directly, so this
# module never depends on it (the dependency only ever goes the other way -- see
# platform_common.py's own docstring).
#
# Originally built on RegisterHotKey/WM_HOTKEY alone. A WH_KEYBOARD_LL global low-level hook (the
# same mechanism the clip editor's own space bar listener uses) was added as a SELECTABLE
# alternative after a real user report: "Add Marker" (Ctrl+F3) never fired even once across many
# League of Legends sessions, while it worked reliably in every other game (Balatro, REPO) --
# confirmed from the app's own log history, not a one-off. The signature (a registered-
# successfully hotkey that then never delivers a single WM_HOTKEY, with literally nothing logged
# on each press -- not even this app's own "OBS is not connected" fallback) matches a known
# Windows behavior: a game holding true fullscreen EXCLUSIVE mode can suppress WM_HOTKEY delivery
# to every other process system-wide, independent of whether the hotkey itself registered fine
# beforehand. A low-level hook taps the raw input stream at a lower level than the window-message-
# queue-based WM_HOTKEY mechanism, so it keeps working over exclusive fullscreen games where
# RegisterHotKey does not.
#
# It was made a CHOICE rather than an outright replacement after a second real report from the
# same user: perceived system-wide input lag after switching. This is a real, known risk of
# WH_KEYBOARD_LL specifically -- Windows delivers every keystroke, system-wide, SYNCHRONOUSLY
# through the whole hook chain before the target application (a game) ever sees it, so any delay
# in this app's own Python callback (e.g. waiting for the GIL while another of this app's own
# threads -- the audio-mixer overlay's own event callback is a likely culprit, since it can fire
# many times a second -- is mid-bytecode) lands as a real, perceptible input delay in EVERY other
# application, not just this one. RegisterHotKey never sits in that path at all: it only ever
# receives a message for the exact bound combo, asynchronously, so it carries none of that risk.
# Defaulting to RegisterHotKey (DEFAULT_CUSTOM_KEYBIND_HOTKEY_MODE in autostart_script.py) and
# making the hook opt-in trades guaranteed input responsiveness for most users against working
# keybinds in fullscreen-exclusive games specifically for whoever explicitly asks for that trade.
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
# Each modifier name (the vocabulary CUSTOM_KEYBIND_MODIFIER_OPTIONS/a binding's own "modifiers"
# list already uses) maps to every VK code that counts as that modifier -- Win has a left AND right
# key, so it's the only one with more than one.
_CUSTOM_KEYBIND_MODIFIER_VKS = {
    "ctrl": (VK_CONTROL,), "alt": (VK_MENU,), "shift": (VK_SHIFT,), "win": (VK_LWIN, VK_RWIN),
}
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
    """RegisterHotKey's own MOD_* flag vocabulary -- used only by the register_hotkey mode below;
    the low_level_hook mode uses held_modifier_names/find_matching_custom_keybind instead."""
    flags = 0
    for m in modifiers or []:
        flags |= _CUSTOM_KEYBIND_MODIFIER_FLAGS.get(m, 0)
    return flags


def held_modifier_names(is_key_down):
    """is_key_down(vk_code) -> bool (real callers pass a GetAsyncKeyState-backed check). Returns
    the set of modifier names (the same "ctrl"/"alt"/"shift"/"win" vocabulary a binding's own
    "modifiers" list uses) currently held down. Pure and hook-free on purpose, same reasoning as
    is_space_bar_toggle_event below, so the exact-match logic in find_matching_custom_keybind is
    fully unit-testable without a real OS hook or real key state."""
    return {name for name, vks in _CUSTOM_KEYBIND_MODIFIER_VKS.items() if any(is_key_down(vk) for vk in vks)}


def find_matching_custom_keybind(vk_code, is_key_down, bindings_by_vk):
    """bindings_by_vk: {vk_code: [(frozenset(modifier names), binding), ...]}, as built by
    run_custom_keybind_listener. Returns the one binding whose modifiers EXACTLY match what's
    currently held (same semantics RegisterHotKey itself always had -- e.g. Ctrl+Shift+F3 must
    NOT also trigger a plain Ctrl+F3 binding), or None if nothing matches vk_code at all, or none
    of its candidates' modifier sets match exactly."""
    candidates = bindings_by_vk.get(vk_code)
    if not candidates:
        return None
    current = held_modifier_names(is_key_down)
    for mods, binding in candidates:
        if mods == current:
            return binding
    return None


def run_custom_keybind_listener(
    bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
    icon=None, notifications_config=None, status=None, stop_event=None, hotkey_mode=None,
):
    """Dispatches to one of two mechanisms based on hotkey_mode -- see the module comment above
    this whole section for the full tradeoff. "low_level_hook" (or any other non-"register_hotkey"
    value) uses _run_custom_keybind_listener_low_level_hook; everything else, including None
    (so an old/incomplete config that never set this at all gets the safe choice), uses
    _run_custom_keybind_listener_register_hotkey."""
    if hotkey_mode == "low_level_hook":
        _run_custom_keybind_listener_low_level_hook(
            bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
            icon=icon, notifications_config=notifications_config, status=status, stop_event=stop_event,
        )
    else:
        _run_custom_keybind_listener_register_hotkey(
            bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
            icon=icon, notifications_config=notifications_config, status=status, stop_event=stop_event,
        )


def _run_custom_keybind_listener_register_hotkey(
    bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
    icon=None, notifications_config=None, status=None, stop_event=None,
):
    """The default, safe mechanism -- see the module comment above this section for the full
    RegisterHotKey-vs-low-level-hook tradeoff this exists alongside.

    Runs for its whole lifetime on one dedicated daemon thread: RegisterHotKey (and the WM_HOTKEY
    messages it produces) has thread affinity, so every binding must be registered from -- and
    received on -- the same thread. Passing hwnd=None posts WM_HOTKEY straight to this thread's
    message queue instead of routing through a window, so no hidden window is needed.

    stop_event: a threading.Event -- same PeekMessageW-polling pattern as
    run_clip_editor_space_bar_listener's own stop_event handling (see that function's own
    docstring for the full rationale), used here so THIS process's own UnregisterHotKey cleanup
    below actually runs within ~50ms of being asked to shut down, instead of only whenever Windows
    gets around to noticing the whole process has died. That distinction matters a lot here
    specifically: confirmed live (repeatedly, in real use) that a plain GetMessageW loop with no
    stop_event -- relying solely on process-death cleanup -- left a self-restarted app's new
    process racing the OLD one for the same hotkey, and the old process's real teardown time (VLC/
    libvlc, Tk, pystray, etc. all still unwinding) was observed to exceed even a generous 15-second
    retry budget on the NEW process's side. Proactively releasing here removes the race entirely
    for any graceful shutdown (a Settings-save restart, or Quit); an actual crash/force-kill still
    falls back to Windows' own process-death cleanup, same as before. Optional (defaults to None,
    falling back to the old plain GetMessageW loop) only so a caller that genuinely has no
    stop_event of its own doesn't crash -- every real caller in this app always provides one."""
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
        # 30 attempts at 0.5s (15s ceiling), not the 5x0.3s (1.5s) originally here: confirmed live,
        # twice in the same real session, that 1.5s genuinely isn't enough -- a restart with a
        # clip editor open (VLC/libvlc threads still unwinding) took noticeably longer than that
        # for the old process to actually let go, and both times the keybind silently never came
        # back until the NEXT restart happened to win the race instead. This only ever costs
        # anything in that exact race (a plain first launch always succeeds on attempt 1, instantly).
        registered_ok = False
        for _attempt in range(30):
            if user32.RegisterHotKey(None, hotkey_id, mods, vk):
                registered_ok = True
                break
            time.sleep(0.5)
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

    def dispatch(msg):
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

    msg = wintypes.MSG()
    try:
        if stop_event is None:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                dispatch(msg)
        else:
            while not stop_event.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                    dispatch(msg)
                else:
                    stop_event.wait(0.05)
    finally:
        for hotkey_id, _ in registered:
            user32.UnregisterHotKey(None, hotkey_id)


def _run_custom_keybind_listener_low_level_hook(
    bindings, get_client, get_manual_split_buffer_seconds, fire_keybind, describe_keybind, notify,
    icon=None, notifications_config=None, status=None, stop_event=None,
):
    """The opt-in, fullscreen-compatible mechanism -- see the module comment above this section
    for the full tradeoff (real, perceptible input-lag risk system-wide) this carries relative to
    the default _run_custom_keybind_listener_register_hotkey.

    Runs for its whole lifetime on one dedicated daemon thread, same shape as
    run_clip_editor_space_bar_listener below (see that function's own docstring for the full
    SetWindowsHookExW/thread-affinity/message-loop rationale, and for why the ctypes callback
    trampoline must stay referenced for the hook's entire lifetime) -- this installs ONE global
    WH_KEYBOARD_LL hook covering every configured binding, rather than one OS-level registration
    per binding the way RegisterHotKey needed.

    Held-key de-duplication: a low-level hook keeps delivering WM_KEYDOWN on every OS auto-repeat
    tick for as long as a key stays held, unlike RegisterHotKey's own MOD_NOREPEAT flag -- without
    tracking which vk codes are already "down" (via the `held` set below, cleared on the matching
    WM_KEYUP/WM_SYSKEYUP), a key held a little too long would fire its action many times over
    (adding a burst of duplicate markers, repeatedly toggling recording, etc.).

    Exact modifier matching (see find_matching_custom_keybind) is checked at the moment of the
    matched key's OWN keydown via GetAsyncKeyState, not by also hooking the modifier keys
    themselves -- simpler, and sufficient since a hotkey is only ever evaluated on its own
    non-modifier key's press.

    stop_event: a threading.Event -- same PeekMessageW-polling pattern as
    run_clip_editor_space_bar_listener's own stop_event handling, so this thread notices a shutdown
    request and unhooks within ~50ms rather than only whenever Windows notices the whole process
    has died. Optional (defaults to None, falling back to the old plain GetMessageW loop) only so a
    caller that genuinely has no stop_event of its own doesn't crash -- every real caller in this
    app always provides one."""
    bindings_by_vk = {}
    for binding in bindings:
        if not binding.get("enabled", True):
            continue
        vk = vk_code_for_key(binding.get("key", ""))
        if vk is None:
            logging.warning("Custom keybind has an invalid key %r; skipping.", binding.get("key"))
            continue
        mods = frozenset(m for m in (binding.get("modifiers") or []) if m in _CUSTOM_KEYBIND_MODIFIER_VKS)
        bindings_by_vk.setdefault(vk, []).append((mods, binding))

    if not bindings_by_vk:
        return

    user32 = ctypes.windll.user32
    user32.SetWindowsHookExW.restype = wintypes.HANDLE
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LowLevelKeyboardProc, wintypes.HINSTANCE, wintypes.DWORD]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    # c_void_p, not wintypes.LPARAM -- see run_clip_editor_space_bar_listener's own comment on the
    # identical line below for why (the same ctypes gotcha applies here unchanged).
    user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

    def is_key_down(vk):
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    held = set()

    def hook_proc(n_code, wparam, lparam):
        try:
            if n_code == HC_ACTION and lparam:
                info = lparam.contents
                vk = info.vkCode
                if wparam in (WM_KEYUP, WM_SYSKEYUP):
                    held.discard(vk)
                elif wparam in (WM_KEYDOWN, WM_SYSKEYDOWN) and vk not in held:
                    held.add(vk)
                    binding = find_matching_custom_keybind(vk, is_key_down, bindings_by_vk)
                    if binding:
                        threading.Thread(
                            target=fire_keybind,
                            args=(binding, get_client, get_manual_split_buffer_seconds, icon, notifications_config, status),
                            daemon=True,
                        ).start()
        except Exception:
            logging.exception("Custom keybind hook callback failed.")
        try:
            return user32.CallNextHookEx(None, n_code, wparam, lparam)
        except Exception:
            logging.exception("Custom keybind hook's CallNextHookEx call failed.")
            return 0

    # Kept referenced for the hook's entire lifetime (this function's own stack frame) -- see
    # run_clip_editor_space_bar_listener's own comment on why an unreferenced trampoline crashes.
    callback = LowLevelKeyboardProc(hook_proc)
    hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, callback, None, 0)
    if not hook_handle:
        logging.warning(
            "Could not install the custom-keybind hook (error %s) -- custom keybinds won't work "
            "this session.", ctypes.get_last_error(),
        )
        notify(
            icon, notifications_config, "Keybinds not registered",
            "Could not install the custom-keybind hook -- your configured keybinds won't work "
            "until the app is restarted.",
        )
        return

    for entries in bindings_by_vk.values():
        for _mods, binding in entries:
            logging.info("Registered custom keybind %s -> %s", describe_keybind(binding), binding.get("action"))

    msg = wintypes.MSG()
    try:
        if stop_event is None:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        else:
            while not stop_event.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    stop_event.wait(0.05)
    finally:
        user32.UnhookWindowsHookEx(hook_handle)


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


# --- Shortcuts / autostart (Startup-folder .lnk) ---
# Moved wholesale from installer.py's old create_shortcut/get_desktop_shortcut_path/
# get_startup_shortcut_path and autostart_script.py's old get_startup_shortcut_path/
# is_startup_shortcut_enabled/set_startup_shortcut_enabled -- both call sites used their own
# separate (but identical) PowerShell-shortcut implementation before this move; now there's one,
# reached from both through platform_common.is_autostart_enabled/enable_autostart/
# disable_autostart, per CROSS_PLATFORM_PLAN.md Phase 5.
APP_NAME = "OBS Auto Recorder"
STARTUP_SHORTCUT_NAME = "OBSAutoRecorder.lnk"


def _ps_single_quote(value):
    return "'" + value.replace("'", "''") + "'"


def create_shortcut(link_path, target, working_dir):
    try:
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
    except OSError:
        pass
    ps_script = (
        "$WshShell = New-Object -ComObject WScript.Shell; "
        f"$Shortcut = $WshShell.CreateShortcut({_ps_single_quote(link_path)}); "
        f"$Shortcut.TargetPath = {_ps_single_quote(target)}; "
        f"$Shortcut.WorkingDirectory = {_ps_single_quote(working_dir)}; "
        "$Shortcut.Save()"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def get_desktop_shortcut_path():
    desktop = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop")
    return os.path.join(desktop, f"{APP_NAME}.lnk")


def get_startup_shortcut_path():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", STARTUP_SHORTCUT_NAME)


def is_autostart_enabled():
    path = get_startup_shortcut_path()
    return bool(path and os.path.isfile(path))


def enable_autostart(target_path, working_dir):
    path = get_startup_shortcut_path()
    if not path:
        logging.warning("Could not resolve the Startup folder; cannot manage the startup shortcut.")
        return False
    if create_shortcut(path, target_path, working_dir):
        logging.info("Created startup shortcut at %s", path)
        return True
    logging.error("Failed to create startup shortcut at %s", path)
    return False


def disable_autostart():
    path = get_startup_shortcut_path()
    if not path:
        logging.warning("Could not resolve the Startup folder; cannot manage the startup shortcut.")
        return False
    if os.path.isfile(path):
        try:
            os.remove(path)
            logging.info("Removed startup shortcut.")
        except OSError as exc:
            logging.error("Failed to remove startup shortcut: %s", exc)
            return False
    return True


# --- Optional dependency install (winget) ---
# Generalizes installer.py's old winget_install(package_id)/has_winget() and
# autostart_script.py's own duplicate winget_install_ffmpeg()/winget_install_vlc() into one
# shared function, reached through platform_common.install_optional_dependency() --
# CROSS_PLATFORM_PLAN.md Phase 7 / §3.3's interface table.
_WINGET_PACKAGE_IDS = {
    "ffmpeg": "Gyan.FFmpeg",
    "obs": "OBSProject.OBSStudio",
    "vlc": "VideoLAN.VLC",
}


def has_package_manager():
    return shutil.which("winget") is not None


def install_optional_dependency(package, timeout=600):
    """Best-effort silent install via winget. Returns (True, None) on success, (False, reason)
    otherwise -- never raises, so a failed/missing winget never blocks the rest of setup."""
    package_id = _WINGET_PACKAGE_IDS.get(package)
    if not package_id:
        return False, f"No winget package id known for '{package}'."
    try:
        result = subprocess.run(
            [
                "winget", "install", "--id", package_id, "-e", "--silent",
                "--accept-source-agreements", "--accept-package-agreements",
            ],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, None
    return False, (result.stdout or result.stderr or f"winget exited with code {result.returncode}")[-500:].strip()


def embed_video_player(player, tk_widget):
    """Embeds a python-vlc player's output into tk_widget -- Windows takes a raw HWND, the same
    integer Tk's own winfo_id() already returns. Moved unchanged from the clip editor's old
    direct player.set_hwnd(video_frame.winfo_id()) call site."""
    player.set_hwnd(tk_widget.winfo_id())


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
