# Cross-Platform (Linux/macOS) Migration Plan

**Status: Phases 0-8 complete and CI-verified; this document is kept as the technical record of *why* the architecture looks the way it does and *what was actually confirmed working* (vs. guessed), not archived — read it before touching any OS-specific code path.** Phase 6 (game audio isolation) is implemented on Windows and macOS, both confirmed empirically against a real, running OBS instance (§6.1, §6.2); Linux remains a confirmed negative finding, not an unimplemented feature — see §6.1 before assuming this feature could exist there at all. **§5.12 is worth reading before assuming the macOS port works at all**: the very first real end-to-end run on real Mac hardware found the app crashed on startup every time (a Tk/AppKit main-thread conflict with `pystray`, never caught before since CI can't run a real GUI on macOS) — now fixed and verified live, but a reminder that "CI passes" and "actually confirmed to run" are different claims on this OS specifically. For user-facing setup/usage docs, see [README.md](README.md), which now also has OS-specific instructions throughout (Phase 9).

Branch: `os-independent`. This document was the working plan for porting OBS Auto Recorder from Windows-only to Windows + Linux + macOS, maximizing shared code between all three so a bug fixed on one platform is fixed (or trivially portable) on the others.

This was a planning document, not a changelog, while the migration was in progress — updated as work proceeded (phases checked off, wrong assumptions corrected, discovered issues added) rather than treated as frozen. It's kept in that same updated-in-place form now that the migration itself is done, since the reasoning and real findings throughout remain the most complete explanation of this codebase's cross-platform design.

---

## 1. Goals and non-goals

**Goal:** one shared codebase (`autostart_script.py`'s business logic, Tk UI, config schema, OBS WebSocket control flow, clip editor) with a thin, swappable platform layer underneath it, so Windows/Linux/macOS differ only where the OS genuinely forces a difference.

**Non-goals for this plan (explicitly out of scope unless stated otherwise):**
- Wayland support for global hotkeys or window-title detection (see §2.3). Documented gap, not silently broken.
- GOG Galaxy / Xbox Game Pass auto-detection on Linux **or** macOS, and Epic Games Launcher / Battle.net auto-detection on Linux specifically — none of these launchers ship a native client on those OSes at all (see §2.5's rule). Manual process-name configuration remains the fallback wherever auto-detection doesn't apply.
- Changing any existing Windows behavior. This is additive: Windows keeps working exactly as it does today, verified by the existing test suite staying green throughout.

## 2. Decisions already made (from clarifying questions)

1. **Platform/package targets:** Linux — broad/distro-agnostic (AppImage as the primary format, plus a plain tarball). macOS — originally planned as universal2 (Intel + Apple Silicon in one binary); confirmed NOT possible via Phase 7's real build (see §7.4) since `numpy` doesn't publish a universal2 wheel at all — ships as an Apple Silicon (arm64)-only build for now, real Intel support tracked as follow-up.
2. **Testing approach:** CI-runner-only for now. No assumption of real Linux/macOS hardware. GitHub Actions' `ubuntu-latest`/`macos-latest` runners plus Docker (optional, for local Linux-distro variation testing from your Windows machine) are the "simulate other OSes" mechanism — see §7.
3. **Global hotkeys:** X11 only for v1 (same conceptual model as Windows' `RegisterHotKey` — a real global hotkey that fires regardless of focus). Wayland users get no in-app global hotkey for v1; this is a documented, known gap, not a bug to chase down. The same X11-vs-Wayland split applies to the clip editor's space-bar-while-video-focused behavior and to window-title detection (§5.3's Minecraft-style rules), since all three rely on similar low-level input/window APIs that don't exist on Wayland by design.
4. **Game Audio isolation:** build real per-OS equivalents — PipeWire per-app capture on Linux OBS, CoreAudio/ScreenCaptureKit-based per-app capture on macOS OBS — not just a stub. This is flagged as the single highest-uncertainty item in the whole plan (§6, Phase 6) since it requires empirically confirming OBS's actual source-type schema on each OS, not just reading docs.
5. **Launcher auto-detection scope — general rule:** support a launcher's auto-detection on every OS that launcher itself ships a native client for; don't build detection for an OS a launcher was never released on. Applied to the 5 launchers this app knows about:
   - **Steam** — native on Windows, macOS, and Linux → full auto-detection on all 3.
   - **Epic Games Launcher** — native on Windows and macOS, no Linux client → auto-detection on Windows + macOS. macOS manifest location follows Epic's standard app-support convention (`~/Library/Application Support/Epic/EpicGamesLauncher/Data/Manifests`, same `.item` JSON format as Windows) — confirm the exact path against a real Epic macOS install or Epic's own docs during Phase 2 implementation, since this is inferred from platform convention, not independently verified yet.
   - **Battle.net** — native on Windows and macOS, no Linux client → auto-detection on Windows + macOS. This is actually the *easiest* of the four non-Steam launchers to extend: today's Windows implementation doesn't do registry/manifest discovery at all, just user-configured `install_dirs` (§5.3) — the macOS port is mostly just macOS-appropriate default paths/UI copy, no new discovery mechanism needed.
   - **GOG Galaxy** — the Galaxy client itself has never shipped for macOS or Linux (Windows-only), even though GOG sells DRM-free Mac/Linux installers for individual games separately through its web store — that's a different, unrelated distribution path with no manifest/registry this app could hook into. Stays Windows-only.
   - **Xbox / PC Game Pass app** — Windows-only (UWP-based), no Mac or Linux client. Stays Windows-only.
6. **macOS minimum version:** 14.4 (Sonoma) — raised from the original 13 (Ventura) once Phase 6's real per-app-audio-capture path was identified: Apple's Core Audio process taps (`AudioHardwareCreateProcessTap`, the mechanism OBS's own macOS "Application Audio Capture" source is built on) need macOS 14.4+, not the 13+ ScreenCaptureKit-based approach originally assumed. Rather than maintaining two floors (13 for everything else, 14.4 just for audio isolation) and the support/testing complexity of "most things work on Ventura, one feature silently doesn't," the whole app's floor moved to 14.4 -- Ventura is no longer a supported target at all.
7. **Linux dependency install (ffmpeg, and OBS itself via the installer's existing "Easy Install" button):** not a silent background auto-install like Windows' `winget`, and not just static manual instructions either. Instead: detect the available package manager (`apt`/`dnf`/`pacman`/`zypper`, checked via `shutil.which`) and/or Flatpak, build the **exact** install command for it, and show that command to the user as read-only text in the installer UI — transparent, nothing hidden — with a "Run this command" button that executes it via `pkexec` (the standard Linux mechanism for a GUI app to request a graphical privilege-escalation prompt, the equivalent of Windows' UAC prompt or macOS's `osascript "with administrator privileges"`) if they're satisfied it's correct. If package-manager detection fails, or the user prefers not to click it, fall back to the same manual per-distro instructions text as before. Note for Phase 7 implementation: OBS specifically often isn't in a distro's *default* repos (notably Fedora, which needs RPM Fusion enabled first) — the generated command should prefer a Flatpak install (`flatpak install flathub com.obsproject.Studio`) for OBS where Flatpak is available, since that's the most reliably up-to-date and distro-agnostic path, while ffmpeg (near-universally packaged) can just use the detected native package manager directly.
8. **Installer elevation model:** confirmed as proposed — Linux/macOS app installs default to a **per-user** location (`~/.local/share/OBSAutoRecorder` on Linux, `~/Applications` on macOS), needing no elevation for the app install itself. This is a separate concern from §2.7's `pkexec` use, which is specifically for installing *system packages* (ffmpeg/OBS) via the OS package manager — that always needs root regardless of where this app itself lives, the same way Windows' `winget install` needs admin even though the app itself installs to `Program Files` by user choice, not because installing *this app* requires it.

## 3. Architecture: the platform abstraction layer

This is the mechanism that satisfies "reuse as much code as possible" and "simulate other OSes."

### 3.1 New module layout

```
platform_common.py    # dispatcher + the shared interface every OS module implements
platform_windows.py   # current Windows-only code, MOVED here (not duplicated)
platform_linux.py     # new
platform_macos.py     # new
```

`autostart_script.py` and `installer.py` import from `platform_common`, never directly from `platform_windows`/`platform_linux`/`platform_macos`. `platform_common` picks the right implementation:

```python
import sys

def _select_backend():
    if sys.platform == "win32":
        import platform_windows as backend
    elif sys.platform == "darwin":
        import platform_macos as backend
    else:
        import platform_linux as backend
    return backend

_backend = _select_backend()

def find_obs_executable(*args, **kwargs):
    return _backend.find_obs_executable(*args, **kwargs)
# ... one thin forwarding function per interface entry point, OR
# `_backend = _select_backend(); globals().update({name: getattr(_backend, name) for name in INTERFACE_NAMES})`
# whichever reads more clearly once the actual interface list is drafted at implementation time.
```

### 3.2 The critical testability rule

**Every platform-dispatch decision must be overridable, not a hardcoded `sys.platform` check buried deep in a function.** Concretely: `_select_backend()` (and any place that branches on OS) takes an optional `platform_name` parameter defaulting to `sys.platform`, so a test can do:

```python
def test_find_obs_executable_checks_flatpak_path_on_linux():
    backend = platform_common._select_backend(platform_name="linux")
    ...
```

This is what makes "simulate other OSes" real rather than aspirational: the **entire** Linux and macOS code path's *logic* (not the actual OS calls, which still need a real runner to execute against a real filesystem/registry/window system — see §7) can be exercised and asserted on by the Windows-hosted test suite, and vice versa. Every new `platform_*.py` function should be written to be callable and assertable this way — pure inputs (a fake filesystem via `unittest.mock.patch`, a fake `psutil.process_iter` list, etc.) in, deterministic output out, exactly like the existing `test_custom_keybinds.py`/`test_vlc.py` patterns already do for Windows (just no longer needing the OS to match).

### 3.3 Interface surface (functions `platform_common` exposes)

Drafted from the audit in §5; exact signatures get finalized during Phase 0-1 implementation, not frozen here:

| Function | Backends needed | Notes |
|---|---|---|
| `find_obs_executable(configured_path=None)` | Win/Linux/Mac | replaces `installer.py`'s `find_obs_exe` + the main app's implicit reliance on `obs.path` |
| `find_ffmpeg_executable(configured_path=None)` | Win/Linux/Mac | `shutil.which` fast path already shared; only fallback candidates differ |
| `find_ffprobe_executable(ffmpeg_path)` | Win/Linux/Mac | fixes the hardcoded `ffprobe.exe` suffix bug (§5.2) as a side effect |
| `find_vlc_executable_or_libvlc()` | Win/Linux/Mac | replaces registry-based `find_vlc_path` |
| `get_steam_library_paths(steam_config)` | Win/Linux/Mac | VDF-parsing logic shared; only the install-root discovery differs |
| `get_epic_installed_games(...)` | Win/Mac | macOS reads Epic's Mac manifest location instead of `%PROGRAMDATA%`; same `.item` JSON parsing logic reused |
| `get_battlenet_install_dirs(...)` | Win/Mac | already just user-configured dirs on Windows today — macOS needs only OS-appropriate default paths/UI copy, no new discovery code |
| `get_gog_installed_games(...)`, `get_xbox_install_dirs(...)` | Win only | GOG Galaxy and the Xbox/Game Pass app have never shipped for Linux or macOS at all — see §2.5 |
| `get_window_titles()` | Win/Linux(X11 only) | Mac via Quartz; Linux returns `{}` under Wayland (detected via `XDG_SESSION_TYPE`) with one logged warning, not a crash |
| `register_global_hotkey(...)`/`unregister_global_hotkey(...)` | Win/Linux(X11 only)/Mac | Linux returns a clear "not supported under Wayland" result rather than silently doing nothing |
| `embed_video_player(vlc_player, tk_widget)` | Win/Linux/Mac | `set_hwnd`/`set_xwindow`/`set_nsobject` |
| `enable_autostart(exe_path)`/`disable_autostart()`/`is_autostart_enabled()` | Win/Linux/Mac | `.lnk` / XDG `.desktop` / LaunchAgents `.plist` |
| `get_monitor_rects()` | Win/Linux/Mac | **prefer replacing this with the `screeninfo` PyPI package outright** rather than hand-writing 3 backends — it already supports all 3 OSes and removes an entire abstraction-layer entry |
| `hide_console_subprocess_kwargs()` | Win/Linux/Mac | returns `{"creationflags": subprocess.CREATE_NO_WINDOW}` on Windows, `{}` elsewhere — replaces all 11 direct `CREATE_NO_WINDOW` references |
| `configure_process_audio_capture(client, input_name, process_name)` | Win(WASAPI)/Linux(PipeWire)/Mac(CoreAudio) | Phase 6, highest uncertainty — see §6 |
| `install_optional_dependency(package)` (ffmpeg/OBS auto-install) | Win(winget)/Mac(brew) | silent best-effort background install, same as today |
| `build_linux_install_command(package)` / `run_linux_install_command(command)` | Linux only | returns the exact detected-package-manager (or Flatpak, preferred for OBS) command as a string for the UI to display verbatim, and a separate function to execute it via `pkexec` only on explicit user click — see §2.7. Falls back to manual instructions if no package manager is detected. |

Functions **not** listed here (disk usage, config/log paths, process enumeration via `psutil`, the OBS WebSocket control flow, the entire Tk UI, the recording state machine) are **already portable today** per the audit — they stay exactly where they are, untouched.

## 4. Why a platform layer instead of scattering `if sys.platform` checks inline

Scattering `if sys.platform == "win32":` throughout `autostart_script.py` (which is already ~8,400 lines) would make the file harder to read on every OS at once, and — critically — undermines the "fix a bug once, fixed everywhere" goal, since a Linux-specific bug fix would be buried inside a Windows-centric file instead of living in a Linux-owned module a Linux-focused contributor (or future you) can find and reason about in isolation. The forwarding-function pattern in §3.1 keeps `autostart_script.py` OS-agnostic reading top to bottom, with exactly one place (`platform_common.py`) that ever branches on `sys.platform`.

## 5. Complete Windows-dependency catalog

Everything found by direct audit, organized by subsystem. File:line references are current as of branch creation (2026-09-16) — re-verify line numbers before editing, since earlier phases will shift later ones.

### 5.1 Blocking issue — must be fixed before anything else works

- `autostart_script.py:16` — `import winreg` (unconditional, top-level module import)
- `autostart_script.py:17` — `from ctypes import wintypes` (unconditional, top-level)
- `installer.py:24` — `import winreg` (unconditional, top-level)

**Effect:** `winreg` and (in practice) `ctypes.wintypes` don't exist outside Windows. Every one of the 12+ files under `tests/` that does `import autostart_script` or `import installer` currently fails at collection time on Linux/macOS with `ModuleNotFoundError: No module named 'winreg'` — not a test failure, a hard crash before any test runs. This is Phase 0, step 1, before any other work in this plan is meaningful.

### 5.2 OBS / ffmpeg / VLC discovery

- `installer.py:34-37` (`DEFAULT_OBS_PATHS`), `installer.py:164-181` (`find_obs_exe`) — hardcoded `C:\Program Files\obs-studio\...` + `winreg` uninstall-key lookup.
- `installer.py:184-192` (`is_obs_running`) — matches literal `"obs64.exe"`/`"obs32.exe"`.
- `autostart_script.py:5375-5376`, `6294` — GUI default for `obs.process_name` is `"obs64.exe"`.
- `autostart_script.py:2776-2802` (`find_ffmpeg`) — `shutil.which("ffmpeg")` fast path already portable; Windows-only fallback candidates (`C:\ffmpeg\bin`, `C:\Program Files\ffmpeg\bin`, chocolatey, scoop, WinGet package glob).
- `autostart_script.py:2805-2817` (`resolve_ffmpeg_path`) — portable logic, feeds from the above.
- `autostart_script.py:3384-3385` — **bug, not just a porting gap**: `ffprobe.exe` suffix is hardcoded even on the code path that already found a `.exe`-less `ffmpeg` via `shutil.which`. Fix as part of Phase 1 regardless of OS.
- `installer.py:64-83` (`is_ffmpeg_installed`) — duplicates `find_ffmpeg`'s Windows candidate list (installer intentionally doesn't import the main app's module — keep that separation, just give the installer the same platform-layer call).
- `autostart_script.py:2859-2862` — `find_vlc_path`'s registry lookup (`HKEY_LOCAL_MACHINE`/`HKEY_CURRENT_USER`, `Software\VideoLAN\VLC`); already has a `shutil.which` fallback per `tests/test_vlc.py:63`.

### 5.3 Game / launcher / window detection

- `autostart_script.py:173-184` (`get_steam_install_path`) — `winreg` (`HKCU`/`HKLM`, `Software\Valve\Steam`, value `SteamPath`).
- `autostart_script.py:187-211` (`get_steam_common_dirs`) — VDF parsing (portable) + `allowed_drives`/`os.path.splitdrive` filtering (Windows-only concept; **silently becomes a no-op filter on POSIX today if ported naively**, since `splitdrive` returns `''` there — real functional-regression risk to design around, not just a syntax fix).
- `autostart_script.py:214-227` (`is_exe_under_dirs`), `230-240` (`get_display_name_from_dirs`) — portable path-prefix logic, shared by Steam/Xbox/Battle.net.
- `autostart_script.py:243-249` (`get_xbox_install_dirs`) — Xbox/Game Pass has no Linux or macOS client at all; per §2.5, backend returns `[]` on both.
- `autostart_script.py:252-261` (`get_battlenet_install_dirs`) — no registry/manifest lookup today, just user-configured dirs; per §2.5, Battle.net ships a native macOS client, so the macOS backend gets the same mechanism with macOS-appropriate default paths (no new discovery code needed, just data/copy). Linux backend returns `[]` (no Battle.net client there).
- `autostart_script.py:264-297` (`get_gog_installed_games`) — pure `winreg` enumeration (`HKLM\SOFTWARE\WOW6432Node\GOG.com\Games`); per §2.5, not ported to either OS (GOG Galaxy itself has never shipped for Mac or Linux).
- `autostart_script.py:300-302` (`get_epic_manifest_dir`), `305-337` (`get_epic_installed_games`) — hardcoded `%PROGRAMDATA%\Epic\...` + `.item` JSON manifests. Per §2.5, Epic Games Launcher ships a native macOS client, so this gets a real macOS port: same `.item`-manifest-parsing logic, pointed at Epic's macOS manifest location instead (`~/Library/Application Support/Epic/EpicGamesLauncher/Data/Manifests` by platform convention — verify the exact path during Phase 2, not independently confirmed yet). Linux backend returns `[]` (no Epic client there; Heroic Games Launcher is a different app with a different manifest format and out of scope).
- `autostart_script.py:340-349` (`find_game_by_install_dir`), `352-375` (`find_target_process`) — the core matching pipeline; **algorithm is portable as-is**, only the data feeding it (from the functions above) is Windows-specific. No changes needed to this function itself beyond what naturally follows from its inputs changing shape.
- `autostart_script.py:4520-4529` (`COMMON_GAMES`) — hardcoded `.exe` catalog (League of Legends, Valorant, Apex, Genshin, Wizard101, Warframe, Roblox, Minecraft's `javaw.exe`). Most of these either have no Linux/macOS client, or run via Proton/Wine with a different process identity than the native Windows binary. **Do not attempt a 1:1 port of this list.** Add `COMMON_GAMES_LINUX`/`COMMON_GAMES_MACOS` (or a per-entry `platforms: {...}` key) containing only genuinely cross-platform titles (Minecraft: Java Edition, Roblox) with their real Linux/macOS process names, and drop the rest for those platforms rather than shipping entries that will silently never match.
- `autostart_script.py:152-170` (`get_window_titles`) — pure `ctypes.windll.user32` (`EnumWindows`, `GetWindowTextW`, `GetWindowThreadProcessId`). Needs a full reimplementation per OS, not a shim:
  - **Linux/X11**: `python-xlib`, querying `_NET_CLIENT_LIST` + `_NET_WM_NAME` + `_NET_WM_PID`. New dependency.
  - **Linux/Wayland**: no reliable unprivileged cross-compositor API exists. Detect via `XDG_SESSION_TYPE=wayland` and return `{}` with one logged notice, consistent with the hotkey decision in §2.
  - **macOS**: `pyobjc` (`Quartz.CGWindowListCopyWindowInfo`). New dependency (`pyobjc-framework-Quartz`).
- `autostart_script.py:4507-4529` — comment block explains `COMMON_GAMES` exists for launchers the auto-detection can't reach (Riot Client, EA app, etc.) — all additional Windows-only launchers with zero existing detection code; no action needed beyond what's already covered above.

### 5.4 Global hotkeys

- `autostart_script.py:2498-2562` (`run_custom_keybind_listener`) — `RegisterHotKey`/`WM_HOTKEY` message loop on a dedicated thread. Needs an X11 backend (`python-xlib` `XGrabKey` + an event loop on its own thread, same threading shape as today) for Linux; a Quartz `CGEventTap` backend for macOS (requires the user to grant Accessibility permission — needs an in-app detection-and-guidance flow, since a silently-failing hotkey with no explanation is a bad experience).
- `autostart_script.py:2591-2680`ish (`run_clip_editor_space_bar_listener`, `is_space_bar_toggle_event`, `KBDLLHOOKSTRUCT`, `LowLevelKeyboardProc`) — `WH_KEYBOARD_LL` global hook, scoped in practice to "only act while the editor is the foreground window." Reuse the same X11/Quartz backends built for custom keybinds where possible rather than a second bespoke mechanism — worth a design pass at implementation time to confirm the foreground-window-gating check ports cleanly (X11: compare against `_NET_ACTIVE_WINDOW`; macOS: `NSWorkspace.frontmostApplication`).
- `autostart_script.py:7773-7787` (`start_space_bar_listener`) — `ctypes.windll.user32.GetAncestor` to resolve the editor's top-level HWND for the foreground check above; needs an X11/Quartz equivalent window-handle resolution.

### 5.5 Clip editor video embedding

- `autostart_script.py:6999` — `player.set_hwnd(video_frame.winfo_id())`.
  - **Linux/X11**: `player.set_xwindow(video_frame.winfo_id())` — same `winfo_id()` call already used today, just a different python-vlc method. Low-risk swap.
  - **macOS**: `player.set_nsobject(...)` needs an actual `NSView` pointer, which Tk's `winfo_id()` does **not** provide on macOS (it returns something else entirely there). This needs a small PyObjC bridge to get the real NSView from the Tk widget — flagged as the highest-effort item in Phase 4, needs hands-on iteration against real macOS CI output (screenshots/logs), not just code review. **Still open**: implemented (`platform_macos.embed_video_player`) but not yet exercised against a real video file with a real user watching the preview render — §5.12 below fixed a more fundamental blocker (the app couldn't stay running at all) that had to be resolved first before this could even be reached.

### 5.12 Real-hardware finding: Tk/pystray main-thread conflict crashed the whole app on macOS

Found on real Mac hardware (Apple Silicon, macOS 15.5, Python 3.12 python.org build) running the actual app end-to-end for the first time — not caught by anything before this, because CI cannot launch a real GUI session on macOS at all (§6.1's own macOS finding), and nobody had run the real app on real macOS hardware from a fresh config until this point. **This wasn't a clip-editor-specific or audio-specific bug — the app crashed on startup, before the tray icon, overlay, or any window ever appeared, on every launch.**

**Symptom**: `*** Terminating app due to uncaught exception 'NSInvalidArgumentException', reason: '-[NSApplication macOSVersion]: unrecognized selector sent to instance ...'`, deep inside Tk's own color-resolution code (`GetRGBA`/`TkpGetColor`), the moment the first colored widget (e.g. `tk.Frame(..., bg=...)`) was created — an uncaught Objective-C exception, which terminates the whole process outright (not a Python-catchable exception, and no crash report was written either).

**Root cause — two independent, both confirmed empirically with minimal isolated repros before touching the real app**:
1. **AppKit requires all GUI work on the real process main thread.** This app's default architecture (unchanged on Windows/Linux, where it works fine) runs `pystray`'s `icon.run()` — which blocks the real main thread — while the entire Tk GUI (the overlay, and everything built as a `Toplevel` of its one `tk.Tk()`: Settings, the clip editor) runs on a background thread (`overlay_thread`). Confirmed directly: a `tk.Tk()` + real `mainloop()` on a background thread throws a *different*, plainly-worded crash — `NSWindow should only be instantiated on the main thread!` — the instant its window actually gets mapped.
2. **Tk's own Cocoa integration must finish registering itself on the shared `NSApplication` before anything else touches it.** Confirmed directly: creating (and destroying, without ever running `mainloop()`) a throwaway `tk.Tk()` *before* constructing a `pystray.Icon(...)` avoids the `macOSVersion` crash entirely, even for a second, fully-real Tk window created afterward; constructing `pystray.Icon(...)` (which touches `AppKit.NSApplication.sharedApplication()` via PyObjC in its own `__init__`) *before* Tk has ever initialized reproduces the crash reliably. This app's `main()` always constructed `pystray.Icon(...)` well before the overlay's Tk instance was ever created, on every OS — harmless on Windows/Linux, fatal on macOS.

**Fix** (`platform_common.gui_requires_main_thread()`, `True` only on `darwin`), applied in `autostart_script.py`'s `main()`, macOS-only — Windows/Linux keep the exact original arrangement:
- A throwaway `tk.Tk()` (a `Frame`, then immediately destroyed, `mainloop()` never called) runs *before* `pystray.Icon(...)` is constructed, satisfying constraint 2.
- `icon.run_detached(setup=setup)` (a real `pystray` API "for integrating with other libraries requiring a mainloop") replaces `icon.run(setup=setup)` — pystray's own event handling moves to a background thread and this call returns immediately instead of blocking.
- `setup()` no longer starts `overlay_thread` on macOS; instead, `run_overlay(...)` is called directly, blocking the real calling (main) thread with its own `mainloop()` — satisfying constraint 1. Shutdown/cleanup (`stop_event.set()`, thread joins) is adjusted to match: `overlay_thread` was never started on macOS, so it's not joined either — `run_overlay()`'s own direct call has already returned (and fully torn down its Tcl interpreter) by the time execution reaches that point.

Verified live end-to-end after the fix: the real app runs stably for several minutes with no crash, the tray icon actually renders in the real macOS menu bar (confirmed via screenshot), and clean shutdown leaves no orphaned process.

### 5.6 Autostart ("start with Windows")

- `installer.py:129-149` (`create_shortcut`) — shells out to `powershell -Command` running a `WScript.Shell` COM script to write a `.lnk`.
- `installer.py:157-161` (`get_startup_shortcut_path`), `152-155` (`get_desktop_shortcut_path`) — `%APPDATA%\...\Startup`, Desktop `.lnk` paths.
- `autostart_script.py:472-479`, `482-536` (`get_startup_shortcut_path`, `is_startup_shortcut_enabled`, `set_startup_shortcut_enabled`) — same `.lnk`/PowerShell mechanism, duplicated for the in-app Settings toggle (gated on `sys.frozen`, i.e. doesn't work running from source today either — worth keeping that same gating logic, just behind the new abstraction).
- **Linux**: XDG autostart — write a `.desktop` file to `~/.config/autostart/OBSAutoRecorder.desktop`. No elevation needed.
- **macOS**: LaunchAgents — write a `.plist` to `~/Library/LaunchAgents/com.obsautorecorder.plist`, loaded via `launchctl load`. No elevation needed for a per-user agent.

### 5.7 Self-restart

- `autostart_script.py:8203-8223` (`do_restart`) — the `subprocess.Popen([sys.executable, ...])` relaunch pattern itself is portable. The `TCL_LIBRARY`/`TK_LIBRARY`-stripping workaround exists specifically for a PyInstaller-onefile-on-Windows temp-extraction race — **re-verify whether this recurs on Linux/macOS onefile builds** rather than assuming it's Windows-only; if the Linux/macOS builds use `--onedir` (as recommended in §7 for the same antivirus-adjacent reasons that drove Windows to onedir — though Linux/macOS don't have the identical AV-scanning failure mode, onedir is still simpler and faster to start), this workaround may become dead code on those platforms specifically. Confirm empirically, don't assume.

### 5.8 `CREATE_NO_WINDOW` subprocess flag

- 11 call sites: `autostart_script.py:527, 1419, 1647, 2840, 2904, 3011, 3105, 3393, 3586, 3668, 3713` (verify exact lines at implementation time — several will have shifted from other changes in this plan). All need `subprocess.CREATE_NO_WINDOW` replaced with `**platform_common.hide_console_subprocess_kwargs()` (or equivalent) — raises `AttributeError` today if ever reached with an unconditional reference on non-Windows.

### 5.9 Already fully portable — no changes needed

- Disk usage / storage cleanup: `autostart_script.py:2109-2119`, `2150-2204` (`shutil.disk_usage`, `os.remove` — no OS-specific code at all).
- Config/log file paths: `autostart_script.py:25-29` (relative to executable, not `%APPDATA%`-based — good foundation, keep as-is).
- OBS hang-detection/recovery logic: `autostart_script.py:806-827` (`ensure_obs_ready`) — WebSocket-unresponsive heuristic, no Win32 API involved.
- Tray icon: `pystray` (`autostart_script.py:22, 82-88, 8336-8368, 8398`) already backend-abstracts Windows/macOS/Linux. **Environment caveat, not a code issue**: Linux (especially GNOME/Wayland) needs the AppIndicator/KStatusNotifierItem extension installed for a tray icon to appear at all, and `python3-gi`/GTK system packages aren't pip-installable — document this as a Linux runtime prerequisite, nothing to fix in code.
- Notifications: `autostart_script.py:2745-2751` (`notify`) — uses `pystray`'s own `Icon.notify()`, which forwards to each OS's native mechanism. Linux's `xorg` pystray backend has no notification support at all; the existing try/except around `icon.notify()` already swallows that failure gracefully — no code change needed, just confirm the silent-degradation behavior is still acceptable off Windows.
- Process management for OBS: `autostart_script.py:539-551` (`launch_obs`), `399-401`/`417-425` (process check/kill) — plain `Popen`/`psutil`, no Windows-only calls (`CREATE_NO_WINDOW` aside, covered in §5.8).

### 5.10 UI copy / cosmetic Windows-centric text (low risk, but numerous — needs a pass)

- `autostart_script.py:5363` — "Allowed drives (comma-separated, e.g. C, D)" label; ties to the `allowed_drives` redesign in §5.3.
- `autostart_script.py:4602` — "exact exe name, e.g. cs2.exe" placeholder copy.
- `autostart_script.py:4668` — "javaw.exe" example in window-title-rule help text.
- `autostart_script.py:5979` — "Show Windows toast notifications..." checkbox label names Windows explicitly even though the underlying mechanism (`pystray`) is cross-platform.
- `autostart_script.py:4424` — file-browse dialog's default filter is `[("Executable", "*.exe"), ("All files", "*.*")]`, misleading on Linux/macOS where binaries have no extension.
- Proposal: make this copy OS-aware via the same `platform_common` layer (e.g. a `platform_common.example_executable_name()` helper returning `"cs2.exe"`/`"cs2"` as appropriate) rather than hardcoding one OS's convention into shared UI strings.

### 5.11 Test suite coupling (see §7.3 for the fix strategy)

- All 12+ files under `tests/` fail to collect at all today on non-Windows (§5.1's blocker).
- `tests/test_custom_keybinds.py` — patches `a.ctypes.windll.user32` directly (lines ~116, 191) to test the hotkey listener; will need an equivalent for whichever backend is under test once §5.4 lands, following the same "mock the OS call, assert on the logic" pattern.
- `tests/test_vlc.py:14-67` — patches `a.winreg.OpenKey`/`QueryValueEx` directly for VLC path discovery; same treatment once `find_vlc_executable_or_libvlc` moves behind the platform layer.
- `tests/test_detection.py`, `tests/test_quick_setup.py`, `tests/test_installer.py`, `tests/fakes.py`, others — hardcoded Windows-style path/`.exe` fixtures throughout (e.g. `r"D:\SteamLibrary\...\Balatro.exe"`, `r"C:\OBS\obs64.exe"`). These aren't broken by porting per se (they're just fixture data), but should get platform-parameterized equivalents (a Linux-path and macOS-path variant of the same test) rather than staying Windows-only assertions once the functions they test support multiple OSes.

## 6. Phase 6 (audio isolation) — why it's flagged separately as highest-risk

"Game Audio isolation" today configures OBS's `wasapi_process_output_capture` input type — a Windows-only WASAPI feature. There is no drop-in equivalent:

- **Linux**: OBS 28+ supports per-application audio capture via PipeWire (when the desktop's audio server is PipeWire, which is now the default on most modern distros — but not universal, e.g. older/minimal distros may still run plain PulseAudio without the PipeWire compatibility layer's per-app routing). The exact OBS input `kind` string and its settings schema need to be **confirmed empirically against a real OBS install** (via `obs-websocket`'s `GetInputKindList`/`GetInputDefaultSettings` calls, the same technique this app could use at runtime to self-discover the right schema rather than hardcoding a guess) — do not hardcode a source-type name from documentation alone without verifying it against a live OBS instance in CI.
- **macOS**: per-app audio capture is newer and version-gated — reliable support needs Apple's Core Audio process taps (`AudioHardwareCreateProcessTap`, new in macOS 14.4 Sonoma) and a recent OBS build (the "Application Audio Capture" source OBS added on macOS is built on this, landing around OBS 30.x) — this superseded an earlier assumption in this plan that ScreenCaptureKit (macOS 13+) was the mechanism, and drove raising the whole app's macOS floor to 14.4 (§2 item 6) rather than gating just this one feature. Same empirical-verification approach applies, though real verification here is blocked in CI specifically — see §6.1's macOS finding: GitHub's macOS runners can't get a real OBS instance to finish launching at all, regardless of version, so the exact source `kind` string still needs confirming via the runtime capability-probe approach below (on real hardware) rather than CI.
- **Recommended approach**: rather than hardcoding source-type strings per OS, add a small runtime capability probe (`client.get_input_kind_list()` via obs-websocket) that the app queries once at startup on Linux/macOS, matching against a small preference-ordered list of known-plausible per-app-capture input kinds, logging clearly which one (if any) it found — this turns "we guessed wrong about OBS's exact source-type name" from a silent feature failure into a clear, debuggable log line, and survives future OBS version changes better than a hardcoded string.

### 6.1 Research spike results (real CI findings, not guesses)

Ran per this section's own instruction, via `.github/workflows/obs-audio-spike.yml` + `ci/obs_audio_capability_spike.py`: install a real, current OBS (official PPA on Ubuntu, since Ubuntu's own repo build is too old to have obs-websocket v5 built in; Homebrew cask on macOS), pre-seed obs-websocket's `config.json` so its server starts enabled with zero GUI interaction, launch OBS, and query `GetInputKindList`/`GetInputDefaultSettings` for real. Two genuine, confirmed findings, both **environmental limitations of the CI runner, not code bugs** — exactly the "full parity isn't achievable in some environment" outcome this section says to document rather than push past:

- **Linux — confirmed negative result: stock OBS has no per-app audio capture on this build/environment.** The connection/config-seeding mechanism itself works cleanly: OBS 32.2.0 launches under Xvfb and obs-websocket accepts a connection on the very first attempt, and a real PipeWire session (`pipewire`/`wireplumber`/`pipewire-pulse` under a `dbus-launch`-provided persistent session bus) is confirmed running. `GetInputKindList` only ever reports generic whole-device capture kinds (`alsa_input_capture`, `pulse_input_capture`, `pulse_output_capture`, `jack_output_capture`, plus `pipewire-camera-source` for video) — no distinct per-app-capture kind. The follow-up spike settles the "maybe it's a runtime dropdown option instead" theory: with a real audio-producing process running (`speaker-test`, a continuous sine tone) and a genuine `pulse_output_capture` input created via `create_input`, `GetInputPropertiesListPropertyItems` on that input's `device_id` field returns only `["Default", "Dummy Output (auto_null.monitor)"]` — **no per-application entry at all**, even with real audio actively playing. `pulse_input_capture`'s own property list came back empty. This is a genuine negative result, not an inconclusive one: this OBS build (official PPA, version 32.2.0) does not expose per-application audio capture through any input kind it ships, at least not on this runner's audio configuration (a bare `auto_null` dummy sink, no real hardware output). The plan's original assumption ("OBS 28+ supports per-application audio capture via PipeWire") needs to be re-examined against OBS's actual current plugin/feature docs — it's possible this needs a separate, non-default plugin, or was never accurate for stock OBS to begin with, rather than something this app's own code is missing.
- **macOS — blocked, real OBS won't complete startup headless on this CI runner.** OBS's own process launches and stays alive (confirmed via a `ps aux` check at +10s and +30s: same PID, ~0% CPU throughout, no growth) but produces no further log output past its initial macOS permission checks (`audio device access granted`, `video device access denied`, `input monitoring granted`, `screen capture granted`) — no version/hardware-detection/module-loading lines like Linux's run shows immediately, no crash report in `~/Library/Logs/DiagnosticReports` either. This is a genuine hang, not a crash: OBS is a native Cocoa/Metal GUI app and most likely blocks waiting for a real WindowServer session to attach its GUI to, which GitHub's macOS runners don't provide the way Xvfb provides Linux a working (if virtual) X11 display. **This CI mechanism cannot verify anything about OBS's real behavior on macOS at all** — confirming the macOS per-app-audio-capture kind name needs either a self-hosted macOS runner with a real logged-in GUI session, or a human on real Mac hardware. Flagged as blocked, not attempted further, consistent with this document's existing macOS real-hardware-verification gaps (Accessibility permission, the NSView embedding bridge).

**Implication for implementation**: `configure_process_audio_capture` should not be written for Linux via this path — the confirmed negative result means stock OBS (as installed via the official PPA) has no per-app audio capture to configure at all, on any input kind it ships. Before writing any Linux implementation, this needs either (a) confirming against OBS's current official docs/release notes whether per-app PipeWire capture ships in stock OBS at all or needs a separate plugin this app would need to detect and instruct the user to install, or (b) re-running this same spike on a real desktop Linux machine with a full PipeWire session (real hardware output, a real desktop environment) in case this CI runner's minimal/dummy-sink audio setup specifically suppressed it. macOS's CI block is resolved by real-hardware verification instead — see §6.2. Linux stays open follow-up work rather than shipped as guessed, unverified code.

### 6.2 macOS: confirmed on real hardware, implemented

Per §6.1's own conclusion ("needs either a self-hosted macOS runner with a real logged-in GUI session, or a human on real Mac hardware"), this was run for real on a Mac (Apple Silicon, macOS 15.5 Sequoia, OBS 30.2.3, obs-websocket 5.5.2) rather than attempted again in CI. Same technique as the CI spike (pre-seed `obs-websocket`'s config, launch OBS, query `GetInputKindList`/`GetInputDefaultSettings`/`GetInputPropertiesListPropertyItems` for real), plus one extra step CI could never reach at all: actually creating a real input and reading its live property list.

**Two real environmental blockers hit before any of this would even respond, both worth recording since they'll bite the next person too:**
- OBS returned error 207 ("not ready") on *every* request, indefinitely, immediately after a clean launch — not a startup race (retried for over a minute). OBS's own log showed all four macOS privacy permissions denied (Microphone, Camera, Accessibility, Screen Recording) at the very top. Granting them (System Settings → Privacy & Security) is a manual, human-only step — macOS deliberately blocks any script/app from granting another app's TCC permissions, so this cannot be automated around. Screen Recording specifically also gates Core Audio process taps, the exact API this feature depends on.
- Separately, `global.ini`'s `FirstRun=true` is a documented cause of the same persistent 207 (an invisible Auto-Configuration Wizard blocking readiness under `--minimize-to-tray`) — worth ruling out first since it's the one part of this that *can* be fixed without a human, but it did not turn out to be the actual cause here; the permissions were.

**Findings, once OBS was actually able to respond:**
- `GetInputKindList` includes `sck_audio_capture` (ScreenCaptureKit-based — the actual mechanism, confirming §6's superseding note that this is ScreenCaptureKit/Core-Audio-process-tap based, not a separate API). `GetInputDefaultSettings` gives `{"application": "", "type": 0}`.
- `GetInputPropertiesListPropertyItems(name, "type")` on a real created input confirms this is a two-value enum: `0` = "Desktop Audio Capture", `1` = "Application Audio Capture". The `"application"` property's own live item list is **empty at type 0** and only populates at **type 1** — confirmed against real running apps, e.g. `{"itemName": "Discord", "itemValue": "com.hnc.Discord"}`. The value is a **bundle identifier**, not a process/executable name — a meaningfully different identification scheme than Windows's `wasapi_process_output_capture` (which matches by exe name directly), so the macOS implementation resolves a running process's `psutil`-reported `exe` path up to its owning `.app` bundle's `Info.plist` and reads `CFBundleIdentifier` out of it (`platform_macos.resolve_bundle_identifier`), rather than using the process/executable name at all.
- **A real footgun, not just a documentation gap**: passing `type` anything other than 0 or 1 (tried once, with `2`, purely to check whether a third mode existed) crashed OBS's entire process outright — no crash report, no error surfaced over the websocket first, the process just disappeared. `platform_macos.py`'s `SCK_AUDIO_CAPTURE_TYPE_APPLICATION` constant exists specifically so this value is never typed in more than once, anywhere.

**Implemented**: `platform_common.process_audio_capture_kind()` / `process_audio_capture_settings()`, dispatching to `platform_windows.py` (unchanged behavior, exe-name `"window"` match) and the new `platform_macos.py` (`sck_audio_capture` + bundle-identifier resolution) backends; `platform_linux.py` returns `None`/`None`, matching §6.1's confirmed negative finding rather than leaving the function missing. `autostart_script.py`'s `set_game_audio_capture_target` and `compute_quick_setup_tracks` (the multi-track quick-setup wizard) both go through this dispatch now instead of a hardcoded Windows-only kind string; `AUDIO_TRACK_MANAGED_KINDS` and `INPUT_KIND_LABELS` recognize `sck_audio_capture` too. Real unit tests (`tests/test_platform_macos.py`, `tests/test_platform_windows.py`, `tests/test_platform_linux.py`, `tests/test_platform_common.py`) are keyed on this actual observed schema, not a guess.

**Known follow-up, deliberately not guessed at**: `DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS` (-27ms) was measured specifically for Windows's WASAPI loopback-vs-process-capture timing difference; it has no empirical basis on ScreenCaptureKit's completely different capture pipeline and may not even have the same sign, let alone magnitude. Left as-is (still just a *default*, always overridable via `obs.process_audio_capture_sync_offset_ms` or this app's own live calibration flow) rather than invented for macOS with no measurement behind it — a real cross-correlation calibration run on a Mac is the correct way to find that number, not a guess here. `run_audio_sync_calibration`'s own current implementation is also unverified on macOS (its docstring's ffplay/visible-window assumption was written against Windows's process-capture behavior specifically) and should be checked before relying on it there.

## 7. "Simulate other OSes" — concrete plan

1. **CI matrix**: extend `.github/workflows/build-release.yml` (or split into a separate `test.yml` that runs on every push/PR, distinct from the release-build workflow) to run the test suite on `windows-latest`, `macos-latest`, and `ubuntu-latest`. This alone catches import-time errors, platform-dispatch logic bugs (via the mocking pattern in §3.2), and packaging issues per §7.4.
2. **Headless Tkinter on Linux CI**: `ubuntu-latest` has no display server by default; wrap test invocation in `xvfb-run -a python -m pytest` (Xvfb is preinstalled on GitHub's Ubuntu runners) so GUI-constructing tests (the `verify_ui.py`-style smoke tests used throughout this project's own development) can run there too, not just the pure-logic tests.
3. **Docker for local Linux-distro variation (optional, since you're on Windows)**: Docker Desktop on Windows can run Linux containers directly. A `Dockerfile.test-ubuntu` / `Dockerfile.test-fedora` (or a docker-compose matrix) gives a fast local loop for catching distro-packaging differences (different Python versions, different system GTK/tray dependencies) without waiting on CI. Not required — CI alone covers correctness — but cheap to add and genuinely useful given your setup.
4. **The mocking-based logic simulation from §3.2** is the mechanism that lets you (on Windows) meaningfully exercise "what would the Linux/macOS code do here" without waiting for a CI round-trip at all, for anything that's pure logic rather than a real OS/filesystem/window-system call.
5. **What CI genuinely can't simulate**: real GUI rendering fidelity (does the clip editor actually look right on macOS), real global-hotkey behavior against a real X11 session's window manager, real VLC-embedding visual correctness, real Accessibility-permission prompts on macOS. These need a human on real hardware eventually — explicitly deferred per your "CI-only for now" answer, but worth a follow-up plan once you (or a tester) has access to real Linux/macOS machines.

### 7.4 Packaging differences CI will surface

- Linux: does the AppImage actually launch on a stock Ubuntu container with no extra packages installed? Are the `python3-gi`/GTK tray dependencies actually present, or does pystray need a fallback/clear error?
- macOS: does a universal2 PyInstaller build actually succeed given every dependency (`numpy`, `Pillow`, `python-vlc`, `psutil`, `pystray`, `obsws-python`) needs a universal2 wheel available? **Answered, via the real `build-release.yml` build itself (not a separate spike): no.** `numpy` does not publish a `universal2`-tagged wheel on PyPI at all for the version this project resolves to — confirmed live via `pip download --platform macosx_11_0_universal2 numpy`, which fails with "no matching distribution," not a PyInstaller-side error. (Pillow separately needed `--exclude-module PIL._avif`, an unused optional AVIF codec that ships arm64-only even where Pillow's own core extension is fine — a real but much smaller issue, superseded by the numpy finding either way.) Building numpy from source for both architectures and merging the results by hand would be possible in principle but is real, dedicated packaging work of its own, not attempted here. **Result: macOS ships an Apple Silicon (arm64)-only build for now** (GitHub's `macos-latest` runner's own native arch) — the exact fallback this bullet already named as acceptable. Real Intel Mac support needs either an x86_64 runner/cross-build (e.g. via Rosetta 2) or building on real Intel hardware, tracked as follow-up rather than blocking this phase.

## 8. Phased implementation plan

Each phase lists: goal, concrete steps, dead code this phase creates (to delete, not leave behind), and exit criteria. Phases are ordered so each one only depends on earlier ones — implementable start-to-finish without needing another round of questions, per your request.

### Phase 0 — Unblock imports, establish the platform layer skeleton, stand up CI

**Steps:**
1. Create `platform_common.py`, `platform_windows.py`, `platform_linux.py`, `platform_macos.py` (empty backends to start, filled in over later phases).
2. Move `import winreg` and `from ctypes import wintypes` out of `autostart_script.py`'s top-level scope — either into `platform_windows.py` (preferred, since that's where the code using them is headed anyway) or guarded behind `if sys.platform == "win32":` if a full move isn't ready yet. Same for `installer.py:24`.
3. Add `hide_console_subprocess_kwargs()` to `platform_common.py`; replace all 11 `CREATE_NO_WINDOW` sites (§5.8).
4. Add `screeninfo` to `requirements.txt`; replace `get_monitor_rects()` (§3.3) — this single swap removes an entire Win32 ctypes block (`autostart_script.py:91-169`'s monitor-enumeration portion specifically, not the window-title portion which stays) with zero per-OS code needed.
5. Add a `test.yml` GitHub Actions workflow running `pytest` on the 3-OS matrix (§7.1), with Xvfb wrapping on Linux (§7.2). It's fine — expected — for it to be mostly red on Linux/macOS at the end of this phase; the goal here is just that tests **collect** (no `ModuleNotFoundError`) everywhere, not that they all pass yet.

**Dead code created:** none yet — this phase is additive/relocating, not replacing behavior.

**Exit criteria:** `pytest` collects (runs, regardless of pass/fail count) successfully on all 3 CI runners. Windows test suite still 100% green (no regression).

### Phase 1 — OBS / ffmpeg / VLC discovery

**Steps:**
1. Implement `find_obs_executable`, `find_ffmpeg_executable`, `find_ffprobe_executable`, `find_vlc_executable_or_libvlc` in all 3 `platform_*.py` modules per §3.3/§5.2.
2. Fix the `ffprobe.exe` hardcoded-suffix bug (§5.2) as part of this — it's a real bug on Windows too (breaks if `ffmpeg` was found via `shutil.which` pointing somewhere `ffprobe.exe` isn't co-located), not purely a porting concern.
3. Wire `installer.py` and `autostart_script.py` to call through `platform_common` instead of their current inline implementations.
4. Update `config.example.json`'s `obs.path`/`obs.process_name` Windows-specific defaults to be resolved via the platform layer at config-generation time (installer) rather than baked into the shipped JSON as a Windows string.

**Dead code to delete:** `installer.py:34-37` (`DEFAULT_OBS_PATHS`), `164-181` (`find_obs_exe`), `64-83` (`is_ffmpeg_installed`'s Windows candidate list) — all superseded by calls into `platform_windows.py`. `autostart_script.py:2776-2802` (`find_ffmpeg`), `2805-2817` (`resolve_ffmpeg_path`'s inline Windows logic — keep the function as a thin wrapper if other code calls it by that name, but its body moves), `2859-2862` (`find_vlc_path`'s registry block).

**Exit criteria:** on Linux/macOS CI, `find_obs_executable`/`find_ffmpeg_executable`/`find_vlc_executable_or_libvlc` unit tests pass (mocked filesystem, per §3.2) even without OBS/ffmpeg/VLC actually installed on the runner. Windows behavior unchanged (existing tests still green).

### Phase 2 — Game / window / process detection

**Steps:**
1. Port `get_steam_install_path`/`get_steam_common_dirs` — Linux (`~/.steam/steam`, `~/.local/share/Steam`, Flatpak `~/.var/app/com.valvesoftware.Steam/.steam/steam`), macOS (`~/Library/Application Support/Steam`). Reuse the existing VDF-parsing code unchanged (§5.3).
2. Redesign `allowed_drives` as a path-prefix/mount-point filter that works identically on all 3 OSes (e.g. a list of allowed root directories instead of drive letters) — needs a config-schema migration note (old `allowed_drives: ["C","D"]` values need a defined fallback/ignore behavior on read, not a crash, for users upgrading an existing config).
3. Port `get_epic_installed_games` to macOS (real port, same `.item` manifest parsing, macOS manifest path — §2.5/§5.3) and `get_battlenet_install_dirs` to macOS (default paths/copy only). Confirm `get_xbox_install_dirs`/`get_gog_installed_games` return `[]` cleanly on both non-Windows OSes, and `get_epic_installed_games`/`get_battlenet_install_dirs` return `[]` cleanly on Linux specifically — these three are intentionally no-op backends where no native client exists, not bugs.
4. Implement `get_window_titles` for X11 (`python-xlib`, new dependency) and macOS (`pyobjc-framework-Quartz`, new dependency); Wayland detection + graceful empty-result + one-time logged notice.
5. Split `COMMON_GAMES` into OS-aware variants per §5.3 (only genuinely cross-platform titles get Linux/macOS entries).
6. Update the Settings UI copy flagged in §5.10 to be OS-aware.

**Dead code to delete:** none of `find_target_process`/`find_game_by_install_dir`/`is_exe_under_dirs` (these stay as shared, unmodified logic per §5.3) — this phase is pure additive-backend work plus the `allowed_drives` redesign, which does replace `autostart_script.py:206-207`'s `os.path.splitdrive` block.

**Exit criteria:** Steam-based game detection works end-to-end on Linux CI (a CI step installs/fakes a minimal Steam library layout and confirms detection). Window-title-based rules (Minecraft-style) work on `ubuntu-latest` under Xvfb+X11 (not Wayland). Windows behavior unchanged.

### Phase 3 — Global hotkeys

**Steps:**
1. Implement `register_global_hotkey`/`unregister_global_hotkey` for X11 (`python-xlib`, `XGrabKey`) — reuses the `python-xlib` dependency added in Phase 2.
2. Implement the same for macOS via `pyobjc`'s Quartz `CGEventTap`, including an Accessibility-permission detection-and-guidance flow (check permission status, show a clear in-app message with a link to System Settings if not granted, rather than a silently-dead hotkey).
3. Port the clip editor's space-bar mechanism (`run_clip_editor_space_bar_listener`, `is_space_bar_toggle_event`, `start_space_bar_listener`) onto the same X11/Quartz backends, reusing the foreground-window-check pattern (§5.4) adapted per OS (`_NET_ACTIVE_WINDOW` on X11, `NSWorkspace.frontmostApplication` on macOS).
4. Wayland: `register_global_hotkey` returns a clear "unsupported on Wayland" result; calling code (the custom-keybind settings UI, the clip editor) shows this to the user instead of silently failing.

**Dead code to delete:** none — `run_custom_keybind_listener`/`run_clip_editor_space_bar_listener` stay as the Windows backend's implementation, now living in `platform_windows.py` (moved wholesale from `autostart_script.py`, not duplicated).

**Exit criteria:** a custom keybind fires correctly on `ubuntu-latest` under Xvfb+X11 in an automated test (synthesize a key event the same way this project's own `verify_space_bar_hook.py`-style live test did for Windows earlier). Space bar toggles play/pause in the clip editor on Linux X11. macOS: at minimum, the Accessibility-permission-not-granted path is tested (CI can't grant real Accessibility permission, so the "happy path" likely needs deferred real-hardware verification — flag this explicitly rather than claiming full macOS hotkey coverage from CI alone).

### Phase 4 — Clip editor VLC embedding

**Steps:**
1. Implement `embed_video_player` for Linux (`player.set_xwindow(tk_widget.winfo_id())`) — should be close to a direct swap of the Windows call.
2. Implement for macOS (`player.set_nsobject(...)`) — needs the PyObjC NSView-extraction bridge flagged in §5.5; budget real iteration time against macOS CI output here, this is not a confident one-shot port.
3. Update all call sites that currently assume `set_hwnd` (just the one at `autostart_script.py:6999`, but re-verify nothing else in the clip editor assumes an HWND specifically).

**Dead code to delete:** none — `set_hwnd` becomes the Windows branch inside `embed_video_player`.

**Exit criteria:** clip editor opens and shows a rendering VLC surface (even if just confirmed via a screenshot artifact from CI, not full playback verification) on `ubuntu-latest` under Xvfb. macOS: flagged as needing real-hardware confirmation per the PyObjC bridge's inherent uncertainty (§5.5) — CI can validate the code runs without crashing, not that the video visually renders correctly.

### Phase 5 — Autostart

**Steps:**
1. Implement `enable_autostart`/`disable_autostart`/`is_autostart_enabled` for Linux (XDG `.desktop` file) and macOS (LaunchAgents `.plist` + `launchctl load`/`unload`).
2. Wire both `installer.py` and `autostart_script.py`'s in-app Settings toggle through the same 3 functions (removing the duplication noted in §5.6 as part of this move, not preserving it).

**Dead code to delete:** `installer.py:129-149` (`create_shortcut`), `152-161` (shortcut path helpers) move into `platform_windows.py` wholesale; `autostart_script.py:472-479, 482-536` likewise move (not duplicate) into `platform_windows.py`, called from both the installer and the main app through the same `platform_common` functions — this actually **reduces** total code versus today's two separate Windows-only implementations.

**Exit criteria:** `is_autostart_enabled()`/`enable_autostart()`/`disable_autostart()` round-trip correctly in a unit test on all 3 CI runners (Linux/macOS can genuinely write and read back a real `.desktop`/`.plist` file in CI, unlike hotkeys/video-embedding which need a real display/window manager).

### Phase 6 — Audio isolation (Linux PipeWire / macOS CoreAudio)

Per §6: this phase starts with a research spike (confirm real OBS source-type schemas empirically, ideally via a CI job that actually installs OBS on `ubuntu-latest`/`macos-latest` and queries `GetInputKindList` for real) before any implementation, since hardcoding a guessed source-type name is worse than not having the feature — it would silently mis-configure OBS rather than falling back cleanly to "not supported yet" the way §5.9's notification-degradation already does gracefully.

**Steps:**
1. ~~CI spike: install real OBS on `ubuntu-latest`/`macos-latest`, launch it headless, connect via `obsws-python`, call `GetInputKindList`, record the actual available per-app-audio-capture input kind names and their `GetInputDefaultSettings` schema.~~ **Done** — see §6.1.
2. ~~Follow-up Linux spike: create a real `pulse_output_capture` input via obs-websocket, start a test process actually producing audio, then query that input's `device_id` property list to see whether a per-application entry actually appears.~~ **Done** — confirmed negative (§6.1): no per-application entry appears even with real audio playing, on this OBS build/environment.
3. ~~macOS: blocked on CI entirely per §6.1 — needs either a self-hosted macOS runner with a real logged-in GUI session, or a human on real Mac hardware, neither available in this environment.~~ **Done, on real Mac hardware** — see §6.2.
4. Before writing any Linux implementation: confirm against OBS's current official docs/release notes whether stock OBS ships per-app PipeWire audio capture at all, or whether it needs a separate plugin — the spike's negative result could mean either "this feature doesn't exist in OBS the way the original plan assumed" or "this CI runner's minimal dummy-sink audio setup specifically suppressed it" (worth one confirmation on a real desktop Linux machine with a full PipeWire session before concluding the former). **Not started.**
5. ~~Implement `configure_process_audio_capture` per OS once real-hardware testing (macOS) actually confirms a real, working mechanism, with the capability-probe-and-log-clearly fallback design from §6 (not a hardcoded assumption).~~ **Done for macOS** — see §6.2 (`platform_common.process_audio_capture_kind()`/`process_audio_capture_settings()`). Linux stays **not started**, blocked on step 4.
6. ~~Wire into the same higher-level multi-track-audio-profile setup flow the Windows WASAPI path already uses.~~ **Done** — `set_game_audio_capture_target` and the multi-track quick-setup wizard (`compute_quick_setup_tracks`) both go through the same OS-dispatched functions now, not a hardcoded Windows-only kind string.

**Dead code to delete:** none — this is new backend code alongside the existing Windows WASAPI implementation, which moves into `platform_windows.py` unchanged.

**Exit criteria:** the CI spike from step 1 produces a concrete, verified answer (not a guess) about what OBS actually calls this feature on Linux/macOS, documented back into this plan file before implementation proceeds. This is explicitly allowed to reveal that full parity isn't achievable on some OS/OBS-version combination — if so, document that clearly rather than shipping a broken feature. **Status: met.** Both spikes (Linux in CI, macOS on real hardware after CI confirmed it couldn't reach this at all) ran for real and produced genuine, concrete findings rather than a guess: Linux's stock OBS build exposes no per-application audio capture on any input kind it ships (confirmed with real audio playing against a real, running PipeWire session, not just inferred from a static schema) — implementation stays paused there per step 4 above, exactly the "document clearly rather than ship a broken feature" outcome this section anticipates. macOS is now implemented and empirically confirmed (§6.2), closing out the one part of Phase 6 that was still open.

### Phase 7 — Packaging, installers, CI release matrix

**Steps:**
1. ~~Split `.github/workflows/build-release.yml`'s single Windows job into a 3-OS matrix (`windows-latest`/`macos-latest`/`ubuntu-latest`), each building its native artifact. Fix the Windows-only `Compress-Archive` (PowerShell cmdlet) and PyInstaller `--add-data` separator (`;` vs `:`) to be conditional per-OS in the workflow (§5's build-config findings).~~ **Done.** A separate `release` job (`needs: build`) now collects every OS's artifacts into one combined GitHub release, replacing the old single-OS inline release step.
2. ~~Linux: PyInstaller `--onedir` build, packaged as an AppImage (via `appimagetool`) plus a plain `.tar.gz` fallback, per §2's decision.~~ **Done** — confirmed building successfully via a real CI run.
3. ~~macOS: PyInstaller universal2 build...~~ **Done, with the §7.4 fallback**: universal2 confirmed NOT possible (numpy publishes no universal2 wheel at all) via the real build itself, not a separate spike — ships as an Apple Silicon (arm64)-only `.app`/`.dmg` for now; see §7.4 for the full finding.
4. ~~Rewrite `installer.py`'s per-OS steps (find OBS, configure autostart, install ffmpeg) to call through `platform_common`...~~ **Done** for OBS-finding (already Phase 1), autostart (already Phase 5), and ffmpeg/OBS dependency install (this phase, step 6 below). The wizard's shared logic (collect preferences, build `config.json`, orchestrate steps, the Tk UI itself) stays one codebase across all 3 installers, as intended — no separate per-OS installer.py forks were created.
5. ~~Default Linux/macOS installs to a per-user location (§2.8)~~ **Done** — `installer.py`'s `DEFAULT_INSTALL_DIR` is now OS-aware (`~/.local/share/OBSAutoRecorder` on Linux, `~/Applications/OBSAutoRecorder` on macOS), needing no elevation for the app install itself on either OS.
6. ~~ffmpeg/OBS dependency install: macOS via Homebrew detection... Linux gets the transparent "detect package manager, show the exact command, run it via `pkexec`..." flow...~~ **Done.** `platform_common.install_optional_dependency` (Windows/macOS) and `build_linux_install_command`/`run_linux_install_command` (Linux) implement this; the installer's "Locate OBS Studio" page has the new read-only command field + "Run this command" button on Linux. The in-app Settings ffmpeg/VLC "Easy Install" buttons (a separate, pre-existing UI in `autostart_script.py` itself, not just the installer) got the same Windows/macOS Homebrew/winget treatment but were **not** extended to Linux's command+button UI — they simply don't offer a quick-install path on Linux (same as before this phase, when they only worked on Windows at all), a deliberately bounded scope decision rather than an oversight; users can still install ffmpeg/VLC manually via their own terminal there.

**Dead code to delete:** none new here beyond what earlier phases already flagged — this phase is primarily CI/build-config and `installer.py`'s orchestration layer, not `autostart_script.py` internals. (Removed as genuinely dead: `installer.py`'s old `has_winget`/`winget_install`/`OBS_WINGET_ID`/`FFMPEG_WINGET_ID`, and `autostart_script.py`'s old `FFMPEG_WINGET_ID`/`VLC_WINGET_ID` module constants, all superseded by `platform_common.install_optional_dependency`.)

**Exit criteria:** all 3 installers produce a working install from a single CI run on a tagged release, verified by each installer's own smoke test (install → confirm the app launches → uninstall cleanly) running on its native CI runner. **Status: partially met.** All 3 OSes' `build-release.yml` build succeeds end-to-end via a real, manually-triggered CI run (this is real verification, not aspirational) — confirmed only after fixing two real macOS-specific packaging bugs it surfaced along the way: Pillow's `_avif`/core `_imaging` extensions and numpy publishing no universal2 wheel at all (→ arm64-only fallback, §7.4), and PyInstaller's own codesigning pass choking on the `.app` bundle's Mach-O binary when embedded as plain installer data (→ embed it as a zip instead, extracted at install time). Windows produces a zip + installer exe; Linux a tar.gz, an AppImage, and an installer binary; macOS an arm64 `.app`/`.dmg` and an installer binary. The full "install → launches → uninstall cleanly" smoke test itself is **not implemented** (would need real interactive install-wizard automation per OS, e.g. via a GUI-automation tool, which this phase didn't attempt) — the CI verification that did happen is "does the build itself succeed AND produce artifacts with no known-broken embedding," a real and valuable but narrower check than the exit criteria's original ask. Flagged as follow-up, consistent with how this plan treats other real-hardware/interactive-only gaps (Phase 3's macOS hotkey happy path, Phase 4's NSView bridge).

### Phase 8 — Dead code sweep

A dedicated cleanup pass, done last (after every earlier phase's own "dead code to delete" list has actually been carried out) to catch anything missed:

1. Grep for any remaining direct `ctypes.windll`/`winreg`/`wintypes` references outside `platform_windows.py` — there should be none.
2. Grep for any remaining raw `CREATE_NO_WINDOW` references outside `platform_common.hide_console_subprocess_kwargs()`'s own implementation.
3. Confirm no orphaned config keys from the `allowed_drives` redesign (§8, Phase 2) are silently ignored without a migration note in `README.md`'s config-reference section.
4. Confirm `installer.py` and `autostart_script.py` no longer contain their own copies of anything now living in `platform_windows.py` (Phases 1, 5 especially call out functions that should be **moved**, not duplicated — verify that actually happened rather than leaving a stale, now-unused original behind two names).
5. Re-run the full test suite on all 3 CI runners one more time as the final gate for this plan.

### Phase 9 — Documentation

1. `README.md` gets OS-specific install/setup instructions sections (currently entirely Windows-phrased).
2. This plan file (`CROSS_PLATFORM_PLAN.md`) gets a final pass converting it from "plan" to "how this actually works," or gets archived/linked from `README.md` once superseded by real docs — your call at that point.

## 9. Suggested phase ordering vs. parallelizability

Phases 1 and 2 can happen in either order or in parallel (independent subsystems). Phase 3 depends on Phase 2's `python-xlib` dependency already being added but is otherwise independent of Phase 2's actual detection logic. Phase 4 is fully independent of 1-3. Phase 5 is fully independent of 1-4. Phase 6 is independent but benefits from Phase 1's OBS-connection plumbing being solid first. Phase 7 depends on Phases 0-6 being functionally complete (or at least Phase 0, with later phases' features gracefully degrading, since a working-but-reduced-feature Linux/macOS build is preferable to blocking packaging on 100% parity). Phase 8 depends on everything. Phase 9 can start anytime and finish last.

If working through this with limited time, the highest-value-per-effort ordering is: **Phase 0 → Phase 1 → Phase 2 → Phase 7 (a minimal, reduced-feature Linux/macOS build) → Phases 3/4/5/6 to fill in remaining parity → Phase 8 → Phase 9.** This gets a genuinely usable (if not fully-featured) Linux/macOS build shipping sooner, with hotkeys/video-embedding/autostart/audio-isolation arriving as follow-up parity improvements rather than blocking the first cross-platform release entirely.
