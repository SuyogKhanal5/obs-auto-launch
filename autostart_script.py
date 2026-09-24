import ctypes
import glob
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import obsws_python as obsws
import psutil
import pystray
import screeninfo
from PIL import Image, ImageDraw

import platform_common

if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


MAX_LOG_LINES = 10000


class LineCappedFileHandler(logging.Handler):
    """Appends to a log file, trimming the oldest lines once it exceeds max_lines."""

    def __init__(self, filename, max_lines=MAX_LOG_LINES, encoding="utf-8"):
        super().__init__()
        self.filename = filename
        self.max_lines = max_lines
        self.encoding = encoding

    def emit(self, record):
        try:
            with open(self.filename, "a", encoding=self.encoding, newline="\n") as f:
                f.write(self.format(record) + "\n")
            self._trim_if_needed()
        except Exception:
            self.handleError(record)

    def _trim_if_needed(self):
        with open(self.filename, "r", encoding=self.encoding, errors="replace") as f:
            lines = f.readlines()
        if len(lines) > self.max_lines:
            with open(self.filename, "w", encoding=self.encoding, newline="\n") as f:
                f.writelines(lines[-self.max_lines:])


IDLE_COLOR = (90, 90, 90, 255)
RECORDING_COLOR = (220, 30, 30, 255)
SPLIT_COLOR = (34, 197, 94, 255)
ERROR_COLOR = (230, 160, 20, 255)
SPLIT_FLASH_SECONDS = 5


def current_color(status):
    if status.get("flash_until", 0) > time.time():
        return SPLIT_COLOR
    return RECORDING_COLOR if status["recording"] else IDLE_COLOR


def color_to_hex(color):
    return "#%02x%02x%02x" % color[:3]


def build_tray_image(color):
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = 6
    draw.ellipse((margin, margin, size - margin, size - margin), fill=color, outline=(255, 255, 255, 255), width=4)
    return image


def get_monitor_rects():
    """Cross-platform monitor enumeration via the `screeninfo` package -- this used to be a
    Windows-only ctypes EnumDisplayMonitors call (see CROSS_PLATFORM_PLAN.md Phase 0); screeninfo
    already supports Windows/Linux/macOS itself, so there's no per-OS branch needed here at all.
    Returns [] rather than raising if screeninfo can't detect any display (e.g. a genuinely
    headless CI runner) -- callers already treat an empty monitor list as "no overlay available."
    """
    monitors = []
    try:
        for m in screeninfo.get_monitors():
            monitors.append({"left": m.x, "top": m.y, "right": m.x + m.width, "bottom": m.y + m.height})
    except screeninfo.ScreenInfoError:
        return []

    monitors.sort(key=lambda m: (m["left"], m["top"]))
    for m in monitors:
        m["width"] = m["right"] - m["left"]
        m["height"] = m["bottom"] - m["top"]
        m["label"] = f"{m['width']}x{m['height']} monitor at ({m['left']}, {m['top']})"
    return monitors


def resolve_default_overlay_monitor_index(monitors, overlay_config):
    """Resolves overlay.default_monitor_label (saved as a label like "1920x1080 monitor at
    (0, 0)", since Windows doesn't hand out a stable numeric monitor id) against the monitors
    actually detected this run. Returns None (Off) if unset, or if the saved monitor no longer
    matches any currently connected display -- safer than silently guessing a different one when
    a multi-monitor setup has changed since it was configured."""
    label = overlay_config.get("default_monitor_label")
    if not label:
        return None
    for i, m in enumerate(monitors):
        if m["label"] == label:
            return i
    return None


def default_overlay_monitor_index(monitors):
    """Picks a sensible monitor to auto-select when the overlay needs to turn itself on (e.g.
    enabling the audio mixer levels toggle while no monitor was chosen yet) -- Windows doesn't
    report which monitor is "primary" through EnumDisplayMonitors, but the primary monitor's
    origin is always (0, 0), so that's the most reliable stand-in. Falls back to the first
    monitor (index 0) if none sits exactly at the origin, or None if there are no monitors at all."""
    for i, m in enumerate(monitors):
        if m["left"] == 0 and m["top"] == 0:
            return i
    return 0 if monitors else None


def get_running_processes():
    processes = []
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = proc.info.get("name")
            if name:
                processes.append((name, proc.info.get("exe") or "", proc.pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return processes


def get_window_titles():
    """Maps each visible top-level window's owning PID to a list of that window's titles -- used
    for "window title contains X" game-detection rules (e.g. Minecraft's javaw.exe, whose process
    name alone is too generic to watch for on its own). Thin wrapper -- the real per-OS
    implementation now lives in platform_common.get_window_titles() / platform_windows.py /
    platform_linux.py / platform_macos.py, see CROSS_PLATFORM_PLAN.md Phase 2. Returns {} (not an
    error) when this isn't supported in the current session at all -- e.g. a Wayland desktop,
    which has no unprivileged cross-compositor API for this."""
    return platform_common.get_window_titles()


def get_steam_install_path():
    """Best-effort search for Steam's install root. Thin wrapper -- the real per-OS search (the
    registry on Windows, well-known install locations on Linux/macOS, since Steam ships a native
    client on all 3) now lives in platform_common.find_steam_install_path() /
    platform_windows.py / platform_linux.py / platform_macos.py, see
    CROSS_PLATFORM_PLAN.md Phase 2."""
    return platform_common.find_steam_install_path()


def normalize_allowed_library_roots(allowed_entries):
    """Migrates/normalizes each configured "allowed Steam library root" entry into a real path
    prefix, usable identically on every OS. Pre-redesign configs may still hold bare Windows
    drive letters (the old "allowed_drives": ["C", "D"] scheme, back when this filter only
    understood drive letters) -- those expand to that drive's root (e.g. "C" -> "C:\\") when
    running on Windows, where they're still meaningful, and are dropped (with a one-time log, not
    a crash) everywhere else, since there's no equivalent concept to fall back to on a POSIX
    mount-point layout. Anything else is treated as a real folder path prefix as-is, which is what
    makes this filter work the same way on Linux/macOS (e.g. "/mnt/data", "/Volumes/External")."""
    normalized = []
    for raw in allowed_entries or []:
        entry = str(raw).strip()
        if not entry:
            continue
        is_bare_drive_letter = len(entry.rstrip("\\/:")) == 1 and entry[0].isalpha()
        if is_bare_drive_letter:
            if sys.platform == "win32":
                normalized.append(entry[0].upper() + ":\\")
            else:
                logging.warning(
                    "Ignoring legacy drive-letter Steam library filter '%s' -- drive letters "
                    "aren't meaningful on this OS; use a full folder path instead.", entry,
                )
            continue
        normalized.append(entry)
    return normalized


def get_steam_common_dirs(steam_config):
    if not steam_config.get("enabled", True):
        return []

    install_path = get_steam_install_path()
    if not install_path:
        logging.warning("Could not locate a Steam install on this machine.")
        return []

    library_paths = [install_path]
    vdf_path = os.path.join(install_path, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf_path):
        with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        for match in re.finditer(r'"path"\s+"([^"]+)"', content):
            library_paths.append(os.path.normpath(match.group(1).replace("\\\\", "\\")))

    # A path-prefix filter rather than the old drive-letter-equality one -- works identically on
    # every OS (a POSIX path has no drive letter to match against at all, but a plain string
    # prefix check needs no OS-specific concept), and is opt-in (empty/unset by default) either
    # way. os.path.normcase lowercases on Windows (case-insensitive filesystem) and is a no-op on
    # Linux (case-sensitive), matching each OS's own path-equality semantics.
    allowed_roots = normalize_allowed_library_roots(steam_config.get("allowed_drives"))
    if allowed_roots:
        normalized_roots = [os.path.normcase(os.path.normpath(os.path.expanduser(root))) for root in allowed_roots]
        library_paths = [
            p for p in library_paths
            if any(os.path.normcase(os.path.normpath(p)).startswith(root) for root in normalized_roots)
        ]

    common_dirs = sorted({os.path.join(p, "steamapps", "common").lower() for p in library_paths})
    logging.info("Watching Steam library folders: %s", ", ".join(common_dirs))
    return common_dirs


def is_exe_under_dirs(exe_path, common_dirs, exclude_keywords):
    """True if exe_path lives under one of common_dirs and isn't excluded.

    Shared by any launcher whose games each get their own top-level folder
    under a common root (Steam, Xbox/Game Pass, Battle.net).
    """
    if not exe_path:
        return False
    exe_lower = exe_path.lower()
    for common_dir in common_dirs:
        prefix = common_dir if common_dir.endswith(os.sep) else common_dir + os.sep
        if exe_lower.startswith(prefix):
            return not any(kw in exe_lower for kw in exclude_keywords)
    return False


def get_display_name_from_dirs(exe_path, common_dirs):
    if not exe_path:
        return None
    exe_lower = exe_path.lower()
    for common_dir in common_dirs:
        prefix = common_dir if common_dir.endswith(os.sep) else common_dir + os.sep
        if exe_lower.startswith(prefix):
            remainder = exe_path[len(prefix):]
            folder = remainder.split(os.sep)[0]
            return folder or None
    return None


def get_xbox_install_dirs(xbox_config):
    # The Xbox/PC Game Pass app is Windows-only (UWP-based) -- there's no Mac or Linux client at
    # all to auto-detect, and the r"C:\XboxGames" default below is meaningless off Windows, so
    # this is a deliberate no-op there rather than an oversight. See CROSS_PLATFORM_PLAN.md §2.5.
    if sys.platform != "win32" or not xbox_config.get("enabled", True):
        return []
    configured = xbox_config.get("install_dirs") or [r"C:\XboxGames"]
    dirs = sorted({os.path.normpath(d).lower() for d in configured if os.path.isdir(d)})
    logging.info("Watching Xbox/Game Pass folders: %s", ", ".join(dirs) if dirs else "none found")
    return dirs


def get_battlenet_install_dirs(battlenet_config):
    if not battlenet_config.get("enabled", True):
        return []
    configured = battlenet_config.get("install_dirs") or []
    dirs = sorted({os.path.normpath(d).lower() for d in configured if os.path.isdir(d)})
    if dirs:
        logging.info("Watching Battle.net folders: %s", ", ".join(dirs))
    else:
        logging.info("No Battle.net install_dirs configured; Battle.net auto-detection is inactive.")
    return dirs


def get_gog_installed_games(gog_config):
    # GOG Galaxy itself has never shipped a Mac or Linux client (GOG's DRM-free Mac/Linux
    # installers for individual games are a separate, unrelated distribution path with no
    # registry/manifest this could hook into), so this is a deliberate no-op there rather than an
    # oversight. See CROSS_PLATFORM_PLAN.md §2.5. The real registry-reading logic now lives in
    # platform_common.find_gog_installed_games() / platform_windows.py, Phase 8.
    if sys.platform != "win32" or not gog_config.get("enabled", True):
        return []

    exclude_keywords = [k.lower() for k in gog_config.get("exclude_keywords", [])]
    games = platform_common.find_gog_installed_games(exclude_keywords)
    logging.info(
        "Watching GOG Galaxy installs: %s", ", ".join(g["display_name"] for g in games) if games else "none found"
    )
    return games


def get_epic_manifest_dir():
    """Directory Epic Games Launcher writes its .item install manifests to. Thin wrapper -- the
    real per-OS path (Epic ships a native client on Windows and macOS, but not Linux -- see
    CROSS_PLATFORM_PLAN.md §2.5) now lives in platform_common.epic_manifest_dir() /
    platform_windows.py / platform_linux.py / platform_macos.py."""
    return platform_common.epic_manifest_dir()


def get_epic_installed_games(epic_config):
    if not epic_config.get("enabled", True):
        return []

    manifest_dir = get_epic_manifest_dir()
    if not manifest_dir or not os.path.isdir(manifest_dir):
        logging.info("No Epic Games manifests found%s", f" at {manifest_dir}" if manifest_dir else " (Epic Games Launcher has no client on this OS)")
        return []

    exclude_keywords = [k.lower() for k in epic_config.get("exclude_keywords", [])]
    games = []
    for entry in os.listdir(manifest_dir):
        if not entry.lower().endswith(".item"):
            continue
        try:
            with open(os.path.join(manifest_dir, entry), "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        install_dir = manifest.get("InstallLocation")
        display_name = manifest.get("DisplayName")
        if not install_dir or not display_name or not manifest.get("LaunchExecutable"):
            continue
        if any(kw in display_name.lower() for kw in exclude_keywords):
            continue

        games.append({"install_dir": os.path.normpath(install_dir).lower(), "display_name": display_name})

    logging.info(
        "Watching Epic Games installs: %s", ", ".join(g["display_name"] for g in games) if games else "none found"
    )
    return games


def find_game_by_install_dir(exe_path, manifest_games):
    """Matches an exe against launchers that list exact install dirs (Epic, GOG)."""
    if not exe_path:
        return None
    exe_lower = os.path.normpath(exe_path).lower()
    for game in manifest_games:
        prefix = game["install_dir"] if game["install_dir"].endswith(os.sep) else game["install_dir"] + os.sep
        if exe_lower.startswith(prefix):
            return game["display_name"]
    return None


def find_target_process(
    watched_games, root_common_dirs, root_exclude_keywords, manifest_games, watched_windows, processes
):
    for name, exe, pid in processes:
        if name.lower() in watched_games:
            return name, exe, pid, None
    for name, exe, pid in processes:
        if is_exe_under_dirs(exe, root_common_dirs, root_exclude_keywords):
            return name, exe, pid, None
    for name, exe, pid in processes:
        manifest_display_name = find_game_by_install_dir(exe, manifest_games)
        if manifest_display_name:
            return name, exe, pid, manifest_display_name
    if watched_windows:
        window_titles = get_window_titles()
        for name, exe, pid in processes:
            name_lower = name.lower()
            for entry in watched_windows:
                if name_lower != entry["process_name"].lower():
                    continue
                needle = entry["title_contains"].lower()
                if any(needle in t.lower() for t in window_titles.get(pid, [])):
                    return name, exe, pid, entry.get("display_name")
    return None, None, None, None


INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def get_game_display_name(name, exe, root_common_dirs):
    folder = get_display_name_from_dirs(exe, root_common_dirs)
    if folder:
        return folder

    base = name[:-4] if name.lower().endswith(".exe") else name
    return base.title()


def sanitize_filename_part(text):
    cleaned = "".join(c for c in text if c not in INVALID_FILENAME_CHARS)
    return cleaned.strip()


def is_process_running(pid):
    return psutil.pid_exists(pid)


def is_obs_running(process_name, processes):
    process_name_lower = process_name.lower()
    return any(name.lower() == process_name_lower for name, _, _ in processes)


def get_process_memory_bytes(process_name, processes):
    process_name_lower = process_name.lower()
    total = 0
    for name, _, pid in processes:
        if name.lower() != process_name_lower:
            continue
        try:
            total += psutil.Process(pid).memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total


def kill_process_by_name(process_name):
    process_name_lower = process_name.lower()
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() == process_name_lower:
                proc.kill()
                proc.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass


def clear_obs_crash_sentinel():
    obs_config_dir = platform_common.obs_config_dir()
    if not obs_config_dir:
        return
    sentinel_dir = os.path.join(obs_config_dir, ".sentinel")
    if not os.path.isdir(sentinel_dir):
        return
    for entry in os.listdir(sentinel_dir):
        try:
            os.remove(os.path.join(sentinel_dir, entry))
        except OSError:
            pass


def cleanup_orphaned_pyinstaller_temp_dirs():
    """A frozen onefile build extracts to a fresh <tempdir>/_MEI<pid> folder every launch (the
    same _MEI<pid> naming convention on every OS a onefile PyInstaller build runs on, not just
    Windows) and normally deletes it again on clean exit. That delete can fail -- most commonly
    on Windows, where antivirus real-time scanning can still have a newly-extracted DLL open for
    scanning at that exact moment (a widely-reported PyInstaller/Windows Defender interaction,
    not specific to this app) -- leaving the folder orphaned. Left unchecked these just
    accumulate indefinitely. Only removes a folder whose PID no longer belongs to any running
    process (regardless of which app it came from), so a still-running process's own folder is
    never touched.

    tempfile.gettempdir() (not raw TEMP/TMP env var lookups, which only Windows commonly sets)
    finds the right base directory on every OS -- it already checks TMPDIR/TEMP/TMP in that
    order before falling back to each OS's own platform default (e.g. /tmp on Linux/macOS)."""
    temp_dir = tempfile.gettempdir()
    if not temp_dir or not os.path.isdir(temp_dir):
        return
    removed = 0
    try:
        entries = os.listdir(temp_dir)
    except OSError:
        return
    for entry in entries:
        if not entry.startswith("_MEI"):
            continue
        pid_part = entry[len("_MEI"):]
        if not pid_part.isdigit() or psutil.pid_exists(int(pid_part)):
            continue
        entry_path = os.path.join(temp_dir, entry)
        shutil.rmtree(entry_path, ignore_errors=True)
        if not os.path.isdir(entry_path):
            removed += 1
    if removed:
        logging.info("Cleaned up %d orphaned PyInstaller temp folder(s) from a previous run.", removed)


def is_startup_shortcut_enabled():
    """Thin dispatcher -- the real per-OS autostart mechanism (a Startup-folder .lnk on Windows,
    an XDG autostart .desktop file on Linux, a LaunchAgents .plist on macOS) now lives in
    platform_common.is_autostart_enabled() / platform_windows.py / platform_linux.py /
    platform_macos.py, see CROSS_PLATFORM_PLAN.md Phase 5."""
    return platform_common.is_autostart_enabled()


def set_startup_shortcut_enabled(enabled):
    """Enables/disables autostart for THIS running app (sys.executable) -- see
    is_startup_shortcut_enabled's own docstring for where the real per-OS work happens. Only
    works from a built/frozen executable (nothing standalone to point an autostart entry at when
    running from source -- sys.executable would just be the Python interpreter itself)."""
    if not enabled:
        return platform_common.disable_autostart()
    if not getattr(sys, "frozen", False):
        logging.warning("Cannot enable autostart while running from source; use the built app.")
        return False
    return platform_common.enable_autostart()


def launch_obs(obs_config):
    path = obs_config["path"]
    if not os.path.isfile(path):
        logging.error("OBS executable not found at %s", path)
        return False
    clear_obs_crash_sentinel()
    logging.info("OBS not running, launching from %s", path)
    subprocess.Popen(
        [path] + obs_config.get("launch_args", []),
        cwd=os.path.dirname(path),
    )
    time.sleep(obs_config.get("startup_wait_seconds", 8))
    return True


def connect_obs(ws_config, retries=5, delay=2):
    for attempt in range(1, retries + 1):
        try:
            client = obsws.ReqClient(
                host=ws_config["host"],
                port=ws_config["port"],
                password=ws_config["password"],
                timeout=3,
            )
            logging.info("Connected to OBS WebSocket at %s:%s", ws_config["host"], ws_config["port"])
            return client
        except Exception as exc:
            logging.warning("OBS WebSocket connection attempt %d/%d failed: %s", attempt, retries, exc)
            time.sleep(delay)
    logging.error("Could not connect to OBS WebSocket after %d attempts", retries)
    return None


RECORD_STARTED_STATE = "OBS_WEBSOCKET_OUTPUT_STARTED"


def is_automatic_split(recording_state, old_path, auto_split_config):
    if not auto_split_config or not auto_split_config.get("enabled"):
        return False

    by = auto_split_config.get("by", "time")
    if by == "size":
        if not old_path:
            return False
        try:
            size_mb = os.path.getsize(old_path) / (1024 * 1024)
        except OSError:
            return False
        target_mb = auto_split_config.get("megabytes", 0)
        tolerance_mb = auto_split_config.get("tolerance_megabytes", 50)
        return target_mb > 0 and target_mb <= size_mb <= target_mb + tolerance_mb

    elapsed = time.time() - recording_state.get("segment_start_time", time.time())
    target_seconds = auto_split_config.get("minutes", 0) * 60
    tolerance_seconds = auto_split_config.get("tolerance_seconds", 8)
    return target_seconds > 0 and target_seconds <= elapsed <= target_seconds + tolerance_seconds


def is_segment_silent(recording_state, silent_config):
    if not silent_config.get("enabled"):
        return False
    return not recording_state.get("heard_any_audio", False)


def reset_segment_audio_tracking(recording_state):
    recording_state["heard_any_audio"] = False
    recording_state["last_audio_peak_time"] = time.time()
    recording_state["silent_warning_sent"] = False


def maybe_transcode(path, transcode_config, icon=None, notifications_config=None):
    if transcode_config.get("enabled") and path:
        threading.Thread(
            target=transcode_recording, args=(path, transcode_config, icon, notifications_config), daemon=True
        ).start()


def maybe_apply_audio_sync_shift(path, audio_sync_shift_config, icon=None, notifications_config=None):
    if audio_sync_shift_config.get("enabled") and path:
        threading.Thread(
            target=apply_audio_sync_shift, args=(path, audio_sync_shift_config, icon, notifications_config),
            daemon=True,
        ).start()


def is_event_client_connected(event_client):
    """Best-effort liveness check for an obsws_python EventClient -- e.g. OBS was closed while
    the idle audio-mixer connection was open, silently dropping the socket. Used to decide
    whether to reconnect rather than trusting a held reference forever. Assumes connected if the
    underlying attribute can't be inspected (a version/API mismatch shouldn't itself force
    unnecessary reconnect churn)."""
    try:
        return bool(event_client.base_client.ws.connected)
    except Exception:
        return True


def connect_obs_events(config, icon, status, audio_state, recording_state):
    ws_config = config["obs"]["websocket"]
    auto_split_config = config["obs"].get("auto_split", {})
    silent_config = config.get("cleanup", {}).get("flag_silent_recordings", {})
    transcode_config = config.get("post_record_transcode", {})
    audio_sync_shift_config = config.get("audio_sync_shift", {})
    notifications_config = config.get("notifications", {})
    subfolders_enabled = config.get("organize_into_game_subfolders", False)

    try:
        event_client = obsws.EventClient(
            host=ws_config["host"],
            port=ws_config["port"],
            password=ws_config["password"],
            subs=obsws.Subs.LOW_VOLUME | obsws.Subs.INPUTVOLUMEMETERS,
            timeout=3,
        )
    except Exception as exc:
        logging.warning("Could not connect OBS event listener; split-flash disabled: %s", exc)
        return None

    def on_record_state_changed(data):
        if getattr(data, "output_state", "") != RECORD_STARTED_STATE:
            return
        output_path = getattr(data, "output_path", None)
        if output_path:
            recording_state["current_path"] = output_path
            recording_state["part_index"] = 1
            recording_state["did_split"] = False
            recording_state["segment_start_time"] = time.time()
            recording_state["session_start_time"] = time.time()
            recording_state["segment_files"] = []
            reset_segment_audio_tracking(recording_state)

    def on_record_file_changed(data):
        new_path = getattr(data, "new_output_path", "")
        if new_path == recording_state.get("current_path"):
            # OBS emits RecordFileChanged twice for a single split; ignore the duplicate.
            return

        old_path = recording_state.get("current_path")
        manual = not is_automatic_split(recording_state, old_path, auto_split_config)
        logging.info(
            "Recording file split detected (%s): %s", "manual" if manual else "automatic", new_path
        )
        status["flash_until"] = time.time() + SPLIT_FLASH_SECONDS
        icon.icon = build_tray_image(current_color(status))

        silent = is_segment_silent(recording_state, silent_config)
        finalized_path = None
        if manual:
            part_index = recording_state.get("part_index", 1)
            if old_path:
                finalized_path = rename_with_game_prefix(
                    old_path, recording_state.get("display_name"), part_index, silent, subfolders_enabled
                )
            recording_state["did_split"] = True
            recording_state["part_index"] = part_index + 1
        elif old_path:
            finalized_path = rename_with_game_prefix(
                old_path, recording_state.get("display_name"), None, silent, subfolders_enabled
            )

        if finalized_path:
            recording_state.setdefault("segment_files", []).append(finalized_path)
            maybe_transcode(finalized_path, transcode_config)
            maybe_apply_audio_sync_shift(finalized_path, audio_sync_shift_config)

        recording_state["current_path"] = new_path
        recording_state["segment_start_time"] = time.time()
        reset_segment_audio_tracking(recording_state)

        def revert():
            icon.icon = build_tray_image(current_color(status))

        timer = threading.Timer(SPLIT_FLASH_SECONDS, revert)
        timer.daemon = True
        timer.start()

    def on_replay_buffer_saved(data):
        # Same flash + renaming treatment as a manual split -- both are "a clip just landed on
        # disk" events from the user's point of view, so they should look and sound the same.
        saved_path = getattr(data, "saved_replay_path", None)
        if not saved_path:
            return
        logging.info("Replay buffer saved to %s", saved_path)
        status["flash_until"] = time.time() + SPLIT_FLASH_SECONDS
        icon.icon = build_tray_image(current_color(status))

        finalized_path = rename_with_game_prefix(
            saved_path, recording_state.get("display_name"), use_subfolder=subfolders_enabled, is_replay=True,
        )
        if finalized_path:
            maybe_transcode(finalized_path, transcode_config)
            maybe_apply_audio_sync_shift(finalized_path, audio_sync_shift_config)

        def revert():
            icon.icon = build_tray_image(current_color(status))

        timer = threading.Timer(SPLIT_FLASH_SECONDS, revert)
        timer.daemon = True
        timer.start()

    def on_input_volume_meters(data):
        levels = {}
        peak_overall = 0.0
        for entry in data.inputs:
            name = entry.get("inputName")
            if not name:
                continue
            # Each channel entry in inputLevelsMul is a [magnitude, peak, inputPeak]-shaped
            # triplet (all on the same 0-1 multiplier scale, per the "Mul" in the field name) --
            # take the max across every value in every channel rather than assuming index 1 is
            # always the meaningful one, for whichever of the three ends up largest. An
            # Application Audio Capture input pointed at a stale, specific window (rather than
            # an exe-only match) can also report an empty inputLevelsMul entirely -- nothing to
            # take a max of at all -- which looks the same in the overlay (a permanently silent
            # row) but isn't something this parsing can fix; re-pointing the input's capture
            # target (see set_game_audio_capture_target's exe-only pattern) is what resolves that.
            peak = 0.0
            for channel in entry.get("inputLevelsMul") or []:
                for value in channel:
                    if isinstance(value, (int, float)) and value > peak:
                        peak = value
            levels[name] = peak
            peak_overall = max(peak_overall, peak)
        audio_state["levels"] = levels

        if not (silent_config.get("enabled") and status["recording"]):
            return
        threshold = silent_config.get("peak_threshold", 0.02)
        if peak_overall >= threshold:
            recording_state["heard_any_audio"] = True
            recording_state["last_audio_peak_time"] = time.time()
            return

        if recording_state.get("silent_warning_sent"):
            return
        last_peak = recording_state.get("last_audio_peak_time")
        warn_after = silent_config.get("warn_after_seconds", 30)
        if last_peak is not None and time.time() - last_peak >= warn_after:
            recording_state["silent_warning_sent"] = True
            game = recording_state.get("display_name") or "the current recording"
            logging.warning("No audio detected for %ds while recording %s.", warn_after, game)
            notify(
                icon,
                notifications_config,
                "No audio detected",
                f"No audio for {warn_after}s while recording {game}. Check your audio capture source.",
            )

    event_client.callback.register(on_record_state_changed)
    event_client.callback.register(on_record_file_changed)
    event_client.callback.register(on_replay_buffer_saved)
    event_client.callback.register(on_input_volume_meters)
    return event_client


DEFAULT_OBS_RECOVERY_COOLDOWN_SECONDS = 30
DEFAULT_OBS_MEMORY_LIMIT_GB = 5


def get_obs_recovery_cooldown_seconds(obs_config):
    return obs_config.get("recovery", {}).get("cooldown_seconds", DEFAULT_OBS_RECOVERY_COOLDOWN_SECONDS)


def get_obs_memory_limit_bytes(obs_config):
    return obs_config.get("recovery", {}).get("memory_limit_gb", DEFAULT_OBS_MEMORY_LIMIT_GB) * 1024 ** 3


def ensure_obs_ready(config, processes, icon, status, audio_state, recording_state, obs_recovery_state):
    obs_config = config["obs"]
    if not is_obs_running(obs_config["process_name"], processes):
        if not launch_obs(obs_config):
            return None, None
        client = connect_obs(obs_config["websocket"])
    else:
        client = connect_obs(obs_config["websocket"])
        if not client:
            now = time.time()
            if now - obs_recovery_state["last_attempt"] < get_obs_recovery_cooldown_seconds(obs_config):
                logging.warning("OBS WebSocket still unresponsive; recovery was attempted recently, waiting before retrying.")
                return None, None
            obs_recovery_state["last_attempt"] = now
            logging.warning(
                "OBS process is running but its WebSocket is unresponsive (likely hung); restarting OBS."
            )
            kill_process_by_name(obs_config["process_name"])
            time.sleep(2)
            if not launch_obs(obs_config):
                return None, None
            client = connect_obs(obs_config["websocket"])

    if not client:
        return None, None

    # A profile-parameter write alone can't make OBS's replay buffer -- or the recording format
    # markers need -- actually available in the *current* session (see
    # apply_replay_buffer_settings / ensure_hybrid_mp4_for_markers); OBS only picks either up by
    # reading its profile fresh at startup. Both checks run up front so one restart covers
    # whichever actually needed a write, instead of restarting twice.
    markers_wanted = wants_markers(obs_config.get("custom_keybinds"))
    replay_buffer_restart_needed = apply_replay_buffer_settings(client, obs_config.get("replay_buffer", {}))
    marker_restart_needed = ensure_hybrid_mp4_for_markers(client) if markers_wanted else False

    # Filters apply live -- unlike the profile-parameter writes above, this never needs OBS to
    # restart to take effect.
    mic_boost_config = obs_config.get("mic_boost", {})
    if mic_boost_config.get("enabled") and mic_boost_config.get("input_name"):
        noise_gate_config = mic_boost_config.get("noise_gate", {})
        ensure_mic_boost_filter(
            client, mic_boost_config["input_name"], mic_boost_config.get("boost_db", 0.0),
            noise_gate_enabled=noise_gate_config.get("enabled", False),
            noise_gate_threshold_db=noise_gate_config.get("threshold_db", DEFAULT_MIC_BOOST_NOISE_GATE_THRESHOLD_DB),
        )

    if replay_buffer_restart_needed or marker_restart_needed:
        reasons = []
        if replay_buffer_restart_needed:
            reasons.append("replay buffer settings")
        if marker_restart_needed:
            reasons.append("recording format (Hybrid MP4, required for markers)")
        reason_text = " and ".join(reasons)
        logging.info("%s changed -- restarting OBS so the change actually takes effect.", reason_text.capitalize())
        notify(
            icon, config.get("notifications", {}), "OBS restarted",
            f"{reason_text.capitalize()} changed; OBS was restarted so they'd take effect.",
        )
        try:
            client.disconnect()
        except Exception:
            pass
        kill_process_by_name(obs_config["process_name"])
        time.sleep(2)
        if not launch_obs(obs_config):
            return None, None
        client = connect_obs(obs_config["websocket"])
        if not client:
            return None, None

    sync_multi_track_audio(
        client, obs_config.get("multi_track_audio", {}),
        obs_config.get("process_audio_capture_sync_offset_ms", DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS),
    )
    apply_output_folder(client, obs_config.get("output_folder"))
    apply_recording_resolution(client, obs_config)
    if markers_wanted:
        configured_format = obs_config.get("recording_format")
        if configured_format and configured_format != "hybrid_mp4":
            logging.info(
                "Ignoring configured recording format '%s' -- the \"Add Marker\" keybind is "
                "enabled, and OBS only supports chapter markers with Hybrid MP4.", configured_format,
            )
        ensure_hybrid_mp4_for_markers(client)
    else:
        apply_recording_format(client, obs_config.get("recording_format"))
    event_client = connect_obs_events(config, icon, status, audio_state, recording_state)
    return client, event_client


OBS_NOT_READY_CODE = 207


def start_recording(client, retries=6, delay=2):
    for attempt in range(1, retries + 1):
        try:
            client.start_record()
            logging.info("Recording started.")
            return True
        except obsws.error.OBSSDKRequestError as exc:
            if exc.code == OBS_NOT_READY_CODE and attempt < retries:
                logging.warning("OBS not ready to record yet (attempt %d/%d), retrying...", attempt, retries)
                time.sleep(delay)
                continue
            logging.error("Failed to start recording: %s", exc)
            return False
        except Exception as exc:
            logging.error("Failed to start recording: %s", exc)
            return False
    return False


WINDOW_MATCH_PRIORITY_EXE_FALLBACK = 2
OBS_RESOURCE_NOT_FOUND_CODE = 600


def set_game_audio_capture_target(client, input_name, process_name):
    """Points the game-audio-isolation input at the detected game's process, creating that
    Application Audio Capture input in OBS first if it doesn't exist yet -- so obs.game_audio_capture
    works without needing to add the OBS source by hand first, the same way the multi-track
    quick-setup wizard self-creates sources for common apps."""
    settings = {"window": f"::{process_name}", "priority": WINDOW_MATCH_PRIORITY_EXE_FALLBACK}
    try:
        client.set_input_settings(input_name, settings, True)
        logging.info("Pointed '%s' audio capture at %s", input_name, process_name)
        return
    except obsws.error.OBSSDKRequestError as exc:
        if exc.code != OBS_RESOURCE_NOT_FOUND_CODE:
            logging.warning("Could not point '%s' audio capture at %s: %s", input_name, process_name, exc)
            return
    except Exception as exc:
        logging.warning("Could not point '%s' audio capture at %s: %s", input_name, process_name, exc)
        return

    # Input doesn't exist yet -- create it with the target already set, instead of requiring it
    # be added by hand in OBS first.
    try:
        scene = client.get_current_program_scene().current_program_scene_name
        client.create_input(scene, input_name, "wasapi_process_output_capture", settings, True)
        logging.info(
            "Created Application Audio Capture input '%s' in OBS and pointed it at %s.",
            input_name, process_name,
        )
    except Exception as exc:
        logging.warning("Could not create '%s' audio capture input in OBS: %s", input_name, exc)


MIC_BOOST_NOISE_GATE_FILTER_NAME = "OBS Auto Recorder - Mic Boost Noise Gate"
MIC_BOOST_COMPRESSOR_FILTER_NAME = "OBS Auto Recorder - Mic Boost"
MIC_BOOST_LIMITER_FILTER_NAME = "OBS Auto Recorder - Mic Boost Limiter"
# Fixed noise-gate shape -- only open_threshold (the user's own configured threshold_db) varies.
# close_threshold is derived from it (see ensure_mic_boost_filter) rather than left at OBS's own
# fixed default, since a user-chosen open_threshold could otherwise land ABOVE a hardcoded close
# value -- backwards hysteresis, which would make the gate chatter open/closed unpredictably
# instead of cleanly gating silence out. attack/hold/release keep OBS's own real defaults
# (confirmed live via GetSourceFilterDefaultSettings) since those rarely need tuning per source.
MIC_BOOST_NOISE_GATE_HYSTERESIS_DB = 6.0
DEFAULT_MIC_BOOST_NOISE_GATE_THRESHOLD_DB = -26.0  # OBS's own real noise_gate_filter default (open_threshold)
MIC_BOOST_NOISE_GATE_BASE_SETTINGS = {"attack_time": 25, "hold_time": 200, "release_time": 150}
# Fixed compressor shape -- only output_gain (the user's own configured boost_db) varies.
# Threshold sits comfortably below a genuinely quiet mic's own peaks (confirmed live against a
# real recording: -25dBFS peaks on a source averaging -67dBFS) so compression actually engages on
# most of what the mic captures, tempering how hard the loudest moments get pushed up right along
# with the quiet ones. A plain volume/Gain filter has no such mechanism at all -- it multiplies
# every sample by the same fixed amount, so it hits 0dBFS (full digital clipping) far sooner than
# a compressor's makeup gain does, confirmed live via the SAME real recording: a flat +36dB
# export-time gain in the clip editor already had this source's peaks pinned at 0.0dBFS while its
# average level still only reached -32.5dBFS -- there simply isn't a single-stage flat-gain fix
# for a source recorded this quiet.
MIC_BOOST_COMPRESSOR_BASE_SETTINGS = {
    "threshold": -30.0,
    "ratio": 3.0,
    "attack_time": 6,
    "release_time": 60,
    "sidechain_source": "none",
}
# A hard safety ceiling chained AFTER the compressor's own output_gain -- confirmed live (via
# GetSourceFilterKindList/GetSourceFilterDefaultSettings against a real OBS instance) that this is
# a genuinely separate OBS filter kind ("limiter_filter"), not something the compressor itself
# also provides; without it, a large boost_db could still clip outright on a moment the
# compressor's own ratio/threshold didn't fully tame.
MIC_BOOST_LIMITER_SETTINGS = {"threshold": -1.0}


def predict_mic_boost_output_mul(peak_mul, boost_db, noise_gate_enabled=False, noise_gate_threshold_db=None):
    """Approximates what the Audio Mixer Levels overlay's live meter reading for this input would
    become once the real mic-boost filter chain is actually applied, using each filter's own
    static input/output curve (dB in, dB out) rather than fully emulating their real envelope-
    follower timing (attack/hold/release) -- a single instantaneous peak reading has no signal
    history to feed a real stateful compressor/gate simulation anyway. Deliberately only useful
    for PREVIEWING a candidate boost_db/threshold against the CURRENT raw signal before actually
    enabling it: once mic_boost is genuinely enabled, OBS's own InputVolumeMeters for this input
    already reports the real post-filter level directly (confirmed live: a real +30dB boost raised
    a live reading's mean peak from ~0.0028 to ~0.109, matching this same compressor+limiter
    shape) -- computing this on top of an ALREADY-boosted reading would double-apply the chain.

    peak_mul/return value: both an OBS-style 0-1 linear multiplier peak (same units as
    audio_state["levels"]), not dB -- callers never need to think in dB themselves."""
    if peak_mul <= 0:
        return 0.0
    peak_db = 20 * math.log10(peak_mul)

    if noise_gate_enabled and noise_gate_threshold_db is not None and peak_db < noise_gate_threshold_db:
        return 0.0

    threshold = MIC_BOOST_COMPRESSOR_BASE_SETTINGS["threshold"]
    ratio = MIC_BOOST_COMPRESSOR_BASE_SETTINGS["ratio"]
    compressed_db = threshold + (peak_db - threshold) / ratio if peak_db > threshold else peak_db
    boosted_db = compressed_db + boost_db
    output_db = min(boosted_db, MIC_BOOST_LIMITER_SETTINGS["threshold"])
    return min(1.0, 10 ** (output_db / 20))


def _ensure_obs_filter_settings(client, input_name, filter_name, filter_kind, settings):
    """Shared create-or-update-if-changed logic behind every filter in
    ensure_mic_boost_filter's chain. Returns (current_or_new_filter, just_created) --
    just_created lets a caller do something ONLY the first time a filter comes into existence
    (e.g. moving it to a specific position in the chain, which would be pointless -- and would
    fight a user's own manual reordering in OBS -- on every later idempotent check). Returns
    (None, False) on any failure; every failure is logged, never raised, matching this app's
    established rule for OBS-facing background calls."""
    try:
        current = client.get_source_filter(input_name, filter_name)
    except obsws.error.OBSSDKRequestError as exc:
        if exc.code != OBS_RESOURCE_NOT_FOUND_CODE:
            logging.warning("Could not check mic-boost filter '%s' on '%s': %s", filter_name, input_name, exc)
            return None, False
        try:
            client.create_source_filter(input_name, filter_name, filter_kind, settings)
            logging.info("Created OBS mic-boost filter '%s' on '%s'.", filter_name, input_name)
            return client.get_source_filter(input_name, filter_name), True
        except Exception as create_exc:
            logging.warning("Could not create mic-boost filter '%s' on '%s': %s", filter_name, input_name, create_exc)
            return None, False
    except Exception as exc:
        logging.warning("Could not check mic-boost filter '%s' on '%s': %s", filter_name, input_name, exc)
        return None, False

    if not all(current.filter_settings.get(key) == value for key, value in settings.items()):
        try:
            client.set_source_filter_settings(input_name, filter_name, settings, overlay=False)
            logging.info("Updated OBS mic-boost filter '%s' on '%s'.", filter_name, input_name)
            current = client.get_source_filter(input_name, filter_name)
        except Exception as exc:
            logging.warning("Could not update mic-boost filter '%s' on '%s': %s", filter_name, input_name, exc)
    return current, False


def ensure_mic_boost_filter(
    client, input_name, boost_db, noise_gate_enabled=False, noise_gate_threshold_db=None,
):
    """Applies OBS Auto Recorder's mic-boost filter chain directly to input_name -- live, at OBS's
    own audio pipeline, so every FUTURE recording captures this source boosted from the start.
    Deliberately independent from (and stackable with) the clip editor's own per-trim ffmpeg gain
    (Track Routing dialog / Settings > Clip Editor > Default Track Gains): that one can only
    re-encode an already-recorded FILE, and can't do anything about a source that was captured too
    quiet to begin with -- see the settings above for why a flat gain alone can't either, at any
    single stage.

    Chain order (signal flows top to bottom): an optional Noise Gate first (so it gates the RAW
    signal before anything downstream amplifies whatever noise floor is left), then a Compressor
    for makeup gain + dynamics control, then a Limiter as a hard safety ceiling. New filters are
    appended to the end of OBS's own filter list by CreateSourceFilter -- harmless for the
    Compressor/Limiter pair (always created together, in the right relative order), but the Gate
    specifically gets moved to index 0 right after its own creation, since it can be toggled on
    independently, later, well after the other two already exist.

    noise_gate_enabled/noise_gate_threshold_db: unlike the Compressor/Limiter (created once and
    otherwise left alone -- see _ensure_obs_filter_settings), the gate's own OBS-side enabled
    state is actively kept in sync with noise_gate_enabled on every call, since "toggleable" is
    the whole point of exposing it as its own Settings checkbox -- a user flipping it needs that
    to actually take effect, not just influence whether the filter gets created in the first
    place.

    Idempotent and safe to call on every OBS-ready check: only writes a settings/enabled update
    when something has actually drifted from what's configured here, so this never resets the
    Compressor's or Limiter's enabled state if the user disabled one of THOSE by hand in OBS."""
    if not input_name:
        return

    if noise_gate_threshold_db is not None:
        gate_settings = dict(
            MIC_BOOST_NOISE_GATE_BASE_SETTINGS,
            open_threshold=noise_gate_threshold_db,
            close_threshold=noise_gate_threshold_db - MIC_BOOST_NOISE_GATE_HYSTERESIS_DB,
        )
        gate, gate_just_created = _ensure_obs_filter_settings(
            client, input_name, MIC_BOOST_NOISE_GATE_FILTER_NAME, "noise_gate_filter", gate_settings,
        )
        if gate is not None:
            if gate_just_created:
                try:
                    client.set_source_filter_index(input_name, MIC_BOOST_NOISE_GATE_FILTER_NAME, 0)
                except Exception as exc:
                    logging.warning("Could not move mic-boost noise gate to the front of the chain: %s", exc)
            if gate.filter_enabled != noise_gate_enabled:
                try:
                    client.set_source_filter_enabled(input_name, MIC_BOOST_NOISE_GATE_FILTER_NAME, noise_gate_enabled)
                    logging.info(
                        "%s OBS mic-boost noise gate on '%s'.",
                        "Enabled" if noise_gate_enabled else "Disabled", input_name,
                    )
                except Exception as exc:
                    logging.warning("Could not toggle mic-boost noise gate on '%s': %s", input_name, exc)

    compressor_settings = dict(MIC_BOOST_COMPRESSOR_BASE_SETTINGS, output_gain=boost_db)
    _ensure_obs_filter_settings(client, input_name, MIC_BOOST_COMPRESSOR_FILTER_NAME, "compressor_filter", compressor_settings)
    _ensure_obs_filter_settings(client, input_name, MIC_BOOST_LIMITER_FILTER_NAME, "limiter_filter", MIC_BOOST_LIMITER_SETTINGS)


# Confirmed live via a controlled cross-correlation test: a real-world sound captured
# simultaneously through OBS's two WASAPI audio capture paths lands later on a
# wasapi_process_output_capture input (Application Audio Capture -- what Game Audio and every
# multi_track_audio.app_captures entry use) than on wasapi_output_capture (Desktop Audio) --
# a genuine, inherent latency difference between Windows' per-process audio capture and plain
# device loopback capture, not a bug in this app or in OBS. Left uncorrected, an isolated
# app-audio track drifts audibly out of sync with Desktop Audio -- exactly the "doubling"/echo
# effect a multi-track-aware player (Premiere, Discord's mobile app) produces when it plays
# more than one track's audio at once, since it's mixing in a track that's really playing late.
#
# The original measurement of this constant was -55ms, taken before measure_audio_lag_ms's
# click-pairing was hardened (clustering + nearest-neighbor-within-tolerance + per-click
# cross-correlation refinement, see CALIBRATION_CLICK_POSITIONS_SECONDS below) -- it turned out
# to be a noisy outlier: three fresh calibration runs against the SAME physical setup with the
# hardened algorithm measured a tight, repeatable -24/-27/-29ms instead, confirmed live after a
# user report that audio was "still out of sync" even with -55ms applied (i.e. -55 was
# overcorrecting by roughly 2x). Since this default only matters for a user who hasn't run
# their own calibration yet, it's set to the more trustworthy figure.
DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS = -27


def apply_process_capture_sync_offset(client, input_name, offset_ms):
    """Applies a compensating OBS "Sync Offset" (ms) to a wasapi_process_output_capture input so
    its audio lines back up with device-capture sources like Desktop Audio -- see
    DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS. Desktop Audio itself is never touched; it's the
    reference everything else is corrected against. Read-compare-write, same as every other
    OBS-state override in this file, so this doesn't generate a write (and doesn't risk needing
    whatever restart behavior that write might trigger) once it's already correct."""
    try:
        current = client.get_input_audio_sync_offset(input_name).input_audio_sync_offset
    except Exception as exc:
        logging.warning("Could not read sync offset for audio input '%s': %s", input_name, exc)
        return
    if current == offset_ms:
        return
    try:
        client.set_input_audio_sync_offset(input_name, offset_ms)
        logging.info(
            "Set audio input '%s' sync offset to %sms (compensating for process-capture latency).",
            input_name, offset_ms,
        )
    except Exception as exc:
        logging.warning("Could not set sync offset for audio input '%s': %s", input_name, exc)


def compute_process_capture_tracks(obs_config):
    """Returns the sorted list of OBS track numbers (1-6) currently routed to a
    wasapi_process_output_capture input -- Game Audio (if game_audio_capture.enabled) and every
    multi_track_audio.app_captures entry -- the same set apply_process_capture_sync_offset
    already corrects live at record time. Lets the clip editor's "Fix audio track sync" checkbox
    know which tracks are worth measuring/shifting without the user needing to look up or type in
    track numbers themselves. Returns [] if multi-track audio isn't set up (or none of its
    entries are actually process-capture inputs) -- there's nothing to shift in that case."""
    multi_track_config = obs_config.get("multi_track_audio", {})
    entries = normalize_track_entries(multi_track_config.get("tracks"))
    if not entries:
        return []
    process_capture_names = {
        app.get("input_name") for app in multi_track_config.get("app_captures", []) if app.get("input_name")
    }
    game_audio_config = obs_config.get("game_audio_capture", {})
    if game_audio_config.get("enabled"):
        process_capture_names.add(game_audio_config.get("input_name") or "Game Audio")
    return sorted({track for name, track in entries if name in process_capture_names})


def compute_reference_track(obs_config, reference_input_name="Desktop Audio"):
    """Returns the OBS track number reference_input_name is routed to in
    obs.multi_track_audio.tracks, or None if it isn't configured there at all -- the same
    reference run_audio_sync_calibration and apply_process_capture_sync_offset already correct
    every process-capture input against. Used to know which track in an existing clip actually
    holds the "ground truth" audio to measure a process-capture track's real lag against."""
    entries = normalize_track_entries(obs_config.get("multi_track_audio", {}).get("tracks"))
    for name, track in entries:
        if name == reference_input_name:
            return track
    return None


def track_name_hints(obs_config):
    """Returns {track_number: "Name1+Name2"} from obs.multi_track_audio.tracks -- purely a
    display hint for the clip editor's Track Routing dialog, so its column headers can show
    "3 (Game Audio)" instead of a bare, meaningless track number when this app's own config
    happens to explain what's routed there. Multiple input names sharing one track number (OBS
    lets several inputs mix into the same track) are joined with "+". Returns {} if
    multi_track_audio isn't configured at all -- the dialog just shows bare numbers then."""
    entries = normalize_track_entries(obs_config.get("multi_track_audio", {}).get("tracks"))
    names_by_track = {}
    for name, track in entries:
        names_by_track.setdefault(track, []).append(name)
    return {track: "+".join(names) for track, names in names_by_track.items()}


CALIBRATION_TONE_SAMPLE_RATE = 48000
CALIBRATION_TONE_DURATION_SECONDS = 20.0
# Confirmed live that real background/ambient noise (Desktop Audio alone can pick up plenty of
# it) knocks out enough individual clicks that 7 data points isn't always enough redundancy for
# the median to stay reliable run-to-run (observed swinging between ~20ms and ~55ms across
# otherwise-identical runs) -- roughly double the clicks over a longer tone directly buys back
# that robustness, since measure_audio_lag_ms already discards anything without a confident match.
CALIBRATION_CLICK_POSITIONS_SECONDS = [1.0 + i for i in range(18)]
CALIBRATION_CLICK_DURATION_SECONDS = 0.003
# Reference for extraction purposes only -- the reference input's OWN existing track is read
# live and left untouched; only the target input gets temporarily moved here for the test.
CALIBRATION_TARGET_TRACK = 6


def generate_calibration_tone(output_path):
    """Writes a short WAV of sharp broadband click impulses at known positions to output_path --
    used as run_audio_sync_calibration's test signal. Broadband noise bursts (rather than a pure
    tone) correlate sharply even after lossy AAC encoding, which is what actually gets measured
    here (both capture paths re-encode through OBS's own AAC encoder before this ever reads
    them back)."""
    import wave
    import numpy as np

    sr = CALIBRATION_TONE_SAMPLE_RATE
    audio = np.zeros(int(sr * CALIBRATION_TONE_DURATION_SECONDS), dtype=np.float32)
    click_len = int(CALIBRATION_CLICK_DURATION_SECONDS * sr)
    rng = np.random.default_rng()
    for pos_sec in CALIBRATION_CLICK_POSITIONS_SECONDS:
        start = int(pos_sec * sr)
        audio[start:start + click_len] = rng.uniform(-1.0, 1.0, click_len)
    audio_i16 = (audio * 32767).astype(np.int16)
    with wave.open(output_path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(audio_i16.tobytes())


CALIBRATION_CLUSTER_MIN_GAP_MS = 200
CALIBRATION_TEMPLATE_HALF_WINDOW_MS = 8
CALIBRATION_SEARCH_MARGIN_MS = 15


def measure_audio_lag_ms(reference_wav_path, target_wav_path):
    """Measures how many ms the audio in target_wav_path lags behind reference_wav_path, given
    two recordings of the same generate_calibration_tone() clicks captured simultaneously through
    different paths.

    Three-stage approach, in order of what each stage is actually for:

    1. Coarse detection: find candidate clicks via a simple energy threshold. AAC's own
       transient smearing typically splits one real click into a whole cluster of consecutive
       above-threshold samples, not a single spike -- collapsing each cluster down to one
       representative point is what keeps a single real click from registering as several.

    2. Matching: pair up each reference point with its nearest target point, but only within a
       generous tolerance for the actual latency being measured -- NOT by list index. Confirmed
       live that real recordings aren't as clean as clustering alone assumes: Desktop Audio alone
       picked up enough background/ambient noise to register several extra "clicks" unrelated to
       the test tone, and a real audio-rendering process missed its own first click to a
       cold-start glitch right after launch -- both mean the two signals can end up with a
       genuinely different number of points at different positions, not just the same clicks
       smeared differently. Index-pairing that situation silently matches up unrelated events
       (confirmed live: produced multi-*second* garbage). Nearest-neighbor-within-tolerance
       matching instead just discards anything without a genuine counterpart.

    3. Refinement: for each matched pair, cross-correlate the actual click *waveform* (a small
       window around the coarse point) rather than trusting a single loudest sample -- the
       standard time-delay-of-arrival technique (the same idea behind GPS/audio-ranging), and
       meaningfully more precise than comparing two independently-noisy single-sample peak picks.

    Returns the median of all the refined per-click lags -- median rather than mean specifically
    so one bad click can't skew the result. Returns None if fewer than 3 clicks were confidently
    matched in both signals (e.g. one input never actually received the test tone, or too much
    background noise drowned it out)."""
    import wave
    import numpy as np

    def load_mono(path):
        with wave.open(path, "rb") as f:
            sr = f.getframerate()
            n = f.getnframes()
            ch = f.getnchannels()
            data = np.frombuffer(f.readframes(n), dtype=np.int16).astype(np.float64)
            if ch > 1:
                data = data.reshape(-1, ch).mean(axis=1)
            return data, sr

    def find_click_positions(sig, sr, threshold_frac=0.3, min_gap_ms=CALIBRATION_CLUSTER_MIN_GAP_MS):
        abs_sig = np.abs(sig)
        peak = abs_sig.max()
        if peak <= 0:
            return []
        threshold = peak * threshold_frac
        above_idx = np.flatnonzero(abs_sig > threshold)
        if len(above_idx) == 0:
            return []
        min_gap = int(min_gap_ms * sr / 1000)
        clusters = []
        cluster_start = prev = above_idx[0]
        for idx in above_idx[1:]:
            if idx - prev > min_gap:
                clusters.append((cluster_start, prev))
                cluster_start = idx
            prev = idx
        clusters.append((cluster_start, prev))
        # One representative point per cluster: the single loudest sample in it. Only a coarse
        # anchor for stage 2 below -- its own precision doesn't matter much.
        return [start + int(np.argmax(abs_sig[start:end + 1])) for start, end in clusters]

    def refine_lag_samples(ref_point, target_point):
        half = int(CALIBRATION_TEMPLATE_HALF_WINDOW_MS * sr / 1000)
        margin = int(CALIBRATION_SEARCH_MARGIN_MS * sr / 1000)
        r0, r1 = ref_point - half, ref_point + half
        if r0 < 0 or r1 > len(ref):
            return None
        template = ref[r0:r1]
        template = template - template.mean()

        t0, t1 = target_point - half - margin, target_point + half + margin
        t0c, t1c = max(0, t0), min(len(target), t1)
        region = target[t0c:t1c]
        if len(region) < len(template):
            return None
        region = region - region.mean()
        # "valid" mode: for every position the template could sit fully inside region, how well
        # it matches there -- its argmax is where the click's waveform shape actually lines up
        # best, not just where a single sample happens to be loudest.
        corr = np.correlate(region, template, mode="valid")
        best_offset = int(np.argmax(corr))
        target_equivalent = t0c + best_offset + half
        return target_equivalent - ref_point

    ref, sr_ref = load_mono(reference_wav_path)
    target, sr_target = load_mono(target_wav_path)
    if sr_ref != sr_target or sr_ref <= 0:
        return None
    sr = sr_ref

    ref_points = find_click_positions(ref, sr)
    target_points = find_click_positions(target, sr)

    # Real recordings aren't as clean as the coarse threshold assumes: confirmed live that
    # Desktop Audio alone can pick up enough ambient/background noise to register several extra
    # "clicks" that have nothing to do with the test tone, and a real audio-rendering process
    # can miss its own very first click to a cold-start glitch right after launch -- both mean
    # the two signals can end up with a genuinely different NUMBER of detected points, at
    # different positions, not just the same clicks smeared differently. Pairing by list index
    # in that situation silently matches up completely unrelated events (confirmed live: this
    # produced multi-*second* garbage results). Matching each reference point to its nearest
    # target point, but only within a generous tolerance for the real latency being measured,
    # correctly discards anything that doesn't have a genuine counterpart instead of forcing a
    # bogus pairing -- standard nearest-neighbor-with-a-gate matching, not index alignment.
    max_expected_lag_ms = 150
    max_lag_samples = int(max_expected_lag_ms * sr / 1000)
    unmatched_targets = list(target_points)
    pairs = []
    for rp in ref_points:
        candidates = [tp for tp in unmatched_targets if abs(tp - rp) <= max_lag_samples]
        if not candidates:
            continue
        best = min(candidates, key=lambda tp: abs(tp - rp))
        unmatched_targets.remove(best)
        pairs.append((rp, best))
    if len(pairs) < 3:
        return None

    lags_ms = []
    for ref_point, target_point in pairs:
        lag_samples = refine_lag_samples(ref_point, target_point)
        if lag_samples is None:
            lag_samples = target_point - ref_point  # fall back to the coarse estimate
        lags_ms.append(lag_samples * 1000.0 / sr)
    lags_ms.sort()
    return lags_ms[len(lags_ms) // 2]


WAVEFORM_LAG_MIN_CONFIDENCE = 0.05
WAVEFORM_LAG_CHUNK_SECONDS = 6.0
WAVEFORM_LAG_MIN_CHUNKS_FOR_MAJORITY_CHECK = 3
WAVEFORM_LAG_AGREEMENT_TOLERANCE_MS = 8.0


def measure_waveform_lag_ms(reference_wav_path, target_wav_path, max_expected_lag_ms=150, label=""):
    """Measures how many ms the audio in target_wav_path lags behind reference_wav_path via
    FFT cross-correlation. Unlike measure_audio_lag_ms (built specifically for the calibration
    tone's isolated, sparse clicks), this works on ordinary continuous real audio content -- e.g.
    the clip editor's "Fix audio track sync" measuring an actual already-recorded clip directly,
    rather than trusting a fixed pre-calibrated offset that might not exactly match this
    particular clip. Works because Desktop Audio (device capture, picks up the full system mix)
    and an isolated process-capture track (Game Audio, Discord, ...) both contain the SAME
    underlying game/app sound, just via two different capture paths -- correlating them recovers
    the real delay between those two paths directly from the clip's own audio.

    FFT-based rather than np.correlate(..., mode="full") -- confirmed earlier this session that
    direct full-mode correlation hangs for minutes on arrays in the ~480,000-sample range this
    deals with; FFT correlation is the standard, far faster equivalent.

    Confidence is a chunk's peak normalized correlation score (peak / (||ref|| * ||target||),
    the audio equivalent of a Pearson correlation coefficient for the best-aligned overlap)
    rather than a fixed multiple of the correlation's own noise floor -- confirmed empirically
    that the latter gives false-positive "confident" matches on two genuinely UNRELATED signals
    purely by chance (a wide search window makes some random peak clearing "3x the noise floor"
    likely). Real matching content scores far above WAVEFORM_LAG_MIN_CONFIDENCE even under heavy
    independent capture noise on each side; unrelated signals hover near the ~1/sqrt(N) floor
    random noise produces, confirmed via direct testing to stay roughly an order of magnitude
    below that threshold.

    A single confidently-scored correlation over a long window isn't enough on its own, though --
    confirmed live against a real recording that one ~30s window can score a HIGHER confidence at
    a spurious, physically-implausible lag (a menu/loading moment with only sporadic matching
    content) than a genuinely correct ~30s window elsewhere in the very same file scores at the
    real lag. Splitting the signal into WAVEFORM_LAG_CHUNK_SECONDS-long pieces, measuring each
    independently, and requiring a majority of the confident ones to agree within
    WAVEFORM_LAG_AGREEMENT_TOLERANCE_MS of their median (confirmed empirically: correct chunks
    from the same real recording landed within a fraction of a ms of each other, while a
    chunk-batch with no real consensus scattered tens of ms apart) is what actually catches that
    -- the same "redundant measurements + agreement, not one single shot" lesson this session's
    audio-sync calibration work already learned the hard way. Signals too short to split into at
    least WAVEFORM_LAG_MIN_CHUNKS_FOR_MAJORITY_CHECK chunks fall back to one whole-signal
    measurement instead, since there's nothing to cross-check in that case anyway.

    Returns None if either signal is at or near silence, no chunk's best match within
    +/- max_expected_lag_ms clears the confidence bar, or (for long-enough signals) the confident
    chunks don't actually agree with each other -- i.e. the two tracks don't appear to share
    reliably-alignable content, so trusting a "measured" lag here would be worse than not
    measuring at all."""
    import wave
    import numpy as np

    # Logged at INFO (not DEBUG) unconditionally -- confirmed live that a real in-app "Fix audio
    # track sync" run can return an empty/no-confidence result on a file+window where the exact
    # same extracted audio, measured standalone, comes back confident. Without this, there was no
    # way to tell WHY a specific real run failed (short of re-running it outside the app and
    # hoping the same result reproduces) -- these lines are the difference between guessing and
    # actually seeing the per-chunk scores/lags for the run that failed.
    tag = f" [{label}]" if label else ""

    def load_mono(path):
        with wave.open(path, "rb") as f:
            sr = f.getframerate()
            n = f.getnframes()
            ch = f.getnchannels()
            data = np.frombuffer(f.readframes(n), dtype=np.int16).astype(np.float64)
            if ch > 1:
                data = data.reshape(-1, ch).mean(axis=1)
            return data, sr

    def measure_segment(ref_seg, target_seg, sr):
        n = len(ref_seg)
        ref_seg = ref_seg - ref_seg.mean()
        target_seg = target_seg - target_seg.mean()
        ref_norm = float(np.sqrt(np.sum(ref_seg ** 2)))
        target_norm = float(np.sqrt(np.sum(target_seg ** 2)))
        if ref_norm <= 1e-9 or target_norm <= 1e-9:
            return None, None  # this segment is effectively silent on one side
        max_lag_samples = int(max_expected_lag_ms * sr / 1000)
        fft_size = 1
        while fft_size < 2 * n:
            fft_size *= 2
        ref_f = np.fft.rfft(ref_seg, fft_size)
        target_f = np.fft.rfft(target_seg, fft_size)
        corr = np.fft.irfft(ref_f * np.conj(target_f), fft_size)
        lags = np.concatenate([np.arange(0, max_lag_samples + 1), np.arange(-max_lag_samples, 0)])
        scores = corr[lags]
        best_idx = int(np.argmax(scores))
        best_lag_samples = int(lags[best_idx])
        normalized_peak = float(scores[best_idx] / (ref_norm * target_norm))
        # corr[k] = sum_n ref[n] * target[n-k] -- confirmed empirically (a synthetic signal with
        # a known, deliberately-introduced delay) that when target genuinely lags ref by d
        # samples, the peak lands at k = -d, so the lag itself is the negation of that.
        lag_ms = -best_lag_samples * 1000.0 / sr
        if normalized_peak < WAVEFORM_LAG_MIN_CONFIDENCE:
            return None, (lag_ms, normalized_peak)
        return lag_ms, (lag_ms, normalized_peak)

    # A WAV that ffmpeg reported success on (exit code 0, non-trivial file size -- everything
    # extract_audio_track_wav itself checks) can still be malformed enough that Python's own wave
    # module can't parse it (a truncated data chunk, a header/byte-count mismatch from a write
    # that got interrupted partway) -- caught here and logged instead of left to crash this whole
    # function, which previously silently aborted measurement for every remaining track too (an
    # uncaught exception here propagates out of the caller's loop entirely, not just this track).
    try:
        ref, sr_ref = load_mono(reference_wav_path)
        target, sr_target = load_mono(target_wav_path)
    except Exception as exc:
        logging.warning("Audio sync measurement%s: could not read extracted audio (%s).", tag, exc)
        return None
    if sr_ref != sr_target or sr_ref <= 0:
        logging.info(
            "Audio sync measurement%s: sample rate mismatch (reference=%sHz, target=%sHz) -- can't correlate.",
            tag, sr_ref, sr_target,
        )
        return None
    sr = sr_ref

    n = min(len(ref), len(target))
    logging.info(
        "Audio sync measurement%s: loaded %.2fs of reference audio and %.2fs of target audio at %dHz.",
        tag, len(ref) / sr if sr else 0, len(target) / sr if sr else 0, sr,
    )
    if n <= 0:
        return None
    ref = ref[:n]
    target = target[:n]

    chunk_len = int(WAVEFORM_LAG_CHUNK_SECONDS * sr)
    chunk_bounds = [(i, min(i + chunk_len, n)) for i in range(0, n, chunk_len)]
    chunk_bounds = [(s, e) for s, e in chunk_bounds if e - s >= chunk_len * 0.5]
    if len(chunk_bounds) < WAVEFORM_LAG_MIN_CHUNKS_FOR_MAJORITY_CHECK:
        lag_ms, diag = measure_segment(ref, target, sr)
        logging.info(
            "Audio sync measurement%s: only %d chunk(s) available (need %d) -- using one whole-"
            "signal measurement instead: %s.",
            tag, len(chunk_bounds), WAVEFORM_LAG_MIN_CHUNKS_FOR_MAJORITY_CHECK,
            f"lag={diag[0]:.1f}ms score={diag[1]:.4f} (threshold {WAVEFORM_LAG_MIN_CONFIDENCE})"
            if diag else "silent on one side",
        )
        return lag_ms

    measurements = []
    diagnostics = []
    for s, e in chunk_bounds:
        lag_ms, diag = measure_segment(ref[s:e], target[s:e], sr)
        diagnostics.append(diag)
        if lag_ms is not None:
            measurements.append(lag_ms)
    logging.info(
        "Audio sync measurement%s: %d/%d chunk(s) confident (threshold %s) -- %s",
        tag, len(measurements), len(chunk_bounds), WAVEFORM_LAG_MIN_CONFIDENCE,
        ", ".join(
            f"chunk{i}:lag={d[0]:.1f}ms/score={d[1]:.4f}" if d else f"chunk{i}:silent"
            for i, d in enumerate(diagnostics)
        ),
    )
    if len(measurements) < WAVEFORM_LAG_MIN_CHUNKS_FOR_MAJORITY_CHECK:
        logging.info(
            "Audio sync measurement%s: only %d confident chunk(s) (need %d) -- no result.",
            tag, len(measurements), WAVEFORM_LAG_MIN_CHUNKS_FOR_MAJORITY_CHECK,
        )
        return None
    measurements.sort()
    median = measurements[len(measurements) // 2]
    agreeing = [m for m in measurements if abs(m - median) <= WAVEFORM_LAG_AGREEMENT_TOLERANCE_MS]
    if len(agreeing) < max(WAVEFORM_LAG_MIN_CHUNKS_FOR_MAJORITY_CHECK, len(measurements) / 2):
        logging.info(
            "Audio sync measurement%s: confident chunks disagree (median=%.1fms, only %d/%d "
            "within %.1fms of it) -- no result.",
            tag, median, len(agreeing), len(measurements), WAVEFORM_LAG_AGREEMENT_TOLERANCE_MS,
        )
        return None
    agreeing.sort()
    return agreeing[len(agreeing) // 2]


def extract_audio_track_wav(ffmpeg_path, input_path, track_number, output_wav_path, start_seconds=0, duration_seconds=None):
    """Extracts one 1-based OBS audio track (== ffmpeg stream 0:a:{track-1}) from input_path to a
    mono PCM WAV file, optionally limited to [start_seconds, start_seconds+duration_seconds) --
    used to feed measure_waveform_lag_ms a short segment instead of decoding an entire clip.
    Returns True on success. Timeout is deliberately generous per call (see
    AUDIO_SYNC_MEASUREMENT_EXTRACTION_TIMEOUT_SECONDS) -- measure_clip_audio_sync_shifts_ms's own
    overall wall-clock budget is what actually bounds a full multi-track measurement's worst
    case, not this.

    Logs a warning (with the exit code/stderr or exception) on every failure path -- confirmed
    live that a silent False here (a failed extraction just quietly dropping that track from the
    result) made a real in-app measurement failure impossible to diagnose after the fact, since
    the same extraction succeeded reliably in every standalone reproduction outside the app."""
    cmd = [ffmpeg_path, "-y"]
    if start_seconds:
        cmd += ["-ss", format_timestamp(start_seconds)]
    cmd += ["-i", input_path]
    if duration_seconds:
        cmd += ["-t", format_timestamp(duration_seconds)]
    cmd += ["-map", f"0:a:{track_number - 1}", "-ac", "1", "-c:a", "pcm_s16le", output_wav_path]
    try:
        # encoding/errors explicit rather than relying on text=True's own locale-based default --
        # ffmpeg's stderr can contain bytes that aren't valid under whatever codepage a packaged
        # (--noconsole) build resolves as its default, and an undecodable byte there would raise
        # UnicodeDecodeError from inside subprocess.run() itself, before either except clause
        # below gets a chance to catch it -- a genuinely silent crash straight out of this
        # function, indistinguishable from the measurement having simply found nothing.
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            **platform_common.hide_console_subprocess_kwargs(),
            timeout=AUDIO_SYNC_MEASUREMENT_EXTRACTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logging.warning(
            "Audio sync measurement: extracting track %s from %s timed out after %ss.",
            track_number, os.path.basename(input_path), AUDIO_SYNC_MEASUREMENT_EXTRACTION_TIMEOUT_SECONDS,
        )
        return False
    except OSError as exc:
        logging.warning(
            "Audio sync measurement: could not run ffmpeg to extract track %s from %s: %s",
            track_number, os.path.basename(input_path), exc,
        )
        return False
    if result.returncode != 0:
        logging.warning(
            "Audio sync measurement: extracting track %s from %s failed (exit code %s).\nstderr:\n%s",
            track_number, os.path.basename(input_path), result.returncode, result.stderr[-2000:],
        )
        return False
    if not (os.path.isfile(output_wav_path) and os.path.getsize(output_wav_path) > 44):
        logging.warning(
            "Audio sync measurement: extracting track %s from %s produced no usable output.",
            track_number, os.path.basename(input_path),
        )
        return False
    return True


AUDIO_SYNC_MEASUREMENT_EXTRACTION_TIMEOUT_SECONDS = 20
# A hard ceiling across an ENTIRE multi-track measurement, not per track -- confirmed live in
# earlier testing that a single measurement can occasionally take far longer than expected inside
# the packaged app specifically (an in-app-only symptom -- standalone script timing on the exact
# same file/hardware was consistently sub-second). The clip editor now pauses VLC's own preview
# before starting any measurement, since concurrent access to the SAME large source file from
# both VLC's decode/render pipeline and ffmpeg's own decode is a very plausible explanation for
# that gap (no standalone test ever had VLC concurrently reading the file); this budget exists
# regardless as a worst-case backstop that guarantees the clip editor's "Fix audio track sync"
# always resolves in bounded time either way, falling back to per-track defaults for whatever it
# didn't get to, rather than the UI looking stuck indefinitely the way it did before this existed.
AUDIO_SYNC_MEASUREMENT_OVERALL_BUDGET_SECONDS = 45
# The most of the SOURCE FILE (starting at the trim's own start point, but not bounded by the
# trim's own -- possibly very short -- duration) the clip editor's "Fix audio track sync" samples
# per track; a majority-agreement measurement (see measure_waveform_lag_ms) doesn't need more
# than a representative window to be confident.
AUDIO_SYNC_FIX_MAX_MEASUREMENT_SECONDS = 30
# The least it samples, REGARDLESS of how short the actual trim selection is -- confirmed live
# that a ~5-second trim gave the correlation algorithm too little audio to confidently match on
# ANY track, silently falling back to the configured default for all of them. The per-track lag
# being measured is a property of the recording itself (WASAPI capture mechanics), not of
# whatever range happens to get exported, so sampling further into the source file than just the
# trimmed selection is exactly as valid. Comfortably clears WAVEFORM_LAG_MIN_CHUNKS_FOR_MAJORITY_
# CHECK chunks of WAVEFORM_LAG_CHUNK_SECONDS each with real margin to spare.
AUDIO_SYNC_FIX_MIN_MEASUREMENT_SECONDS = 24
# An unconditional ceiling the clip editor waits on the ENTIRE measurement (every track combined)
# before giving up and falling back to the configured default for all of them -- deliberately
# independent of measure_clip_audio_sync_shifts_ms's own internal timeouts (confirmed live, twice,
# that those alone weren't enough to keep the UI from appearing stuck in the real packaged app
# even on a short clip, for reasons never conclusively root-caused; every standalone reproduction
# outside the app ran in 1-3 seconds). Enforced via threading.Thread.join(timeout=...), which
# doesn't depend on subprocess/OS process semantics being correct the way those internal timeouts
# do, so it's a genuinely independent backstop.
AUDIO_SYNC_MEASUREMENT_HARD_TIMEOUT_SECONDS = 20


def measure_clip_audio_sync_shifts_ms(
    ffmpeg_path, input_path, reference_track, target_tracks, start_seconds=0, duration_seconds=None,
    progress_callback=None,
):
    """Measures the real, per-track audio sync shift (ms) needed to line each of target_tracks
    back up with reference_track in input_path -- independently per track, not one shared value,
    since real process-capture tracks were confirmed live to each lag Desktop Audio by a
    genuinely DIFFERENT amount within the very same recording (e.g. Discord's own network/
    jitter-buffer pipeline adds latency a locally-rendered game never has) -- a single shared
    correction was confirmed to still leave a visible residual on whichever tracks it didn't
    match.

    progress_callback(index, total, track_number), if given, is called just before starting each
    track's own measurement -- lets a caller show "Measuring track 2 of 4..." while this runs.
    Exceptions from it are swallowed so a UI glitch there can't abort the measurement itself.

    Bounded so this can never hang indefinitely: each extraction has its own subprocess timeout
    (see extract_audio_track_wav), and AUDIO_SYNC_MEASUREMENT_OVERALL_BUDGET_SECONDS caps the
    total wall-clock time across every track combined -- once exceeded, remaining tracks are
    skipped rather than attempted, and the caller decides what to fall back to for them.

    Returns {track_number: shift_ms} (negative advances that track earlier, matching
    DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS's own sign convention) -- only for tracks that
    were both extracted successfully and confidently measured (see measure_waveform_lag_ms); any
    target_tracks missing from the result simply weren't confidently measurable in the time
    available, and the caller decides what to do about those (e.g. fall back to a configured
    default, or leave them unshifted)."""
    tmp_dir = tempfile.mkdtemp(prefix="obsautorec_clipsync_")
    results = {}
    overall_deadline = time.time() + AUDIO_SYNC_MEASUREMENT_OVERALL_BUDGET_SECONDS
    try:
        ref_wav = os.path.join(tmp_dir, "ref.wav")
        if not extract_audio_track_wav(ffmpeg_path, input_path, reference_track, ref_wav, start_seconds, duration_seconds):
            return results
        for index, track in enumerate(target_tracks):
            if time.time() >= overall_deadline:
                logging.warning(
                    "Audio sync measurement: stopping after %.0fs with %d track(s) left unmeasured "
                    "-- falling back to defaults for those.",
                    AUDIO_SYNC_MEASUREMENT_OVERALL_BUDGET_SECONDS, len(target_tracks) - index,
                )
                break
            if progress_callback:
                try:
                    progress_callback(index, len(target_tracks), track)
                except Exception:
                    logging.exception("Audio sync measurement progress callback failed.")
            target_wav = os.path.join(tmp_dir, f"target_{track}.wav")
            if not extract_audio_track_wav(ffmpeg_path, input_path, track, target_wav, start_seconds, duration_seconds):
                continue
            measured_ms = measure_waveform_lag_ms(
                ref_wav, target_wav, label=f"track {track} vs reference track {reference_track}",
            )
            if measured_ms is not None:
                results[track] = -round(measured_ms)
            try:
                os.remove(target_wav)
            except OSError:
                pass
        return results
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_audio_sync_calibration(client, ffmpeg_path, target_input_name, reference_input_name="Desktop Audio"):
    """Live-measures the actual latency difference between target_input_name (a
    wasapi_process_output_capture input -- Game Audio or an app_captures entry) and
    reference_input_name (Desktop Audio) on THIS machine, using the exact method confirmed
    during development: play a short click-tone through a real audio-rendering process (ffplay,
    which needs a genuine visible window for OBS's own process-capture to find it at all -- an
    invisible/-nodisp player was confirmed live to go undetected) while recording both inputs
    simultaneously to a throwaway scratch file, then cross-correlate the two tracks.

    Returns the measured offset in ms to apply to target_input_name (negative -- how much
    earlier it needs to shift to line back up with reference_input_name), or None if calibration
    couldn't complete (ffplay not found, OBS not reachable, or too few clicks detected to be
    confident). Restores every OBS setting it touches -- record directory, target_input_name's
    window/priority, its sync offset, and its track routing -- before returning either way."""
    ffplay_path = os.path.join(os.path.dirname(ffmpeg_path), "ffplay.exe")
    if not os.path.isfile(ffplay_path):
        logging.warning("Audio sync calibration: ffplay.exe not found next to ffmpeg; skipping.")
        return None

    try:
        if client.get_record_status().output_active:
            logging.warning("Audio sync calibration: refusing to run while OBS is already recording.")
            return None
    except Exception as exc:
        logging.warning("Audio sync calibration: could not check OBS's recording state: %s", exc)
        return None

    tmp_dir = tempfile.mkdtemp(prefix="obsautorec_synccal_")
    tone_path = os.path.join(tmp_dir, "tone.wav")
    try:
        generate_calibration_tone(tone_path)
    except Exception as exc:
        logging.warning("Audio sync calibration: could not generate the test tone: %s", exc)
        return None

    try:
        orig_dir = client.get_record_directory().record_directory
        orig_target_settings = client.get_input_settings(target_input_name).input_settings
        orig_target_offset = client.get_input_audio_sync_offset(target_input_name).input_audio_sync_offset
        orig_target_tracks = client.get_input_audio_tracks(target_input_name).input_audio_tracks
        ref_track = next((i for i in range(1, 7) if client.get_input_audio_tracks(reference_input_name).input_audio_tracks.get(str(i))), None)
    except Exception as exc:
        logging.warning("Audio sync calibration: could not read current OBS state: %s", exc)
        return None
    if ref_track is None:
        logging.warning("Audio sync calibration: '%s' isn't routed to any track.", reference_input_name)
        return None

    proc = None
    measured_ms = None
    try:
        client.set_input_settings(
            target_input_name, {"window": "::ffplay.exe", "priority": WINDOW_MATCH_PRIORITY_EXE_FALLBACK}, True,
        )
        client.set_input_audio_sync_offset(target_input_name, 0)
        target_tracks = {str(i): (i == CALIBRATION_TARGET_TRACK) for i in range(1, 7)}
        client.set_input_audio_tracks(target_input_name, target_tracks)
        client.set_record_directory(tmp_dir)
        client.start_record()
        time.sleep(1.0)
        proc = subprocess.Popen([ffplay_path, "-autoexit", "-loglevel", "quiet", tone_path])
        time.sleep(1.5)  # let the window actually appear before ffplay starts playing
        proc.wait(timeout=CALIBRATION_TONE_DURATION_SECONDS + 10)
        time.sleep(1.0)
    except Exception as exc:
        logging.warning("Audio sync calibration: recording step failed: %s", exc)
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        output_path = None
        try:
            resp = client.stop_record()
            output_path = getattr(resp, "output_path", None)
        except Exception as exc:
            logging.warning("Audio sync calibration: could not stop the test recording: %s", exc)
        for attempt in range(8):
            try:
                client.set_record_directory(orig_dir)
                break
            except Exception:
                time.sleep(3)
        try:
            client.set_input_settings(target_input_name, orig_target_settings, False)
            client.set_input_audio_sync_offset(target_input_name, orig_target_offset)
            client.set_input_audio_tracks(target_input_name, orig_target_tracks)
        except Exception as exc:
            logging.warning("Audio sync calibration: could not restore '%s': %s", target_input_name, exc)

        if output_path and os.path.isfile(output_path):
            ref_wav = os.path.join(tmp_dir, "ref.wav")
            target_wav = os.path.join(tmp_dir, "target.wav")
            try:
                subprocess.run(
                    [
                        ffmpeg_path, "-y", "-i", output_path,
                        "-map", f"0:a:{ref_track - 1}", "-c:a", "pcm_s16le", ref_wav,
                        "-map", f"0:a:{CALIBRATION_TARGET_TRACK - 1}", "-c:a", "pcm_s16le", target_wav,
                    ],
                    capture_output=True, **platform_common.hide_console_subprocess_kwargs(), timeout=30,
                )
                measured_ms = measure_audio_lag_ms(ref_wav, target_wav)
            except Exception as exc:
                logging.warning("Audio sync calibration: could not analyze the test recording: %s", exc)
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    if measured_ms is None:
        logging.warning("Audio sync calibration: could not confidently measure a lag; leaving the offset unchanged.")
        return None
    result_ms = -round(measured_ms)
    logging.info(
        "Audio sync calibration measured '%s' lagging '%s' by %.1fms -> offset %sms.",
        target_input_name, reference_input_name, measured_ms, result_ms,
    )
    return result_ms


def apply_output_folder(client, output_folder):
    """Best-effort override of OBS's recording directory for whichever profile is currently
    active (the dedicated multi-track profile if that ran first, otherwise whatever the user
    has selected) -- an explicit `obs.output_folder` always wins over whatever the profile
    already had, including a cloned-over value from multi-track's profile setup."""
    if not output_folder:
        return
    try:
        os.makedirs(output_folder, exist_ok=True)
    except OSError as exc:
        logging.warning("Could not create recording output folder '%s': %s", output_folder, exc)
        return

    try:
        current = client.get_record_directory().record_directory
    except Exception as exc:
        logging.warning("Could not read OBS's current recording directory: %s", exc)
        current = None

    if current and os.path.normpath(current) == os.path.normpath(output_folder):
        return

    try:
        client.set_record_directory(output_folder)
        logging.info("Set OBS's recording output folder to %s", output_folder)
    except Exception as exc:
        logging.error("Could not set OBS's recording output folder to '%s': %s", output_folder, exc)


# OBS's internal RecFormat2 codes shown in the settings editor's Recording format field. Not
# necessarily exhaustive across every OBS version -- the field stays editable (not a locked
# dropdown) so a value outside this list can still be typed in and used as-is.
RECORDING_FORMAT_OPTIONS = ["mp4", "mkv", "mov", "hybrid_mp4", "fragmented_mp4", "fragmented_mov", "flv", "ts", "hls"]

# ffmpeg output container extensions offered in the post-record transcode field. A free-text
# entry here is easy to typo (a missing leading dot, an unsupported extension for the chosen
# args) in a way that only surfaces as a cryptic ffmpeg failure after a recording finishes, so
# this is a dropdown instead -- still editable, in case someone genuinely needs a container not
# listed here. Blank means "keep the original recording's extension".
TRANSCODE_OUTPUT_EXTENSION_OPTIONS = ["", ".mp4", ".mkv", ".mov", ".ts", ".flv", ".webm", ".avi"]


def apply_recording_format(client, recording_format):
    """Best-effort override of OBS's recording container format for whichever profile is
    currently active -- same precedence as apply_output_folder: an explicit
    `obs.recording_format` always wins, including over a cloned-over value from multi-track's
    profile setup. `recording_format` is OBS's own internal format code (e.g. "mkv", "mp4",
    "hybrid_mp4"), not a display label -- whatever value ends up applied, the multi-track sync's
    own format-safety warning (see MULTI_TRACK_SAFE_FORMATS) still fires against it if relevant."""
    if not recording_format:
        return
    current = get_profile_parameter_value(client, "AdvOut", "RecFormat2")
    if current == recording_format:
        return
    try:
        client.set_profile_parameter("AdvOut", "RecFormat2", recording_format)
        logging.info("Set OBS's recording format to '%s'.", recording_format)
    except Exception as exc:
        logging.error("Could not set OBS's recording format to '%s': %s", recording_format, exc)


OBS_RESOLUTION_OPTIONS = ["Match canvas (no scaling)", "1080p", "720p", "480p"]


def resolve_recording_resolution(base_width, base_height, resolution_choice):
    """Resolves an OBS_RESOLUTION_OPTIONS choice to an explicit (width, height) output
    resolution, given the canvas's current base resolution. "Match canvas" (or anything
    unrecognized, e.g. not yet set) just returns the base resolution unchanged -- the same "do
    nothing extra" default OBS itself starts with. A named preset keeps the base canvas's own
    aspect ratio, scaling width from CLIP_EDITOR_QUALITY_HEIGHTS' height and rounding to the
    nearest even number, same convention build_trim_command's -2 scale filter already uses,
    since libx264 (what OBS's own encoders are built on too) requires even dimensions."""
    height = CLIP_EDITOR_QUALITY_HEIGHTS.get(resolution_choice)
    if height is None or not base_width or not base_height:
        return base_width, base_height
    width = round(base_width * height / base_height / 2) * 2
    return width, height


def apply_recording_resolution(client, obs_config):
    """Best-effort override of OBS's output (scaled) resolution -- confirmed live that, unlike
    the recording format or replay buffer, this genuinely takes effect immediately with no OBS
    restart needed. Leaves the canvas (base) resolution and frame rate untouched; only the
    output dimensions actually used for encoding/recording change."""
    resolution_choice = obs_config.get("recording_resolution")
    if not resolution_choice:
        return
    try:
        video = client.get_video_settings()
    except Exception as exc:
        logging.warning("Could not read video settings to apply the configured recording resolution: %s", exc)
        return
    target_width, target_height = resolve_recording_resolution(
        video.base_width, video.base_height, resolution_choice
    )
    if (video.output_width, video.output_height) == (target_width, target_height):
        return
    try:
        client.set_video_settings(
            video.fps_numerator, video.fps_denominator,
            video.base_width, video.base_height, target_width, target_height,
        )
        logging.info("Set OBS's recording resolution to %dx%d.", target_width, target_height)
    except Exception as exc:
        logging.error("Could not set OBS's recording resolution to %dx%d: %s", target_width, target_height, exc)


def wants_markers(custom_keybinds_config):
    return any(
        kb.get("enabled", True) and kb.get("action") == "add_marker" for kb in (custom_keybinds_config or [])
    )


def ensure_hybrid_mp4_for_markers(client):
    """Chapter markers (this app's "Add Marker" keybind -> OBS's CreateRecordChapter) only work
    when the recording format is Hybrid MP4 -- confirmed via python-vlc's own docstring and live
    testing (every other format fails with OBS_CHAPTER_NOT_SUPPORTED_CODE). Forces that format,
    overriding obs.recording_format if it's set to anything else, since markers simply don't work
    otherwise. Returns True when a write actually happened -- same "OBS needs a restart to pick
    this up" limitation as apply_replay_buffer_settings, so callers restart OBS when this does."""
    current = (
        get_profile_parameter_value(client, "AdvOut", "RecFormat2")
        or get_profile_parameter_value(client, "AdvOut", "RecFormat")
    )
    if current == "hybrid_mp4":
        return False
    try:
        client.set_profile_parameter("AdvOut", "RecFormat2", "hybrid_mp4")
        logging.info("Set OBS's recording format to Hybrid MP4 (required for the \"Add Marker\" keybind).")
        return True
    except Exception as exc:
        logging.warning("Could not set OBS's recording format to Hybrid MP4 for markers: %s", exc)
        return False


def apply_replay_buffer_settings(client, replay_buffer_config):
    """Best-effort override of OBS's own Replay Buffer settings (Settings -> Output -> Replay
    Buffer) for whichever profile is currently active -- so choosing a replay buffer mode/length
    in this app's own Settings actually configures OBS itself, rather than calling
    StartReplayBuffer against whatever length OBS happened to already have set. Same
    read-current-value-first precedence as apply_recording_format, and the same reason for
    checking Output Mode first as sync_multi_track_output_settings: Simple and Advanced output
    modes keep entirely separate copies of these settings (SimpleOutput vs AdvOut).

    Returns True when enabling the buffer (or changing its length) actually required writing a
    new value -- confirmed live against a running OBS instance, a profile-parameter write alone
    doesn't make OBS's replay buffer *available* to Start/SaveReplayBuffer in the current session
    (OBS only builds that output when a profile loads); callers use this to know a restart is
    actually needed. Turning the buffer *off* never returns True -- nothing has to restart just to
    stop calling StartReplayBuffer, and a value that was already "off"/unset (e.g. a fresh install
    that never touched this setting) shouldn't force a restart that accomplishes nothing."""
    mode = get_replay_buffer_mode(replay_buffer_config)
    should_enable = mode != "off"
    category = "AdvOut" if get_profile_parameter_value(client, "Output", "Mode", "Simple") == "Advanced" else "SimpleOutput"
    enabled_str = "true" if should_enable else "false"
    needs_restart = False
    try:
        if get_profile_parameter_value(client, category, "RecRB") != enabled_str:
            client.set_profile_parameter(category, "RecRB", enabled_str)
            if should_enable:
                needs_restart = True
        if should_enable:
            max_seconds = replay_buffer_config.get("max_seconds", DEFAULT_REPLAY_BUFFER_SECONDS)
            if get_profile_parameter_value(client, category, "RecRBTime") != str(max_seconds):
                client.set_profile_parameter(category, "RecRBTime", str(max_seconds))
                needs_restart = True
            logging.info("Set OBS's replay buffer to enabled (%ds).", max_seconds)
        else:
            logging.info("Set OBS's replay buffer to disabled.")
    except Exception as exc:
        logging.warning("Could not apply OBS replay buffer settings: %s", exc)
        return False
    return needs_restart


DEFAULT_MULTI_TRACK_PROFILE_NAME = "OBS Auto Recorder"
# Formats OBS has reliably muxed every enabled recording track into. Others (mp4, mov, flv, ...)
# have historically only embedded track 1 in some OBS versions -- routing still happens either way,
# this is just used to decide whether to warn about it.
MULTI_TRACK_SAFE_FORMATS = {"mkv", "fragmented_mkv", "hybrid_mp4"}


def normalize_track_entries(tracks_config):
    """Validates config['obs']['multi_track_audio']['tracks'] into (input_name, track_number) pairs,
    dropping anything malformed rather than raising -- this runs on every recording start."""
    entries = []
    for t in tracks_config or []:
        name = (t.get("input_name") or "").strip()
        if not name:
            continue
        try:
            track = int(t.get("track"))
        except (TypeError, ValueError):
            continue
        if 1 <= track <= 6:
            entries.append((name, track))
    return entries


def get_profile_parameter_value(client, category, name, default=None):
    try:
        resp = client.get_profile_parameter(category, name)
        value = resp.parameter_value
        return value if value is not None else default
    except Exception:
        return default


def snapshot_recording_source_settings(client):
    """Reads the handful of profile settings worth carrying over when creating the dedicated
    multi-track profile, so it starts out recording to the same place, at the same quality, in
    the same format the user already had in their current profile -- we only add track routing
    on top of that, never reinvent their encoder/path setup."""
    settings = {}
    try:
        settings["video"] = client.get_video_settings()
    except Exception as exc:
        logging.warning("Could not read video settings to carry over to the dedicated profile: %s", exc)
    try:
        settings["record_directory"] = client.get_record_directory().record_directory
    except Exception as exc:
        logging.warning("Could not read recording directory to carry over to the dedicated profile: %s", exc)
    settings["rec_format"] = (
        get_profile_parameter_value(client, "AdvOut", "RecFormat2")
        or get_profile_parameter_value(client, "AdvOut", "RecFormat")
        or get_profile_parameter_value(client, "SimpleOutput", "RecFormat2")
        or get_profile_parameter_value(client, "SimpleOutput", "RecFormat")
    )
    return settings


def apply_recording_source_settings(client, settings):
    video = settings.get("video")
    if video:
        try:
            client.set_video_settings(
                video.fps_numerator, video.fps_denominator,
                video.base_width, video.base_height,
                video.output_width, video.output_height,
            )
        except Exception as exc:
            logging.warning("Could not apply carried-over video settings to the dedicated profile: %s", exc)
    record_directory = settings.get("record_directory")
    if record_directory:
        try:
            client.set_record_directory(record_directory)
        except Exception as exc:
            logging.warning("Could not apply carried-over recording directory to the dedicated profile: %s", exc)
    rec_format = settings.get("rec_format")
    if rec_format:
        try:
            client.set_profile_parameter("AdvOut", "RecFormat2", rec_format)
        except Exception as exc:
            logging.warning("Could not apply carried-over recording format to the dedicated profile: %s", exc)


def ensure_dedicated_profile(client, profile_name):
    """Makes sure OBS is on `profile_name`, creating it (cloned from whatever profile is
    currently active) the first time. All multi-track changes only ever land on this profile,
    so the user's own profile is never modified."""
    try:
        profiles = client.get_profile_list()
    except Exception as exc:
        logging.warning("Could not read the OBS profile list: %s", exc)
        return False

    if profiles.current_profile_name == profile_name:
        return True

    if profile_name in profiles.profiles:
        try:
            client.set_current_profile(profile_name)
            logging.info("Switched OBS to the dedicated '%s' profile.", profile_name)
        except Exception as exc:
            logging.error("Could not switch OBS to the '%s' profile: %s", profile_name, exc)
            return False
        return True

    logging.info(
        "Creating a dedicated '%s' OBS profile for multi-track audio, cloned from your current "
        "profile ('%s') so its recording path/quality/format carry over unchanged.",
        profile_name, profiles.current_profile_name,
    )
    source_settings = snapshot_recording_source_settings(client)
    try:
        client.create_profile(profile_name)
    except Exception as exc:
        logging.error("Could not create the '%s' OBS profile: %s", profile_name, exc)
        return False
    apply_recording_source_settings(client, source_settings)
    return True


def sync_multi_track_output_settings(client, entries, profile_name):
    """Ensures Advanced output mode (required for multi-track recording) and a recording-track
    bitmask covering every track referenced in `entries` -- both scoped to the currently active
    (dedicated) profile only."""
    current_mode = get_profile_parameter_value(client, "Output", "Mode", "Simple")
    if current_mode != "Advanced":
        try:
            client.set_profile_parameter("Output", "Mode", "Advanced")
            logging.warning(
                "Switched the dedicated multi-track profile's Output Mode to Advanced (required for "
                "multi-track recording; it was '%s'). If you want to fine-tune recording quality/encoder "
                "for this profile, do it in OBS's Settings > Output while '%s' is the active profile -- "
                "your original profile is untouched.",
                current_mode, profile_name,
            )
        except Exception as exc:
            logging.error("Could not switch Output Mode to Advanced: %s", exc)
            return False

    track_numbers = sorted({track for _, track in entries})
    bitmask = 0
    for track in track_numbers:
        bitmask |= 1 << (track - 1)

    current_bitmask = get_profile_parameter_value(client, "AdvOut", "RecTracks")
    if str(current_bitmask) != str(bitmask):
        try:
            client.set_profile_parameter("AdvOut", "RecTracks", str(bitmask))
            logging.info("Set OBS recording track bitmask to %s (tracks %s).", bitmask, track_numbers)
        except Exception as exc:
            logging.error("Could not set the recording track bitmask: %s", exc)
            return False

    rec_format = (
        get_profile_parameter_value(client, "AdvOut", "RecFormat2")
        or get_profile_parameter_value(client, "AdvOut", "RecFormat")
    )
    if rec_format and rec_format.lower() not in MULTI_TRACK_SAFE_FORMATS:
        logging.warning(
            "The dedicated profile's recording format is '%s', which hasn't always embedded every "
            "audio track reliably in OBS. Tracks are still being routed independently, but if your "
            "recordings only end up with one audio track, switch this profile's Recording Format to "
            "MKV in OBS's Settings > Output (Recording Format) while '%s' is active.",
            rec_format, profile_name,
        )
    return True


# Input kinds whose track routing this feature manages. Used to clear stale routing left over
# on an input that used to be listed in obs.multi_track_audio.tracks and no longer is -- without
# this, removing a mapping in Settings has no effect in OBS, since SetInputAudioTracks is only
# ever called for inputs actually listed. Deliberately narrow (not every input kind) so a device
# with no business being track-routed (e.g. a pure video source) is never touched.
AUDIO_TRACK_MANAGED_KINDS = {
    "wasapi_output_capture",  # Desktop Audio
    "wasapi_input_capture",  # Mic/Aux and other microphone devices
    "wasapi_process_output_capture",  # Application Audio Capture (Discord, Spotify, browsers, ...)
}


def apply_multi_track_routing(client, entries):
    """Routes each configured input to exactly the track(s) listed for it, clearing any track
    not listed -- read-modify-write against GetInputAudioTracks so we never guess at the shape
    of tracks we're not touching. Also clears routing on any other audio-capable input in the
    scene collection that isn't listed at all, so removing a mapping actually takes effect."""
    by_input = {}
    for name, track in entries:
        by_input.setdefault(name, set()).add(track)

    for input_name, desired_tracks in by_input.items():
        desired = {str(i): (i in desired_tracks) for i in range(1, 7)}
        try:
            current = client.get_input_audio_tracks(input_name).input_audio_tracks
        except Exception as exc:
            logging.warning(
                "Could not route audio input '%s' to track(s) %s -- no input with that exact name "
                "exists in the current scene collection (%s). Add it, or fix the name in Settings.",
                input_name, sorted(desired_tracks), exc,
            )
            continue
        if {str(k): bool(v) for k, v in current.items()} == desired:
            continue
        try:
            client.set_input_audio_tracks(input_name, desired)
            logging.info("Routed audio input '%s' to track(s) %s.", input_name, sorted(desired_tracks))
        except Exception as exc:
            logging.warning("Could not set audio track routing for input '%s': %s", input_name, exc)

    try:
        all_inputs = client.get_input_list().inputs
    except Exception as exc:
        logging.warning("Could not list OBS inputs to clear stale track routing: %s", exc)
        return

    cleared = {str(i): False for i in range(1, 7)}
    for input_info in all_inputs:
        name = input_info.get("inputName")
        if not name or name in by_input or input_info.get("inputKind") not in AUDIO_TRACK_MANAGED_KINDS:
            continue
        try:
            current = client.get_input_audio_tracks(name).input_audio_tracks
        except Exception:
            continue
        if not any(current.values()):
            continue
        try:
            client.set_input_audio_tracks(name, cleared)
            logging.info("Cleared stale track routing on '%s' (no longer in multi_track_audio.tracks).", name)
        except Exception as exc:
            logging.warning("Could not clear stale track routing on '%s': %s", name, exc)


def sync_multi_track_audio(client, multi_track_config, process_capture_sync_offset_ms=DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS):
    if not multi_track_config.get("enabled"):
        return
    entries = normalize_track_entries(multi_track_config.get("tracks"))
    if not entries:
        return
    profile_name = multi_track_config.get("profile_name") or DEFAULT_MULTI_TRACK_PROFILE_NAME
    try:
        if not ensure_dedicated_profile(client, profile_name):
            return
        if not sync_multi_track_output_settings(client, entries, profile_name):
            return
        # Re-point every app-audio-capture input at its exe before routing runs below -- both
        # because a missing input needs to exist first to be routed at all (this call creates it
        # if needed, same fallback obs.game_audio_capture already relies on), and because OBS can
        # silently rewrite a wasapi_process_output_capture's exe-only match into a specific window
        # title behind this app's back (e.g. its Properties dialog was ever opened in OBS itself),
        # which then goes stale the moment the app's window title changes (Discord's includes the
        # current server/channel name) -- exactly what "isolation randomly stops working" turns
        # out to be. This re-point isn't a one-time fix, it runs on every recording start, same as
        # obs.game_audio_capture already gets. The sync-offset correction rides along with it for
        # the same reason -- see DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS.
        for app_capture in multi_track_config.get("app_captures", []):
            input_name = app_capture.get("input_name")
            process_name = app_capture.get("process_name")
            if input_name and process_name:
                set_game_audio_capture_target(client, input_name, process_name)
                apply_process_capture_sync_offset(client, input_name, process_capture_sync_offset_ms)
        apply_multi_track_routing(client, entries)
    except Exception:
        logging.exception("Unexpected error while syncing multi-track audio settings.")


DEFAULT_DISK_SPACE_MINIMUM_GB = 10


def has_sufficient_disk_space(disk_guard_config, fallback_path):
    if not disk_guard_config.get("enabled"):
        return True
    check_path = disk_guard_config.get("path") or fallback_path
    try:
        free_gb = shutil.disk_usage(check_path).free / (1024 ** 3)
    except OSError as exc:
        logging.warning("Disk space guard: could not check free space at %s: %s", check_path, exc)
        return True
    minimum_gb = disk_guard_config.get("minimum_free_gb", DEFAULT_DISK_SPACE_MINIMUM_GB)
    return free_gb >= minimum_gb


DEFAULT_STORAGE_RESERVED_FREE_GB = 20

# Recorded/transcoded clip containers this app itself produces (see RECORDING_FORMAT_OPTIONS and
# TRANSCODE_OUTPUT_EXTENSION_OPTIONS above) -- storage management only ever deletes files matching
# one of these, so it can't wander into unrelated files that happen to share the watch folder.
CLIP_FILE_EXTENSIONS = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".webm", ".avi", ".hls", ".m3u8"}


def get_storage_watch_folder(storage_config, obs_config):
    return storage_config.get("watch_folder") or obs_config.get("output_folder")


def iter_clip_files(folder):
    """Yields (path, mtime) for every recorded/transcoded clip under folder, recursively --
    recursive because organize_into_game_subfolders nests clips one level deeper, per game."""
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if os.path.splitext(name)[1].lower() not in CLIP_FILE_EXTENSIONS:
                continue
            path = os.path.join(root, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            yield path, mtime


def enforce_storage_budget(storage_config, obs_config, recording_state=None, icon=None, notifications_config=None):
    """Keeps at least `reserved_free_gb` free on the watch folder's drive by deleting the oldest
    clips first, so the app can be left recording indefinitely without manually clearing space.
    Never touches the file currently being written to by OBS."""
    if not storage_config.get("enabled"):
        return

    folder = get_storage_watch_folder(storage_config, obs_config)
    if not folder or not os.path.isdir(folder):
        logging.warning(
            "Storage management: no valid watch folder configured (set storage_management.watch_folder "
            "or obs.output_folder); skipping."
        )
        return

    reserved_gb = storage_config.get("reserved_free_gb", DEFAULT_STORAGE_RESERVED_FREE_GB)
    try:
        free_gb = shutil.disk_usage(folder).free / (1024 ** 3)
    except OSError as exc:
        logging.warning("Storage management: could not check free space at %s: %s", folder, exc)
        return
    if free_gb >= reserved_gb:
        return

    active_path = os.path.normpath(recording_state["current_path"]) if recording_state and recording_state.get("current_path") else None
    clips = sorted(
        (item for item in iter_clip_files(folder) if os.path.normpath(item[0]) != active_path),
        key=lambda item: item[1],
    )
    if not clips:
        logging.warning(
            "Storage management: free space (%.1f GB) is below the reserved %.1f GB, but no clips "
            "were found in %s to delete.", free_gb, reserved_gb, folder,
        )
        return

    deleted = []
    for path, _mtime in clips:
        if free_gb >= reserved_gb:
            break
        try:
            size_gb = os.path.getsize(path) / (1024 ** 3)
            os.remove(path)
        except OSError as exc:
            logging.error("Storage management: failed to delete %s: %s", path, exc)
            continue
        free_gb += size_gb
        deleted.append(path)
        logging.info("Storage management: deleted oldest clip %s to free up space.", os.path.basename(path))

    if deleted:
        notify(
            icon, notifications_config, "Old clips deleted",
            f"Deleted {len(deleted)} oldest clip(s) to keep {reserved_gb:g} GB free.",
        )
    if free_gb < reserved_gb:
        logging.warning(
            "Storage management: still below the reserved %.1f GB free in %s after deleting %d "
            "clip(s) -- nothing more eligible to delete.", reserved_gb, folder, len(deleted),
        )


# "off": no replay buffer at all (the pre-existing default). "with_recording": today's original
# behavior -- the buffer runs alongside a normal full recording. "only": the watcher starts/stops
# the replay buffer instead of a full recording at all, for someone who only ever wants to save a
# short clip after the fact and doesn't want a continuous recording eating disk space too.
REPLAY_BUFFER_MODES = ["off", "with_recording", "only"]
REPLAY_BUFFER_MODE_LABELS = {
    "off": "Off",
    "with_recording": "With full recording",
    "only": "Replay buffer only (no full recording)",
}
REPLAY_BUFFER_MODE_LABELS_BY_LABEL = {label: mode for mode, label in REPLAY_BUFFER_MODE_LABELS.items()}
DEFAULT_REPLAY_BUFFER_SECONDS = 30


def get_replay_buffer_mode(replay_buffer_config):
    """Resolves obs.replay_buffer's mode, falling back to the old boolean `enabled` field (still
    read here, though the Settings editor and installer both now write `mode` directly) so a
    config saved before this option existed keeps behaving exactly as it did before."""
    mode = replay_buffer_config.get("mode")
    if mode in REPLAY_BUFFER_MODES:
        return mode
    return "with_recording" if replay_buffer_config.get("enabled") else "off"


# Confirmed live against a running OBS instance: writing RecRB/RecRBTime via SetProfileParameter
# (what apply_replay_buffer_settings does) updates the saved profile, but OBS only actually builds
# the replay buffer output when a profile is loaded -- neither a plain profile-parameter write nor
# re-selecting the same profile causes it to reconstruct the output mid-session. So the very first
# time replay buffer is turned on (or its length changed) via this app's Settings, every start/
# save/stop request fails with this code until OBS is restarted, or the same setting is applied
# once through OBS's own Settings -> Output -> Replay Buffer dialog.
OBS_REPLAY_BUFFER_NOT_AVAILABLE_CODE = 604
OBS_REPLAY_BUFFER_NOT_AVAILABLE_HINT = (
    "OBS reports the replay buffer isn't available. This setting was written to OBS's profile, "
    "but OBS only builds the replay buffer output when a profile is loaded, not from a live "
    "settings change -- restart OBS once (or open Settings -> Output -> Replay Buffer in OBS "
    "itself and click OK) after turning this on or changing its length here, then it'll work "
    "normally from then on."
)


def start_replay_buffer(client):
    try:
        client.start_replay_buffer()
        logging.info("Replay buffer started.")
        return True
    except obsws.error.OBSSDKRequestError as exc:
        if exc.code == OBS_REPLAY_BUFFER_NOT_AVAILABLE_CODE:
            logging.error("Could not start replay buffer: %s", OBS_REPLAY_BUFFER_NOT_AVAILABLE_HINT)
        else:
            logging.warning("Could not start replay buffer: %s", exc)
        return False
    except Exception as exc:
        logging.warning("Could not start replay buffer: %s", exc)
        return False


def stop_replay_buffer(client):
    try:
        client.stop_replay_buffer()
        logging.info("Replay buffer stopped.")
    except obsws.error.OBSSDKRequestError as exc:
        if exc.code != OBS_REPLAY_BUFFER_NOT_AVAILABLE_CODE:
            logging.warning("Could not stop replay buffer: %s", exc)
    except Exception as exc:
        logging.warning("Could not stop replay buffer: %s", exc)


def save_replay_buffer(client):
    try:
        client.save_replay_buffer()
        logging.info("Replay buffer saved.")
    except obsws.error.OBSSDKRequestError as exc:
        if exc.code == OBS_REPLAY_BUFFER_NOT_AVAILABLE_CODE:
            logging.error("Could not save replay buffer: %s", OBS_REPLAY_BUFFER_NOT_AVAILABLE_HINT)
        else:
            logging.error("Could not save replay buffer: %s", exc)
    except Exception as exc:
        logging.error("Could not save replay buffer: %s", exc)


# Confirmed live: OBS only supports CreateRecordChapter (this app's "Add Marker" keybind) when
# the recording format is Hybrid MP4 -- every other format fails with this code (shared with
# OBS_SPLIT_NOT_ENABLED_CODE numerically, but a separate name here since the two checks apply to
# entirely different requests and just happen to reuse the same generic "unsupported" code).
OBS_CHAPTER_NOT_SUPPORTED_CODE = 702
OBS_CHAPTER_NOT_SUPPORTED_HINT = (
    "Markers aren't supported by this recording's video format -- OBS only supports them with "
    "Hybrid MP4. Enabling the \"Add Marker\" custom keybind in Settings already forces this "
    "format and restarts OBS to apply it; if you still see this, OBS may not have restarted yet."
)


def add_recording_marker(client, icon=None, notifications_config=None, status=None):
    try:
        client.create_record_chapter()
        logging.info("Added a marker to the current recording.")
        # Same flash treatment as a manual split or a replay-buffer save -- from the user's
        # point of view, all three are "something just landed/happened, here's confirmation".
        # Unlike those two, there's no OBS event for this (CreateRecordChapter doesn't emit
        # one), so the flash has to be fired right here at the point of success instead.
        if icon is not None and status is not None:
            status["flash_until"] = time.time() + SPLIT_FLASH_SECONDS
            icon.icon = build_tray_image(current_color(status))

            def revert():
                icon.icon = build_tray_image(current_color(status))

            timer = threading.Timer(SPLIT_FLASH_SECONDS, revert)
            timer.daemon = True
            timer.start()
        return True
    except obsws.error.OBSSDKRequestError as exc:
        if exc.code == OBS_CHAPTER_NOT_SUPPORTED_CODE:
            logging.error("Could not add marker: %s", OBS_CHAPTER_NOT_SUPPORTED_HINT)
            notify(
                icon, notifications_config, "Marker not added",
                "Markers don't work with this recording's video format -- only Hybrid MP4 supports them.",
            )
        else:
            logging.error("Could not add marker: %s", exc)
        return False
    except Exception as exc:
        logging.error("Could not add marker: %s", exc)
        return False


OBS_SPLIT_NOT_ENABLED_CODE = 702


def ensure_split_enabled(client):
    """OBS's SplitRecordFile request -- used by both this app's own split triggers and OBS's own
    hotkey -- silently fails with error 702 unless "Automatically split file" is ticked in OBS's
    own Settings -> Output, a master switch despite the misleading name (it also gates a purely
    manual split). Auto-enables just that one checkbox before every split attempt, so a
    configured split trigger works out of the box instead of quietly doing nothing until someone
    finds this setting by hand in OBS -- never touches the split type/interval fields next to it,
    so it can't turn on unwanted automatic time/size-based splitting as a side effect."""
    current = get_profile_parameter_value(client, "AdvOut", "RecSplitFile")
    if str(current).strip().lower() == "true":
        return
    try:
        client.set_profile_parameter("AdvOut", "RecSplitFile", "true")
        logging.info("Enabled OBS's \"Automatically split file\" option (required for any split trigger to work).")
    except Exception as exc:
        logging.warning("Could not enable OBS's \"Automatically split file\" option: %s", exc)


def trigger_buffered_split(client, buffer_seconds):
    # The buffer runs on our side, not OBS's: obs-websocket's SplitRecordFile request fires the
    # split immediately and independently of whatever physical hotkey OBS itself has bound for it,
    # so a delay here just means "wait, then call split", not "delay OBS's own hotkey".
    if buffer_seconds > 0:
        logging.info("Splitting recording in %s second(s)...", buffer_seconds)
        time.sleep(buffer_seconds)
    ensure_split_enabled(client)
    try:
        client.split_record_file()
        logging.info("Recording file split.")
    except obsws.error.OBSSDKRequestError as exc:
        if exc.code == OBS_SPLIT_NOT_ENABLED_CODE:
            logging.error(
                "Could not split recording file: OBS has file splitting turned off entirely. This "
                "is a one-time fix in OBS itself, not this app -- go to OBS's Settings -> Output "
                "(Advanced mode) -> Recording, and enable \"Automatically split file\". That master "
                "switch has to be on for ANY split trigger to work (OBS's own hotkey, this app's "
                "tray item, or a custom keybind), even if you never intend to use the actual "
                "automatic time/size-based splitting -- just leave the time/size fields alone."
            )
        else:
            logging.error("Could not split recording file: %s", exc)
    except Exception as exc:
        logging.error("Could not split recording file: %s", exc)


# obs-websocket has no API to read or set what physical key OBS itself has a hotkey bound to (see
# find_ffmpeg/resolve_ffmpeg_path above for the analogous ffmpeg-discovery story -- this one's
# just a hard protocol limitation instead). Rather than only listing/triggering OBS's existing
# hotkeys, this app registers its own system-wide keybinds directly with Windows and calls the
# matching WebSocket action when pressed -- fully independent of whatever OBS has (or hasn't)
# bound, and works even if OBS's own Hotkeys page has never been touched.
CUSTOM_KEYBIND_ACTIONS = {
    "split_record_file": "Split Recording File",
    "add_marker": "Add Marker",
    "save_replay_buffer": "Save Replay Buffer",
    "start_replay_buffer": "Start Replay Buffer",
    "stop_replay_buffer": "Stop Replay Buffer",
    "toggle_replay_buffer": "Toggle Replay Buffer",
    "start_record": "Start Recording",
    "stop_record": "Stop Recording",
    "toggle_record": "Toggle Recording",
    "pause_record": "Pause Recording",
    "resume_record": "Resume Recording",
    "toggle_record_pause": "Toggle Recording Pause",
}
CUSTOM_KEYBIND_ACTIONS_BY_LABEL = {label: action for action, label in CUSTOM_KEYBIND_ACTIONS.items()}
CUSTOM_KEYBIND_KEY_OPTIONS = (
    [str(d) for d in range(10)] + [chr(c) for c in range(65, 91)] + [f"F{n}" for n in range(1, 13)]
)

def describe_keybind(binding):
    parts = [m.capitalize() for m in binding.get("modifiers", [])]
    parts.append(binding.get("key", "?"))
    return "+".join(parts)


def perform_keybind_action(
    client, action, manual_split_buffer_seconds=0, icon=None, notifications_config=None, status=None,
):
    try:
        if action == "split_record_file":
            trigger_buffered_split(client, manual_split_buffer_seconds)
        elif action == "add_marker":
            if not add_recording_marker(client, icon, notifications_config, status):
                # add_recording_marker already logged the specific reason (and toasted it, if the
                # format is the culprit) -- returning here skips the misleading "performed" log.
                return
        elif action == "save_replay_buffer":
            client.save_replay_buffer()
        elif action == "start_replay_buffer":
            client.start_replay_buffer()
        elif action == "stop_replay_buffer":
            client.stop_replay_buffer()
        elif action == "toggle_replay_buffer":
            client.toggle_replay_buffer()
        elif action == "start_record":
            client.start_record()
        elif action == "stop_record":
            client.stop_record()
        elif action == "toggle_record":
            client.toggle_record()
        elif action == "pause_record":
            client.pause_record()
        elif action == "resume_record":
            client.resume_record()
        elif action == "toggle_record_pause":
            client.toggle_record_pause()
        else:
            logging.warning("Unknown custom keybind action: %s", action)
            return
        logging.info("Custom keybind action performed: %s", action)
    except Exception as exc:
        logging.error("Custom keybind action '%s' failed: %s", action, exc)


def fire_custom_keybind(
    binding, get_client, get_manual_split_buffer_seconds, icon=None, notifications_config=None, status=None,
):
    client = get_client()
    if not client:
        logging.warning("Custom keybind %s pressed but OBS is not connected.", describe_keybind(binding))
        return
    perform_keybind_action(
        client, binding.get("action"), get_manual_split_buffer_seconds(), icon, notifications_config, status,
    )


def run_custom_keybind_listener(
    bindings, get_client, get_manual_split_buffer_seconds, icon=None, notifications_config=None, status=None,
    stop_event=None,
):
    """Thin dispatcher -- the real per-OS implementation (RegisterHotKey/GetMessageW on Windows,
    XGrabKey/XNextEvent on Linux/X11, a CGEventTap/CFRunLoop on macOS; a clear logged no-op under
    Wayland) now lives in platform_common.run_custom_keybind_listener() /
    platform_windows.py / platform_linux.py / platform_macos.py, see
    CROSS_PLATFORM_PLAN.md Phase 3. fire_custom_keybind/describe_keybind/notify are passed in
    rather than imported by the backend modules, so those never depend on this one (the
    dependency only ever goes the other way). stop_event: passed straight through so
    platform_windows.py's backend can release its hotkeys the moment shutdown is requested rather
    than waiting on process death -- see that function's own docstring for why this matters."""
    platform_common.run_custom_keybind_listener(
        bindings, get_client, get_manual_split_buffer_seconds, fire_custom_keybind, describe_keybind, notify,
        icon=icon, notifications_config=notifications_config, status=status, stop_event=stop_event,
    )


def run_clip_editor_space_bar_listener(editor_window_handle, on_toggle, stop_event):
    """Thin dispatcher -- the real per-OS implementation (a WH_KEYBOARD_LL low-level keyboard
    hook on Windows, a window-scoped XGrabKey on Linux/X11, a CGEventTap on macOS) now lives in
    platform_common.run_clip_editor_space_bar_listener() / platform_windows.py /
    platform_linux.py / platform_macos.py, see CROSS_PLATFORM_PLAN.md Phase 3."""
    platform_common.run_clip_editor_space_bar_listener(editor_window_handle, on_toggle, stop_event)


def notify(icon, notifications_config, title, message):
    if not (notifications_config or {}).get("enabled"):
        return
    try:
        icon.notify(message, title)
    except Exception as exc:
        logging.debug("Notification failed: %s", exc)


def delete_recording_files(paths):
    for path in paths:
        if not path:
            continue
        try:
            os.remove(path)
            logging.info("Deleted short recording: %s", os.path.basename(path))
        except OSError as exc:
            logging.error("Failed to delete short recording %s: %s", path, exc)


# A remux-only preset: -map 0 keeps every stream (video + every audio track) instead of
# ffmpeg's default of picking just one "best" stream per type, and -c copy repackages them into
# the new container without re-encoding at all -- lossless and fast, so this is the actual
# "preserve every audio track exactly" option for converting a multi-track recording between
# containers (e.g. the MKV this app recommends for multi-track audio, into a more universally
# compatible MP4). The default re-encode preset below now also includes -map 0 so plain
# compression doesn't silently drop extra tracks either, just without the lossless guarantee.
MKV_TO_MP4_PRESERVE_TRACKS_ARGS = ["-map", "0", "-c", "copy"]
MKV_TO_MP4_PRESERVE_TRACKS_EXTENSION = ".mp4"


def find_ffmpeg():
    """Best-effort search for an ffmpeg install already on this PC, so most users never have to
    know or set an ffmpeg path themselves. Thin wrapper kept under this name since many call
    sites throughout this file already use it -- the actual per-OS search logic (PATH lookup
    first, then each OS's own common non-PATH install locations) now lives in
    platform_common.find_ffmpeg_executable() / platform_windows.py / platform_linux.py /
    platform_macos.py, see CROSS_PLATFORM_PLAN.md Phase 1. Returns None if nothing turns up
    anywhere -- the feature is still opt-in and requires an actual ffmpeg somewhere on the
    machine."""
    return platform_common.find_ffmpeg_executable()


def resolve_ffmpeg_path(configured_path):
    """Resolves the ffmpeg_path from config.json to an actual runnable path: an explicit path
    that still exists wins as-is, otherwise falls back to a PATH lookup of whatever string was
    configured (so plain "ffmpeg" keeps working the normal way), and only falls back to
    find_ffmpeg()'s broader search if that specific configured value can't be resolved -- e.g.
    the default "ffmpeg" isn't on PATH, or a previously-set explicit path no longer exists."""
    configured_path = configured_path or "ffmpeg"
    if os.path.isabs(configured_path) and os.path.isfile(configured_path):
        return configured_path
    found = shutil.which(configured_path)
    if found:
        return found
    return find_ffmpeg()


FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"


def has_winget():
    """Thin dispatcher -- True if this OS's silent-install mechanism is available (winget on
    Windows, Homebrew on macOS) -- see platform_common.has_package_manager() /
    platform_windows.py / platform_macos.py, CROSS_PLATFORM_PLAN.md Phase 7. Always False on
    Linux, which never does a silent background install at all (§2.7) -- every call site below
    hides its "Easy Install"-style button in that case, same as it always did for "no winget"."""
    if sys.platform not in ("win32", "darwin"):
        return False
    return platform_common.has_package_manager()


def winget_install_ffmpeg(timeout=600):
    """Best-effort silent ffmpeg install -- winget on Windows, Homebrew on macOS. Returns
    (True, None) on success, (False, reason) otherwise; never raises. Kept under this name since
    call sites throughout this file already use it; the real per-OS mechanism now lives in
    platform_common.install_optional_dependency()."""
    return platform_common.install_optional_dependency("ffmpeg", timeout=timeout)


VLC_DOWNLOAD_URL = "https://www.videolan.org/vlc/"


def find_vlc():
    """Best-effort search for a VLC install on this PC. Deliberately does NOT import the `vlc`
    module -- see import_vlc_module()'s docstring for why that has to stay separate. Thin wrapper
    kept under this name since many call sites throughout this file already use it -- the actual
    per-OS search logic now lives in platform_common.find_vlc_directory() /
    platform_windows.py / platform_linux.py / platform_macos.py, see
    CROSS_PLATFORM_PLAN.md Phase 1. Returns the install directory, or None."""
    return platform_common.find_vlc_directory()


def resolve_vlc_path(configured_path):
    """Resolves clip_editor.vlc_path to an actual VLC install directory: an explicit configured
    directory that still contains this OS's own libvlc file wins as-is (mirrors
    resolve_ffmpeg_path's precedence -- previously hardcoded "libvlc.dll" here specifically,
    which was actually a latent bug on any OS but Windows, now fixed by asking
    platform_common for the right filename), otherwise falls back to find_vlc()'s
    auto-detection."""
    libvlc_filename = platform_common.libvlc_filename()
    if configured_path and os.path.isfile(os.path.join(configured_path, libvlc_filename)):
        return configured_path
    return find_vlc()


def winget_install_vlc(timeout=600):
    """Best-effort silent VLC install -- winget on Windows, Homebrew on macOS. Returns
    (True, None) on success, (False, reason) otherwise; never raises. Mirrors
    winget_install_ffmpeg()'s own dispatch to platform_common.install_optional_dependency()."""
    return platform_common.install_optional_dependency("vlc", timeout=timeout)


def import_vlc_module():
    """Lazily imports python-vlc -- only ever call this once find_vlc() has already confirmed an
    install exists, and never at module load time. python-vlc's own DLL discovery (vlc.py's
    module-level find_lib(), which runs the instant `import vlc` executes) raises a bare,
    uncaught OSError if libvlc can't be found at all, with no fallback -- so an unconditional
    `import vlc` at the top of this file would crash this entire app on startup for every user
    who doesn't have VLC installed, even though the vast majority of this app's features have
    nothing to do with it. Returns the vlc module, or None if it couldn't be loaded for any
    reason (VLC uninstalled between find_vlc() and this call, a corrupt install, etc.)."""
    try:
        import vlc
        return vlc
    except Exception as exc:
        logging.warning("Could not load VLC (python-vlc/libvlc): %s", exc)
        return None


# libvlc's own default logging writes raw, unformatted text straight to the process's real
# stderr -- including a one-time "stale plugins cache" rebuild notice some Windows installs print
# after VLC updates itself, one line per plugin. That specific burst happens synchronously inside
# vlc.Instance() construction, before there's an instance to attach a log callback to, so no
# instance-level API can redirect or suppress it individually -- it's harmless and self-resolving
# (the cache stays fresh after that first run), and --quiet below keeps it off a console this app
# may not even have (--noconsole builds) without also silencing the log_set() callback: --quiet
# only mutes libvlc's own default output sink, it doesn't lower what still reaches log_set().
_VLC_LOG_LEVEL_MAP = {}


def create_vlc_instance_with_logging(vlc_module, args=None):
    """Creates a vlc.Instance() and routes every log message it emits from that point on (real
    playback/codec errors in particular) into this app's own logging via libvlc's log_set()
    callback, instead of discarding them or leaking raw text to a console that may not exist."""
    args = list(args or [])
    if "--quiet" not in args:
        args.append("--quiet")
    instance = vlc_module.Instance(*args)

    if not _VLC_LOG_LEVEL_MAP:
        _VLC_LOG_LEVEL_MAP.update({
            vlc_module.LogLevel.DEBUG: logging.DEBUG,
            vlc_module.LogLevel.NOTICE: logging.INFO,
            vlc_module.LogLevel.WARNING: logging.WARNING,
            vlc_module.LogLevel.ERROR: logging.ERROR,
        })

    try:
        # vsnprintf is what actually expands libvlc's C-style varargs log message -- it lives in
        # msvcrt on Windows, but msvcrt itself doesn't exist at all on Linux/macOS. A NULL handle
        # to ctypes.CDLL on POSIX resolves against the process's own already-loaded symbols
        # (which always includes libc, dlopen(NULL, ...) under the hood), so this needs an
        # explicit per-OS load rather than one hardcoded library name.
        libc = ctypes.CDLL("msvcrt") if sys.platform == "win32" else ctypes.CDLL(None)
        libc.vsnprintf.restype = ctypes.c_int
        libc.vsnprintf.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p]

        @vlc_module.CallbackDecorators.LogCb
        def on_vlc_log(_data, level, _ctx, fmt, log_args):
            buf = ctypes.create_string_buffer(2048)
            n = libc.vsnprintf(buf, len(buf), fmt, log_args)
            text = buf.raw[:n].decode("utf-8", errors="replace") if n and n > 0 else "<unreadable log message>"
            logging.log(_VLC_LOG_LEVEL_MAP.get(level, logging.DEBUG), "libvlc: %s", text)

        instance.log_set(on_vlc_log, None)
        # ctypes callbacks are only kept alive by Python references -- without holding this one
        # on the instance itself, it can be garbage-collected while libvlc still expects to call
        # it, which segfaults the process rather than raising a catchable Python exception.
        instance._log_callback_ref = on_vlc_log
    except Exception as exc:
        logging.warning("Could not attach VLC log capture: %s", exc)

    return instance


def transcode_recording(input_path, transcode_config, icon=None, notifications_config=None):
    configured_ffmpeg_path = transcode_config.get("ffmpeg_path", "ffmpeg")
    args = transcode_config.get("args", ["-map", "0", "-c:v", "libx264", "-crf", "23", "-c:a", "aac"])
    suffix = transcode_config.get("suffix", "_compressed")
    delete_original = transcode_config.get("delete_original", False)
    output_extension = transcode_config.get("output_extension") or None

    directory = os.path.dirname(input_path)
    base, ext = os.path.splitext(os.path.basename(input_path))
    output_path = os.path.join(directory, f"{base}{suffix}{output_extension or ext}")

    ffmpeg_path = resolve_ffmpeg_path(configured_ffmpeg_path)
    if not ffmpeg_path:
        logging.error(
            "ffmpeg not found (checked '%s', PATH, and common install locations); skipping "
            "post-record transcode for %s. The original recording is untouched -- install ffmpeg "
            "(e.g. `winget install ffmpeg`) or set post_record_transcode.ffmpeg_path to its exact "
            "location, then use Settings > Post-Processing > Auto-detect ffmpeg.",
            configured_ffmpeg_path, os.path.basename(input_path),
        )
        notify(
            icon, notifications_config, "ffmpeg not found",
            f"{os.path.basename(input_path)} was kept, but not transcoded: ffmpeg isn't installed or configured.",
        )
        return

    cmd = [ffmpeg_path, "-y", "-i", input_path] + list(args) + [output_path]
    logging.info("Transcoding %s with ffmpeg: %s", os.path.basename(input_path), " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, **platform_common.hide_console_subprocess_kwargs()
        )
    except OSError as exc:
        logging.error(
            "Could not run ffmpeg at '%s' for %s: %s. The original recording is untouched.",
            ffmpeg_path, os.path.basename(input_path), exc,
        )
        notify(
            icon, notifications_config, "ffmpeg failed to run",
            f"{os.path.basename(input_path)} was kept, but not transcoded: {exc}",
        )
        return
    if result.returncode != 0:
        logging.error(
            "ffmpeg transcode failed for %s (exit code %s).\nCommand: %s\nstderr:\n%s",
            input_path, result.returncode, " ".join(cmd), result.stderr[-4000:],
        )
        notify(
            icon, notifications_config, "Transcode failed",
            f"{os.path.basename(input_path)} was kept, but ffmpeg failed to transcode it -- see the log for details.",
        )
        return
    logging.info("Transcoded to %s", os.path.basename(output_path))
    if delete_original:
        try:
            os.remove(input_path)
        except OSError as exc:
            logging.warning("Could not delete original after transcode: %s", exc)


def apply_audio_sync_shift(input_path, audio_sync_shift_config, icon=None, notifications_config=None):
    """Runs a standalone ffmpeg pass over a finished recording that shifts the configured
    track(s) by shift_ms -- for fixing a recording made before
    DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS (or a live calibration) was applied at record
    time, whose isolated audio track(s) still sound a few ms out of sync with the rest. See
    build_audio_track_filter_args for the actual filter this builds.

    Independent of post_record_transcode -- its own enabled/ffmpeg_path/delete_original, so it
    can run whether or not transcoding is also configured. Video is always stream-copied; every
    audio track gets re-encoded to AAC uniformly (see build_audio_track_filter_args) since a
    filter graph can't be stream-copied. Blocking; callers run this on a background thread the
    same way transcode_recording's callers do.

    Returns the resulting path on success (the caller doesn't need it today, but it mirrors
    transcode_recording's shape for anyone chaining post-record steps together later), or None if
    nothing was done or the shift failed."""
    if not audio_sync_shift_config.get("enabled") or not input_path:
        return None
    shift_ms = audio_sync_shift_config.get("shift_ms", 0)
    tracks = audio_sync_shift_config.get("tracks") or []
    if not shift_ms or not tracks:
        return None

    configured_ffmpeg_path = audio_sync_shift_config.get("ffmpeg_path", "ffmpeg")
    ffmpeg_path = resolve_ffmpeg_path(configured_ffmpeg_path)
    if not ffmpeg_path:
        logging.error(
            "ffmpeg not found (checked '%s', PATH, and common install locations); skipping the "
            "audio sync shift for %s. The original recording is untouched.",
            configured_ffmpeg_path, os.path.basename(input_path),
        )
        notify(
            icon, notifications_config, "ffmpeg not found",
            f"{os.path.basename(input_path)} was kept, but its audio sync shift was skipped: ffmpeg isn't installed or configured.",
        )
        return None

    basename = os.path.basename(input_path)
    audio_stream_count = probe_audio_stream_count(ffmpeg_path, input_path)
    if not audio_stream_count:
        logging.warning("Audio sync shift: could not determine %s's audio track count; skipping.", basename)
        return None
    filter_complex_args, map_args = build_audio_track_filter_args(
        audio_stream_count, {t: shift_ms for t in tracks},
    )
    if not filter_complex_args:
        logging.warning(
            "Audio sync shift: none of the configured track(s) %s exist in %s (it has %s audio "
            "track(s)); skipping.", tracks, basename, audio_stream_count,
        )
        return None

    suffix = audio_sync_shift_config.get("suffix", "_synced")
    directory = os.path.dirname(input_path)
    base, ext = os.path.splitext(basename)
    output_path = os.path.join(directory, f"{base}{suffix}{ext}")
    delete_original = audio_sync_shift_config.get("delete_original", False)

    cmd = [
        ffmpeg_path, "-y", "-i", input_path, "-map", "0:v", "-c:v", "copy",
        *filter_complex_args, *map_args, "-c:a", "aac", "-movflags", "+faststart", output_path,
    ]
    logging.info("Applying audio sync shift to %s: %s", basename, " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, **platform_common.hide_console_subprocess_kwargs())
    except OSError as exc:
        logging.error("Could not run ffmpeg at '%s' to shift audio for %s: %s", ffmpeg_path, basename, exc)
        notify(icon, notifications_config, "Audio sync shift failed", f"Could not shift {basename}: ffmpeg failed to run.")
        return None

    output_ok = os.path.isfile(output_path) and os.path.getsize(output_path) > 0
    if result.returncode != 0 or not output_ok:
        logging.error(
            "Audio sync shift failed for %s (exit code %s).\nCommand: %s\nstderr:\n%s",
            input_path, result.returncode, " ".join(cmd), result.stderr[-4000:],
        )
        notify(
            icon, notifications_config, "Audio sync shift failed",
            f"{basename} was kept, but its audio sync shift failed -- see the log for details.",
        )
        if os.path.isfile(output_path) and not output_ok:
            try:
                os.remove(output_path)
            except OSError:
                pass
        return None

    logging.info("Audio sync shift applied: %s", output_path)
    notify(icon, notifications_config, "Audio sync shift applied", os.path.basename(output_path))
    if delete_original:
        try:
            os.remove(input_path)
            logging.info("Deleted original recording after audio sync shift: %s", basename)
        except OSError as exc:
            logging.warning("Could not delete original after audio sync shift: %s", exc)
    return output_path


def parse_timestamp(text):
    """Parses a "HH:MM:SS.mmm" / "MM:SS.mmm" / "SS.mmm" timestamp (colon-separated, most-
    significant component first, any number of fractional digits) into a float number of
    seconds. Raises ValueError with a message fit to show directly in the editor's status label
    on anything empty, unparseable, negative, or with an out-of-range minutes/seconds field."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Timestamp is empty.")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"Invalid timestamp '{text}'.")
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Invalid timestamp '{text}'.")
    if any(v < 0 for v in values):
        raise ValueError(f"Timestamp '{text}' can't be negative.")
    # A bare number (no colons) is a plain seconds value with no upper bound -- "90" means 90
    # seconds, not an out-of-range SS field. The <60 check only makes sense once there's a more
    # significant component (minutes and/or hours) actually written alongside it.
    if len(values) == 1:
        return values[0]
    while len(values) < 3:
        values.insert(0, 0.0)
    hours, minutes, seconds = values
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Invalid timestamp '{text}': minutes and seconds must be under 60.")
    return hours * 3600 + minutes * 60 + seconds


def format_timestamp(total_seconds):
    """Formats a float number of seconds as "HH:MM:SS.mmm" -- the inverse of parse_timestamp,
    and also what gets passed to ffmpeg's -ss/-t flags in build_trim_command."""
    total_seconds = max(total_seconds, 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"


# libvlc/avcodec per-media options for the clip editor's own preview playback only -- never
# touches the actual trimmed output, which always goes through ffmpeg separately. Confirmed live
# (via a controlled test playing the same file with/without these) that hardware decoding itself
# wasn't the specific cause of a playback stutter investigated in this app -- but on a machine
# where the GPU/driver genuinely is the bottleneck, trading decode quality for speed here (or
# forcing software decoding) is still a real, useful lever, so it's exposed rather than assumed
# away. Applied via Media.add_option() before the media is ever handed to the player, since that's
# the only point these can be set at all.
CLIP_EDITOR_PREVIEW_QUALITY_OPTIONS = [
    "Best (hardware decode)", "Balanced", "Performance (software decode)",
]
# Confirmed live: VLC's hardware-accelerated decode path (D3D11VA) showed real stutter
# ("picture is too late to be displayed") on Hybrid MP4 recordings during development --
# Performance is the safer default for the editor's own preview until that's understood better,
# not "Best" despite the label. Never touches the exported file either way.
CLIP_EDITOR_DEFAULT_PREVIEW_QUALITY = CLIP_EDITOR_PREVIEW_QUALITY_OPTIONS[2]
CLIP_EDITOR_PREVIEW_QUALITY_MEDIA_OPTIONS = {
    "Best (hardware decode)": [],
    "Balanced": [":avcodec-skiploopfilter=nonref"],
    "Performance (software decode)": [":avcodec-hw=none", ":avcodec-skiploopfilter=all", ":avcodec-fast"],
}

CLIP_EDITOR_TARGET_AUDIO_BITRATE_KBPS = 128
# Real-world encodes tend to land slightly above a pure bitrate x duration prediction (container
# overhead, VBV peaks near hard cuts/scene changes) -- this leaves headroom so "target 10 MB"
# reliably comes out at or under 10 MB instead of just over it.
CLIP_EDITOR_BITRATE_SAFETY_MARGIN = 0.95


def compute_target_video_bitrate_kbps(duration_seconds, target_size_mb, audio_bitrate_kbps=CLIP_EDITOR_TARGET_AUDIO_BITRATE_KBPS):
    """Resolves a target output file size to the video bitrate (kbps) needed to hit it, given the
    clip's duration and a fixed audio bitrate carved out of the same size budget. 1 MB is treated
    as 8192 kbit (the 1024-based convention most "target file size" bitrate calculators use).
    Returns None if there's no target, a non-positive duration, or not enough of the budget left
    for any video bitrate at all once audio's fixed share is subtracted."""
    if not target_size_mb or duration_seconds <= 0:
        return None
    total_kbps = (target_size_mb * 8192 * CLIP_EDITOR_BITRATE_SAFETY_MARGIN) / duration_seconds
    video_kbps = int(total_kbps - audio_bitrate_kbps)
    return video_kbps if video_kbps > 0 else None


def parse_track_numbers(text):
    """Parses a comma-separated "3, 4, 5" style entry (as typed into the audio-sync-shift UI)
    into [3, 4, 5], silently dropping anything that isn't a positive integer rather than raising
    -- a stray comma or space shouldn't block a trim. Returns [] for blank/None input."""
    numbers = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if n > 0:
            numbers.append(n)
    return numbers


def build_audio_track_filter_args(audio_stream_count, shift_ms_by_track=None, muted_tracks=None):
    """Builds (-filter_complex ..., -map ... -map ...) args that independently shift and/or mute
    given 1-based OBS track number(s) (== ffmpeg audio stream index N-1, the same convention
    run_audio_sync_calibration already relies on) while leaving every other track's content
    unchanged.

    shift_ms_by_track: {track_number: shift_ms} -- a per-track amount rather than one uniform
    value, since real isolated app-audio tracks (Game Audio, Discord, ...) were confirmed live to
    each lag Desktop Audio by a genuinely DIFFERENT amount in the same recording (e.g. Discord's
    own network/jitter-buffer pipeline adds latency a locally-rendered game never has), so a
    single shared correction still leaves a visible residual on whichever tracks it doesn't
    match. Positive delays a given track (adelay); negative advances it (atrim + asetpts,
    dropping the first |shift_ms| of that track so everything after it lines up that much
    earlier).

    Muting silences a track (volume=0) while still keeping it present in the output -- e.g. to
    drop a Discord call or background music from a shared clip without losing the multi-track
    structure a player like Premiere expects. A track in both shift_ms_by_track and muted_tracks
    is just muted -- there's nothing audible left for a shift to line up.

    Every audio stream is routed through the SAME filter graph -- muted tracks via volume=0,
    shifted tracks via adelay/atrim, every other track via a harmless no-op (anull) -- rather than
    mixing filtered and plain-mapped streams, so the caller can always re-encode every audio
    stream with one uniform -c:a instead of juggling per-stream copy/re-encode codec flags.
    Returns ([], []) if there's nothing to do (audio_stream_count is falsy, or neither
    shift_ms_by_track nor muted_tracks land within [1, audio_stream_count]) -- the caller falls
    back to its own plain "-map 0:a" wildcard in that case."""
    if not audio_stream_count:
        return [], []
    shift_by_index = {
        t - 1: ms for t, ms in (shift_ms_by_track or {}).items() if 1 <= t <= audio_stream_count and ms
    }
    muted_indexes = {t - 1 for t in (muted_tracks or []) if 1 <= t <= audio_stream_count}
    if not shift_by_index and not muted_indexes:
        return [], []
    filter_parts = []
    map_args = []
    for i in range(audio_stream_count):
        label = f"atrack{i}"
        if i in muted_indexes:
            filter_parts.append(f"[0:a:{i}]volume=0[{label}]")
        elif i in shift_by_index:
            shift_ms = shift_by_index[i]
            if shift_ms > 0:
                filter_parts.append(f"[0:a:{i}]adelay={int(round(shift_ms))}:all=1[{label}]")
            else:
                filter_parts.append(f"[0:a:{i}]atrim=start={abs(shift_ms) / 1000:.6f},asetpts=PTS-STARTPTS[{label}]")
        else:
            filter_parts.append(f"[0:a:{i}]anull[{label}]")
        map_args += ["-map", f"[{label}]"]
    return ["-filter_complex", ";".join(filter_parts)], map_args


def build_audio_routing_filter_args(
    audio_stream_count, routing=None, muted_destinations=None, shift_ms_by_track=None, gains_db=None,
):
    """Builds (-filter_complex ..., -map ... -map ...) args for the clip editor's Track Routing
    dialog -- lets a user consolidate multiple source tracks into fewer output tracks (e.g. mix
    Spotify and Firefox together), drop a source entirely (e.g. remove Desktop Audio from the
    export), mute an output track, and/or boost or attenuate a specific source's gain going into a
    specific output, on top of whatever per-source sync-fix shift already applies -- rather than
    the simpler 1:1 build_audio_track_filter_args, which has no notion of combining or dropping
    tracks at all.

    routing: {destination_track: [source_track, ...]} -- which 1-based source track(s) (as in the
    original file) get mixed together into each 1-based destination track of the output. Defaults
    to an identity mapping (each source becomes its own same-numbered destination) when not
    given. A destination with 2+ sources is combined via ffmpeg's amix (auto-normalized so
    combining doesn't clip); with exactly 1, it's a straight passthrough. A source not referenced
    by ANY destination is dropped from the output entirely -- this is how "consolidate tracks 2-6
    into track 1 and remove track 1 (Desktop Audio)" is expressed: route sources 2-6 to
    destination 1, and simply never list source 1 under any destination.

    muted_destinations: DESTINATION track numbers (post-routing/mixing) to silence (volume=0)
    while still keeping them present in the output -- e.g. muting destination 2 silences whatever
    ended up mixed into it, regardless of which source(s) that was.

    shift_ms_by_track: SOURCE track numbers (pre-routing, same convention as
    measure_clip_audio_sync_shifts_ms) to shift before mixing, so a per-track sync-fix correction
    still applies correctly to a source even after it's combined with others.

    gains_db: {destination_track: {source_track: gain_db}} -- an optional dB gain (positive
    boosts, negative attenuates) applied to one SOURCE only as it feeds into one particular
    DESTINATION, before mixing -- e.g. boosting a mic +12dB into destination 1 but leaving it
    unboosted into destination 2, where the same source is routed to both. Applied per
    (destination, source) pair rather than per source, precisely because the same source can need
    different treatment depending on which output track it ends up in -- a per-source-only gain
    couldn't express that. A missing or 0 entry means no change for that pair.

    Returns ([], []) if the result would be a pure identity passthrough with nothing muted,
    shifted, or gained -- the caller falls back to its own plain "-map 0:a" wildcard (eligible for
    a fast stream copy) in that case, same as build_audio_track_filter_args always has."""
    if not audio_stream_count:
        return [], []
    identity_routing = {t: [t] for t in range(1, audio_stream_count + 1)}
    routing = routing if routing is not None else identity_routing
    muted_destinations = set(muted_destinations or [])
    shift_by_source = {t: ms for t, ms in (shift_ms_by_track or {}).items() if ms}
    gains_db = gains_db or {}
    has_gains = any(
        gains_db.get(dest, {}).get(s) for dest, sources in routing.items() for s in sources
    )

    if not muted_destinations and not shift_by_source and not has_gains and routing == identity_routing:
        return [], []

    referenced_sources = sorted({
        s for sources in routing.values() for s in sources if 1 <= s <= audio_stream_count
    })
    if not referenced_sources:
        return [], []

    # Stage 1: per-source filters (shift only -- destination muting happens after mixing, in
    # stage 2 below, so it silences the COMBINED result of everything routed there rather than
    # just one contributing source; gain happens in stage 2 too, and specifically NOT here,
    # since gain can differ per destination for the very same source -- a single per-source
    # filter here couldn't express "boost this source on destination 1 but not destination 2").
    filter_parts = []
    for s in referenced_sources:
        i = s - 1
        label = f"asrc{s}"
        if s in shift_by_source:
            shift_ms = shift_by_source[s]
            if shift_ms > 0:
                filter_parts.append(f"[0:a:{i}]adelay={int(round(shift_ms))}:all=1[{label}]")
            else:
                filter_parts.append(f"[0:a:{i}]atrim=start={abs(shift_ms) / 1000:.6f},asetpts=PTS-STARTPTS[{label}]")
        else:
            filter_parts.append(f"[0:a:{i}]anull[{label}]")

    # Stage 2: apply this destination's own gain (if any) to its own copy of each source, mix
    # each destination's (possibly gain-adjusted) source(s) together -- or pass the one straight
    # through -- then mute if asked. A destination with no valid sources at all is simply
    # omitted from the output.
    map_args = []
    for dest in sorted(routing.keys()):
        sources = [s for s in routing[dest] if 1 <= s <= audio_stream_count]
        if not sources:
            continue
        dest_gains = gains_db.get(dest, {})
        mix_inputs = []
        any_gain_applied = False
        for s in sources:
            gain = dest_gains.get(s)
            if gain:
                any_gain_applied = True
                gained_label = f"again{dest}_{s}"
                filter_parts.append(f"[asrc{s}]volume={gain}dB[{gained_label}]")
                mix_inputs.append(gained_label)
            else:
                mix_inputs.append(f"asrc{s}")
        if len(mix_inputs) == 1:
            mixed_label = mix_inputs[0]
        else:
            mixed_label = f"amix{dest}"
            inputs = "".join(f"[{label}]" for label in mix_inputs)
            # amix's own "normalize" option defaults ON -- it silently rescales EVERY input by
            # 1/N regardless of any gain already applied above, which would otherwise cancel out
            # a meaningful chunk of a deliberate boost the moment 2+ sources land in the same
            # destination (confirmed live: mixing 2 sources with one boosted +12dB measured a
            # further -6dB from amix's own normalization on top -- exactly 1/2 in dB -- and it
            # only gets worse with more sources mixed together). Once the user has taken explicit
            # control of one source's relative level here, amix re-normalizing on top of that
            # would just be fighting the user's own choice -- so it's turned off for any
            # destination that actually used a gain. Left at its default (on) when no gain was
            # set for this destination at all, so plain track consolidation (the original,
            # gain-less feature) keeps its prior auto-balanced behavior unchanged.
            normalize_arg = ":normalize=0" if any_gain_applied else ""
            filter_parts.append(
                f"{inputs}amix=inputs={len(mix_inputs)}:duration=longest:"
                f"dropout_transition=0{normalize_arg}[{mixed_label}]"
            )
        if dest in muted_destinations:
            final_label = f"adest{dest}"
            filter_parts.append(f"[{mixed_label}]volume=0[{final_label}]")
        else:
            final_label = mixed_label
        map_args += ["-map", f"[{final_label}]"]
    if not map_args:
        return [], []
    return ["-filter_complex", ";".join(filter_parts)], map_args


def probe_audio_stream_count(ffmpeg_path, input_path):
    """Best-effort count of audio streams in input_path via ffprobe, assumed to sit next to
    ffmpeg_path -- the normal case for a real ffmpeg install (confirmed live: winget's ffmpeg
    package puts both in the same bin/ folder). build_audio_track_filter_args needs the real
    count to correctly re-map every track when only some of them are being shifted or muted;
    returns None (rather than guessing) if ffprobe can't be found or the probe fails, since a
    caller can't safely build a filter graph without knowing it."""
    ffprobe_path = platform_common.find_ffprobe_executable(ffmpeg_path)
    if not ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_path, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                "-of", "csv=p=0", input_path,
            ],
            capture_output=True, text=True, **platform_common.hide_console_subprocess_kwargs(), timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    count = len([line for line in result.stdout.splitlines() if line.strip()])
    return count or None


def build_trim_command(
    ffmpeg_path, input_path, start_seconds, end_seconds, output_path, precise=False, crf=None,
    scale_height=None, audio_shift_ms_by_track=None, audio_stream_count=None,
    audio_routing=None, muted_destinations=None, gains_db=None,
):
    """Builds the ffmpeg argv to cut [start_seconds, end_seconds) out of input_path. Always uses
    -t (duration) rather than -to (absolute end time) even though both express the same cut --
    -to's meaning shifts depending on whether -ss is an input or output option, a well-known
    ffmpeg gotcha; -t means the same thing either way, so it sidesteps that ambiguity entirely.

    Fast mode (the default): -ss before -i uses the demuxer's own fast seek, paired with -c copy
    for a lossless, near-instant stream-copy trim -- but the actual cut snaps to the nearest
    keyframe at or before start_seconds, which can be off by a couple of seconds depending on the
    source's keyframe interval. Only available when crf and scale_height are both None ("Same as
    source") -- a stream copy can't change quality or resolution at all, so requesting either
    forces a re-encode.

    Precise mode: -ss after -i decodes from the start of the file up to the cut point instead
    (slower), paired with a re-encode, since a non-keyframe-aligned start can't be stream-copied
    at all -- this is what actually buys frame accuracy, not just a different flag position.

    crf: an explicit libx264 CRF (lower = higher quality), or None to leave quality unforced.
    scale_height: an explicit output height in pixels (e.g. 720 for "720p"), applied via
    -vf scale=-2:HEIGHT (the -2 keeps the source aspect ratio, rounded to an even width as
    required by libx264), or None to leave the source resolution untouched.
    Either one forces a re-encode even in fast (non-precise) mode when given -- in that case -ss
    still goes before -i for the fast-seek speed benefit, it just re-encodes afterward instead of
    copying.

    A target output *size* (as opposed to quality) isn't handled here at all -- see
    build_two_pass_size_targeted_commands, which needs a fundamentally different (two-pass)
    command shape to hit a size target accurately rather than just approximately.

    audio_shift_ms_by_track/audio_stream_count: shift each given 1-based OBS track number by its
    own ms amount (a per-track dict, since real process-capture tracks were confirmed live to
    each lag Desktop Audio by a genuinely different amount) to fix a clip whose isolated audio
    track(s) still sound a few ms out of sync with the rest. audio_routing/muted_destinations/
    gains_db: the clip editor's Track Routing dialog -- consolidate multiple source tracks into
    fewer output tracks, drop a source entirely, mute an output track, and/or boost/attenuate one
    source's gain into one specific output track; see build_audio_routing_filter_args, which this
    delegates to (and which also applies audio_shift_ms_by_track to the right SOURCE tracks before
    any consolidation mixes them together). Requires audio_stream_count (e.g. from
    probe_audio_stream_count) to correctly re-map every track; using any of this forces the AUDIO
    side to re-encode (a filter graph can't be stream-copied) but never touches video's own
    copy-vs-re-encode decision above."""
    start_str = format_timestamp(start_seconds)
    duration_str = format_timestamp(end_seconds - start_seconds)
    filter_complex_args, shifted_audio_map_args = build_audio_routing_filter_args(
        audio_stream_count, audio_routing, muted_destinations, audio_shift_ms_by_track, gains_db,
    )
    filtering_audio = bool(filter_complex_args)
    # Only video and audio -- not "-map 0" for every stream. OBS's Hybrid MP4 recordings carry
    # an extra "bin_data" chapter-metadata track (its own custom format for the marker feature)
    # whose internal timestamps a plain stream copy can't rebase to the new, shorter timeline.
    # Confirmed live: leaving it in produced a copied chapter track reporting a wildly wrong
    # duration (visible in ffprobe, and very likely what made VLC show an inaccurate length for
    # the trimmed clip) -- excluding it here fixes that, and a short trimmed clip has no real
    # use for chapter markers about the original recording's timeline anyway.
    audio_map_args = shifted_audio_map_args or ["-map", "0:a"]
    stream_maps = ["-map", "0:v", *audio_map_args]
    # Moves the MP4 "moov" atom (the index of where every frame lives in the file) to the front
    # instead of ffmpeg's default of appending it at the end -- a plain stream copy doesn't do
    # this on its own. Browsers and embedded web players (e.g. Discord's inline preview) need it
    # up front to start streaming at all; VLC and most desktop players are lenient enough to play
    # either way, which is why this can look fine locally and still fail elsewhere. Harmless no-op
    # for non-MP4 containers (confirmed: ffmpeg just ignores it for a Matroska/.mkv output).
    faststart_args = ["-movflags", "+faststart"]
    if not precise and crf is None and scale_height is None and not filtering_audio:
        return [
            ffmpeg_path, "-y", "-ss", start_str, "-i", input_path, "-t", duration_str,
            *stream_maps, "-c", "copy", *faststart_args, output_path,
        ]
    effective_crf = CLIP_EDITOR_DEFAULT_CRF if crf is None else crf
    plain_reencode = precise or crf is not None or scale_height is not None
    encode_args = list(filter_complex_args) + stream_maps
    if scale_height is not None:
        encode_args += ["-vf", f"scale=-2:{scale_height}"]
    encode_args += ["-c:v", "libx264", "-crf", str(effective_crf)] if plain_reencode else ["-c:v", "copy"]
    encode_args += ["-c:a", "aac", *faststart_args]
    if precise:
        return [ffmpeg_path, "-y", "-i", input_path, "-ss", start_str, "-t", duration_str] + encode_args + [output_path]
    return [ffmpeg_path, "-y", "-ss", start_str, "-i", input_path, "-t", duration_str] + encode_args + [output_path]


def build_two_pass_size_targeted_commands(
    ffmpeg_path, input_path, start_seconds, end_seconds, output_path, target_size_mb, scale_height=None,
    passlog_prefix=None,
):
    """Builds a two-pass ffmpeg encode aimed at landing at or under target_size_mb. A single-pass
    average-bitrate encode (-b:v alone) reliably overshoots its nominal target in practice --
    confirmed live: a single-pass attempt at a 10 MB target came out at 10.89 MB, a 9% overshoot
    that would defeat the whole point against a hard upload limit. A first pass (video only,
    analysis discarded to NUL) lets x264 spend the bitrate budget accurately across the whole
    clip on the real, second pass -- the standard technique for actually hitting a size target
    rather than just approximating it.

    Always frame-accurate (-ss after -i) on both passes -- a size target is only meaningful
    against the exact real duration being encoded, and both passes must encode identical content
    for pass 2's stats file to apply correctly.

    Only the *first* audio track is included (0:a:0), not every track like build_trim_command's
    "-map 0:a" -- confirmed live this matters a lot: this app's own multi-track-audio recordings
    can carry 5-6 separate tracks, each independently encoded to ~128 kbps, which blew a 10 MB
    target out to nearly 15 MB when all of them were kept (the audio budget below only ever
    accounts for one). A size-constrained "share this clip" export has no real use for 6 parallel
    audio tracks anyway -- most players only ever play the first one by default.

    Returns (pass1_cmd, pass2_cmd); pass1_cmd must be run to completion before pass2_cmd.
    passlog_prefix is where ffmpeg writes/reads its pass stats (e.g. "<prefix>-0.log") -- pass a
    path in a writable temp location; the caller is responsible for cleaning up the log file(s)
    afterward, since ffmpeg never does."""
    start_str = format_timestamp(start_seconds)
    duration_str = format_timestamp(end_seconds - start_seconds)
    video_kbps = compute_target_video_bitrate_kbps(end_seconds - start_seconds, target_size_mb) or 1
    bufsize_kbps = video_kbps * 2
    video_args = ["-c:v", "libx264", "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{bufsize_kbps}k"]
    if scale_height is not None:
        video_args += ["-vf", f"scale=-2:{scale_height}"]
    pass1 = [
        ffmpeg_path, "-y", "-i", input_path, "-ss", start_str, "-t", duration_str,
        "-map", "0:v", *video_args, "-pass", "1", "-passlogfile", passlog_prefix,
        "-an", "-f", "null", "NUL",
    ]
    pass2 = [
        ffmpeg_path, "-y", "-i", input_path, "-ss", start_str, "-t", duration_str,
        "-map", "0:v", "-map", "0:a:0", *video_args, "-pass", "2", "-passlogfile", passlog_prefix,
        "-c:a", "aac", "-b:a", f"{CLIP_EDITOR_TARGET_AUDIO_BITRATE_KBPS}k", "-movflags", "+faststart", output_path,
    ]
    return pass1, pass2


def compute_trim_output_path(input_path, output_folder=None, suffix="_trimmed", output_ext=None):
    """Computes where a trimmed clip should be written: inside output_folder if given (created
    if it doesn't exist yet, mirroring apply_output_folder), otherwise next to the source file.
    Never overwrites an existing file -- appends " (2)", " (3)", etc. until a free name is found,
    the same convention Windows Explorer itself uses for a colliding copy.

    output_ext, if given (e.g. ".mkv"), overrides the source file's extension -- this is how the
    editor's output-format picker changes the trimmed clip's container; ffmpeg itself picks the
    muxer from the output path's extension, so nothing else needs to change to support it."""
    directory = output_folder or os.path.dirname(input_path)
    if output_folder:
        os.makedirs(output_folder, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(input_path))
    if output_ext:
        ext = output_ext

    candidate = os.path.join(directory, f"{base}{suffix}{ext}")
    if not os.path.exists(candidate):
        return candidate
    n = 2
    while True:
        candidate = os.path.join(directory, f"{base}{suffix} ({n}){ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def cleanup_two_pass_log_files(passlog_prefix):
    """Removes the pass-stats file(s) ffmpeg writes for a two-pass encode (<prefix>-0.log, and
    sometimes <prefix>-0.log.mbtree for x264's mb-tree ratecontrol) -- ffmpeg never cleans these
    up itself, and they're only ever useful for the one encode that just ran."""
    for suffix in ("-0.log", "-0.log.mbtree"):
        try:
            os.remove(passlog_prefix + suffix)
        except OSError:
            pass


def _run_ffmpeg_with_progress(cmd, total_duration_seconds, on_progress):
    """Runs cmd (ffmpeg_path must be cmd[0]) via subprocess.Popen with -progress piped back to
    this process instead of trim_clip's usual subprocess.run, which only ever reports anything
    once the WHOLE command has already finished -- calls on_progress(fraction), fraction in
    [0, 1], as real encoding progress comes in (parsed off ffmpeg's own out_time= field), so a
    caller can show something better than a bare "Trimming..." for however long a re-encode
    actually takes. Only used when a caller actually wants that (trim_clip's own progress_callback
    param); every other caller keeps using plain subprocess.run completely unchanged.

    Returns (returncode, stderr_text) -- the same two fields callers already read off a
    subprocess.run() CompletedProcess, so the caller's existing success/failure handling doesn't
    need to know which of the two actually ran."""
    progress_cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(
        progress_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", **platform_common.hide_console_subprocess_kwargs(),
    )
    stderr_chunks = []

    def drain_stderr():
        if proc.stderr is not None:
            for line in proc.stderr:
                stderr_chunks.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    if proc.stdout is not None:
        for line in proc.stdout:
            if total_duration_seconds <= 0:
                continue
            line = line.strip()
            if not line.startswith("out_time="):
                continue
            try:
                elapsed = parse_timestamp(line.split("=", 1)[1])
            except ValueError:
                continue
            try:
                on_progress(min(1.0, max(0.0, elapsed / total_duration_seconds)))
            except Exception:
                logging.exception("Trim progress callback failed.")

    proc.wait()
    stderr_thread.join(timeout=5)
    return proc.returncode, "".join(stderr_chunks)


def trim_clip(
    input_path, start_seconds, end_seconds, output_path, ffmpeg_path="ffmpeg", precise=False,
    delete_original=False, icon=None, notifications_config=None, crf=None, scale_height=None,
    target_size_mb=None, audio_shift_ms_by_track=None, audio_routing=None, muted_destinations=None,
    gains_db=None, progress_callback=None,
):
    """Runs the actual ffmpeg trim -- blocking, callers run this on a background thread the same
    way transcode_recording's callers do. Verifies the output file actually exists and has a
    nonzero size before reporting success or deleting the source; never deletes on a failed or
    suspicious-looking trim, same rule transcode_recording already follows.

    audio_shift_ms_by_track/audio_routing/muted_destinations/gains_db: see
    build_audio_routing_filter_args. Only probes the source's real audio track count (an extra
    ffprobe subprocess) when any of them is actually requested -- the common case (none) pays
    nothing extra. Not supported together with target_size_mb: a size-targeted export already
    keeps only the first audio track (see build_two_pass_size_targeted_commands), so there's
    nothing left to shift, route, mute, or boost/attenuate.

    progress_callback(phase_text, fraction), if given, is called repeatedly with real progress
    (fraction in [0, 1]) as the actual output-producing encode runs -- see
    _run_ffmpeg_with_progress. Left as None (the default), every ffmpeg invocation here still
    goes through plain subprocess.run exactly as before, unchanged."""
    basename = os.path.basename(input_path)
    if end_seconds <= start_seconds:
        logging.error("Could not trim %s: end time must be after the start time.", basename)
        return False

    passlog_prefix = None
    if target_size_mb:
        if audio_shift_ms_by_track or audio_routing or muted_destinations or gains_db:
            logging.warning(
                "Clip editor: ignoring the audio sync shift/routing/mute/gain for %s -- a "
                "size-targeted export only keeps the first audio track, so there's nothing left "
                "to apply it to.",
                basename,
            )
        passlog_prefix = os.path.join(tempfile.gettempdir(), f"obsautorec_2pass_{os.getpid()}_{int(time.time() * 1000)}")
        pass1_cmd, pass2_cmd = build_two_pass_size_targeted_commands(
            ffmpeg_path, input_path, start_seconds, end_seconds, output_path, target_size_mb, scale_height,
            passlog_prefix,
        )
        logging.info("Trimming %s (pass 1/2, targeting %s MB): %s", basename, target_size_mb, " ".join(pass1_cmd))
        if progress_callback:
            try:
                progress_callback("Analyzing (pass 1 of 2)", 0.0)
            except Exception:
                logging.exception("Trim progress callback failed.")
        try:
            result1 = subprocess.run(
                pass1_cmd, capture_output=True, text=True, **platform_common.hide_console_subprocess_kwargs()
            )
        except OSError as exc:
            logging.error("Could not run ffmpeg at '%s' to trim %s: %s", ffmpeg_path, basename, exc)
            notify(icon, notifications_config, "Trim failed", f"Could not trim {basename}: ffmpeg failed to run.")
            return False
        if result1.returncode != 0:
            logging.error(
                "ffmpeg trim (pass 1/2) failed for %s (exit code %s).\nCommand: %s\nstderr:\n%s",
                input_path, result1.returncode, " ".join(pass1_cmd), result1.stderr[-4000:],
            )
            notify(
                icon, notifications_config, "Trim failed",
                f"{basename} was kept, but the trim failed -- see the log for details.",
            )
            cleanup_two_pass_log_files(passlog_prefix)
            return False
        cmd = pass2_cmd
        phase_text = "Encoding (pass 2 of 2)"
        logging.info("Trimming %s (pass 2/2): %s", basename, " ".join(cmd))
    else:
        audio_stream_count = None
        if audio_shift_ms_by_track or audio_routing or muted_destinations or gains_db:
            audio_stream_count = probe_audio_stream_count(ffmpeg_path, input_path)
            if not audio_stream_count:
                logging.warning(
                    "Clip editor: could not determine %s's audio track count -- skipping the "
                    "requested audio sync shift/routing/mute/gain.", basename,
                )
        cmd = build_trim_command(
            ffmpeg_path, input_path, start_seconds, end_seconds, output_path, precise, crf, scale_height,
            audio_shift_ms_by_track, audio_stream_count, audio_routing, muted_destinations, gains_db,
        )
        phase_text = "Encoding clip"
        logging.info("Trimming %s: %s", basename, " ".join(cmd))

    try:
        if progress_callback:
            progress_callback(phase_text, 0.0)
            returncode, stderr_text = _run_ffmpeg_with_progress(
                cmd, end_seconds - start_seconds,
                lambda fraction: progress_callback(phase_text, fraction),
            )
        else:
            result = subprocess.run(
                cmd, capture_output=True, text=True, **platform_common.hide_console_subprocess_kwargs()
            )
            returncode, stderr_text = result.returncode, result.stderr
    except OSError as exc:
        logging.error("Could not run ffmpeg at '%s' to trim %s: %s", ffmpeg_path, basename, exc)
        notify(icon, notifications_config, "Trim failed", f"Could not trim {basename}: ffmpeg failed to run.")
        return False
    finally:
        if passlog_prefix:
            cleanup_two_pass_log_files(passlog_prefix)

    output_ok = os.path.isfile(output_path) and os.path.getsize(output_path) > 0
    if returncode != 0 or not output_ok:
        logging.error(
            "ffmpeg trim failed for %s (exit code %s).\nCommand: %s\nstderr:\n%s",
            input_path, returncode, " ".join(cmd), stderr_text[-4000:],
        )
        notify(
            icon, notifications_config, "Trim failed",
            f"{basename} was kept, but the trim failed -- see the log for details.",
        )
        if os.path.isfile(output_path) and not output_ok:
            try:
                os.remove(output_path)
            except OSError:
                pass
        return False

    logging.info("Trimmed clip saved to %s", output_path)
    notify(icon, notifications_config, "Clip trimmed", os.path.basename(output_path))

    if delete_original:
        try:
            os.remove(input_path)
            logging.info("Deleted original recording after trim: %s", basename)
        except OSError as exc:
            logging.warning("Could not delete original after trim: %s", exc)

    return True


def rename_with_game_prefix(
    output_path, game_display_name, split_part=None, silent=False, use_subfolder=False, is_replay=False,
):
    """Renames (and optionally relocates) a finished recording. Returns the resulting path,
    or the original path if renaming was skipped/failed."""
    if not output_path or not game_display_name:
        return output_path

    output_path = os.path.normpath(output_path)
    directory = os.path.dirname(output_path)
    filename = os.path.basename(output_path)
    prefix = sanitize_filename_part(game_display_name)
    if not prefix:
        return output_path

    name_parts = []
    if silent:
        name_parts.append("[NO AUDIO]")
    if not use_subfolder:
        name_parts.append(prefix)
    if split_part:
        name_parts.append(f"Split {split_part}")
    elif is_replay:
        name_parts.append("Replay")
    new_name = f"{' - '.join(name_parts)} - {filename}" if name_parts else filename

    target_dir = directory
    if use_subfolder:
        target_dir = os.path.join(directory, prefix)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            logging.error("Failed to create game subfolder %s: %s", target_dir, exc)
            target_dir = directory

    new_path = os.path.join(target_dir, new_name)
    retries, delay = 5, 1
    for attempt in range(1, retries + 1):
        try:
            os.rename(output_path, new_path)
            logging.info("Renamed recording to %s", os.path.relpath(new_path, directory))
            return new_path
        except OSError as exc:
            if attempt < retries:
                time.sleep(delay)
                continue
            logging.error("Failed to rename recording file: %s", exc)
            return output_path


def stop_recording(client, icon, game_display_name=None, recording_state=None, config=None):
    """Stops recording and finalizes the file. Returns True if a recording was kept,
    False if it failed to stop or was deleted (short-clip cleanup)."""
    try:
        resp = client.stop_record()
        logging.info("Recording stopped.")
        clear_active_session_marker()
    except Exception as exc:
        logging.error("Failed to stop recording: %s", exc)
        return False

    config = config or {}
    notifications_config = config.get("notifications", {})

    # OBS's StopRecord response reports the pre-split filename after a split has
    # occurred, so prefer the path we've tracked live from split/start events.
    tracked_path = recording_state.get("current_path") if recording_state else None
    output_path = tracked_path or getattr(resp, "output_path", None)
    split_part = recording_state.get("part_index") if recording_state and recording_state.get("did_split") else None

    short_clip_config = config.get("cleanup", {}).get("delete_short_clips", {})
    session_start = recording_state.get("session_start_time") if recording_state else None
    if short_clip_config.get("enabled") and session_start is not None:
        duration = time.time() - session_start
        minimum_seconds = short_clip_config.get("minimum_seconds", 20)
        if duration < minimum_seconds:
            logging.info(
                "Recording session for %s lasted %.1fs (< %ds minimum); deleting instead of keeping.",
                game_display_name or "game", duration, minimum_seconds,
            )
            segment_files = list(recording_state.get("segment_files", [])) if recording_state else []
            delete_recording_files(segment_files + [output_path])
            notify(
                icon, notifications_config, "Short clip deleted",
                f"Recording of {game_display_name or 'game'} was under {minimum_seconds}s; deleted.",
            )
            return False

    silent_config = config.get("cleanup", {}).get("flag_silent_recordings", {})
    silent = bool(recording_state) and is_segment_silent(recording_state, silent_config)
    subfolders_enabled = config.get("organize_into_game_subfolders", False)

    new_path = rename_with_game_prefix(output_path, game_display_name, split_part, silent, subfolders_enabled)
    if recording_state is not None and new_path:
        recording_state.setdefault("segment_files", []).append(new_path)

    maybe_transcode(new_path, config.get("post_record_transcode", {}), icon, notifications_config)
    maybe_apply_audio_sync_shift(new_path, config.get("audio_sync_shift", {}), icon, notifications_config)
    return True


def set_status(icon, status, text):
    status["text"] = text
    icon.title = f"OBS Auto Recorder - {text}"
    icon.icon = build_tray_image(current_color(status))


ACTIVE_SESSION_MARKER_PATH = os.path.join(SCRIPT_DIR, ".active_recording_session.json")


def write_active_session_marker(display_name):
    # Lets the *next* startup tell "OBS is recording because we crashed/were force-killed
    # mid-session" apart from "OBS is recording because the user started it manually" --
    # only the former should ever be auto-stopped and finalized. Cleared the moment
    # stop_recording() actually stops the output, so it's only ever present while a
    # recording this app itself started is genuinely still in flight.
    try:
        with open(ACTIVE_SESSION_MARKER_PATH, "w", encoding="utf-8") as f:
            json.dump({"display_name": display_name}, f)
    except Exception:
        logging.exception("Could not write active-recording marker.")


def clear_active_session_marker():
    try:
        os.remove(ACTIVE_SESSION_MARKER_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        logging.exception("Could not clear active-recording marker.")


def read_active_session_marker():
    try:
        with open(ACTIVE_SESSION_MARKER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def reset_recording_state(recording_state):
    recording_state["display_name"] = None
    recording_state["current_path"] = None
    recording_state["part_index"] = 1
    recording_state["did_split"] = False
    recording_state["segment_start_time"] = None
    recording_state["session_start_time"] = None
    recording_state["segment_files"] = []
    recording_state["heard_any_audio"] = False
    recording_state["last_audio_peak_time"] = None
    recording_state["silent_warning_sent"] = False


def watcher_loop(icon, status, audio_state, recording_state, runtime_state, stop_event):
    try:
        _watcher_loop_impl(icon, status, audio_state, recording_state, runtime_state, stop_event)
    except Exception:
        logging.exception("Watcher thread crashed unexpectedly.")
        status["recording"] = False
        try:
            status["text"] = "Error - see log"
            icon.title = "OBS Auto Recorder - Error, see log"
            icon.icon = build_tray_image(ERROR_COLOR)
        except Exception:
            pass
    finally:
        icon.stop()


def _watcher_loop_impl(icon, status, audio_state, recording_state, runtime_state, stop_event):
    config = load_config()
    watched_games = {g.lower() for g in config["watched_games"]}
    watched_windows = config.get("watched_windows", [])
    poll_interval = config.get("poll_interval_seconds", 1.5)
    obs_config = config["obs"]
    steam_config = config.get("steam", {})
    xbox_config = config.get("xbox", {})
    battlenet_config = config.get("battlenet", {})
    steam_common_dirs = get_steam_common_dirs(steam_config)
    xbox_common_dirs = get_xbox_install_dirs(xbox_config)
    battlenet_common_dirs = get_battlenet_install_dirs(battlenet_config)
    root_common_dirs = steam_common_dirs + xbox_common_dirs + battlenet_common_dirs
    root_exclude_keywords = sorted(set(
        [k.lower() for k in steam_config.get("exclude_keywords", [])]
        + [k.lower() for k in xbox_config.get("exclude_keywords", [])]
        + [k.lower() for k in battlenet_config.get("exclude_keywords", [])]
    ))
    epic_games = get_epic_installed_games(config.get("epic", {}))
    gog_games = get_gog_installed_games(config.get("gog", {}))
    manifest_games = epic_games + gog_games
    game_audio_config = obs_config.get("game_audio_capture", {})
    replay_buffer_config = obs_config.get("replay_buffer", {})
    disk_guard_config = config.get("disk_space_guard", {})
    storage_config = config.get("storage_management", {})
    notifications_config = config.get("notifications", {})

    logging.info("Watching for processes: %s", ", ".join(sorted(watched_games)))
    if watched_windows:
        logging.info(
            "Watching for window titles: %s",
            ", ".join(f"{w['process_name']} containing '{w['title_contains']}'" for w in watched_windows),
        )
    set_status(icon, status, "Watching")

    obs_client = None
    obs_event_client = None
    active_name = None
    active_pid = None
    active_display_name = None
    active_replay_buffer_only = False
    obs_recovery_state = {"last_attempt": 0, "last_start_failure": 0}
    obs_running_last_known = None
    audio_overlay_launch_state = {"last_attempt": 0}

    # A recording this app started can be left running with nothing tracking it if the
    # previous instance was force-killed, crashed, or otherwise never reached the normal
    # stop-and-rename path below (a plain OBS restart can't fix this -- OBS just keeps
    # recording to the same file across it; only actually stopping the output does). The
    # marker file is only ever present while a recording *this app* started is genuinely
    # still active, so it's safe to auto-finalize on sight -- a manual recording the user
    # started directly in OBS never gets one and is never touched here.
    stale_marker = read_active_session_marker()
    if stale_marker:
        try:
            recovery_client = obsws.ReqClient(
                host=obs_config["websocket"]["host"], port=obs_config["websocket"]["port"],
                password=obs_config["websocket"]["password"], timeout=3,
            )
            if recovery_client.get_record_status().output_active:
                logging.warning(
                    "Found a recording for %s still active from an unclean shutdown; "
                    "stopping and finalizing it now.", stale_marker.get("display_name") or "a previous session",
                )
                stop_recording(recovery_client, icon, stale_marker.get("display_name"), recording_state, config)
            else:
                clear_active_session_marker()
            recovery_client.disconnect()
        except Exception:
            logging.info("Could not check for an orphaned recording from a previous session (OBS not reachable).")

    while not stop_event.is_set():
        processes = get_running_processes()

        # pystray only rebuilds the native tray menu right after a menu item is clicked, not
        # every time it's opened (see pystray.Icon.update_menu's docstring) -- so the tray's
        # dynamic "Kill OBS"/"Start OBS" label would otherwise show whatever OBS's state was
        # the last time *any* menu item was clicked, not its actual current state. Explicitly
        # refresh whenever OBS's running state actually changes, regardless of what changed it
        # (a watched game auto-launching it, the memory-bloat recovery restart, the user closing
        # it by hand, or the tray button itself).
        obs_running_now = is_obs_running(obs_config["process_name"], processes)
        if obs_running_now != obs_running_last_known:
            obs_running_last_known = obs_running_now
            icon.update_menu()

        enforce_storage_budget(storage_config, obs_config, recording_state, icon, notifications_config)

        if active_name is None:
            # The recording-driven obs_event_client (established below once a watched game is
            # detected) also feeds the audio-mixer overlay, but while idle nothing has
            # established a connection yet -- without this, the overlay would show "No active
            # audio sources" any time OBS is merely running with nothing being recorded, even
            # though OBS itself is live and metering fine. Only touches things while idle: once
            # a game is detected below, that path owns obs_event_client's lifecycle exclusively.
            if audio_state.get("enabled"):
                if obs_event_client and not is_event_client_connected(obs_event_client):
                    # Connection died (OBS closed/killed, including via the tray's own "Kill
                    # OBS") -- without clearing levels here, the overlay just keeps showing
                    # whatever it last received forever, looking exactly like a freeze instead
                    # of reverting to "No active audio sources".
                    obs_event_client = None
                    audio_state["levels"] = {}
                if not obs_event_client:
                    if obs_running_now:
                        obs_event_client = connect_obs_events(config, icon, status, audio_state, recording_state)
                    else:
                        audio_state["levels"] = {}
                        # Show Audio Mixer Levels used to just sit there showing "No active audio
                        # sources" forever if OBS wasn't already running -- launch it the same way
                        # a watched game would, so enabling levels alone is enough to get them
                        # showing. Gated by startup_wait_seconds so this fires once per launch
                        # attempt instead of spamming a new OBS process every poll while it starts.
                        now = time.time()
                        launch_cooldown = obs_config.get("startup_wait_seconds", 8)
                        if now - audio_overlay_launch_state["last_attempt"] > launch_cooldown:
                            audio_overlay_launch_state["last_attempt"] = now
                            logging.info("Show Audio Mixer Levels enabled but OBS isn't running; launching OBS.")
                            threading.Thread(target=launch_obs, args=(obs_config,), daemon=True).start()
            elif obs_event_client:
                try:
                    obs_event_client.disconnect()
                except Exception:
                    pass
                obs_event_client = None
                audio_state["levels"] = {}

            memory_bytes = get_process_memory_bytes(obs_config["process_name"], processes)
            if memory_bytes > get_obs_memory_limit_bytes(obs_config):
                now = time.time()
                if now - obs_recovery_state["last_attempt"] >= get_obs_recovery_cooldown_seconds(obs_config):
                    obs_recovery_state["last_attempt"] = now
                    logging.warning(
                        "OBS is using %.1f GB of memory while idle; restarting it.",
                        memory_bytes / (1024 ** 3),
                    )
                    status["text"] = "Error - restarting bloated OBS"
                    icon.title = "OBS Auto Recorder - Error, restarting OBS"
                    icon.icon = build_tray_image(ERROR_COLOR)
                    notify(
                        icon, notifications_config, "OBS restarted",
                        f"OBS was using {memory_bytes / (1024 ** 3):.1f} GB while idle and has been restarted.",
                    )
                    kill_process_by_name(obs_config["process_name"])
                    launch_obs(obs_config)
                    processes = get_running_processes()
                    set_status(icon, status, "Watching")

            name, exe, pid, display_override = find_target_process(
                watched_games, root_common_dirs, root_exclude_keywords, manifest_games, watched_windows, processes
            )
            if name:
                display_name = display_override or get_game_display_name(name, exe, root_common_dirs)
                if not has_sufficient_disk_space(disk_guard_config, os.path.dirname(obs_config["path"])):
                    minimum_gb = disk_guard_config.get("minimum_free_gb", DEFAULT_DISK_SPACE_MINIMUM_GB)
                    logging.error(
                        "Insufficient disk space (< %s GB free); not starting recording for %s.",
                        minimum_gb, exe or name,
                    )
                    status["text"] = "Error - low disk space"
                    icon.title = "OBS Auto Recorder - Error, low disk space"
                    icon.icon = build_tray_image(ERROR_COLOR)
                    notify(
                        icon, notifications_config, "Low disk space",
                        f"Not enough free disk space to start recording {display_name}.",
                    )
                    stop_event.wait(poll_interval)
                    continue

                now = time.time()
                if now - obs_recovery_state["last_start_failure"] < get_obs_recovery_cooldown_seconds(obs_config):
                    stop_event.wait(poll_interval)
                    continue

                logging.info("Detected watched game: %s", exe or name)
                # A previous attempt this session may have connected but failed to start
                # recording (e.g. OBS was slow to respond) -- disconnect it before reconnecting
                # instead of leaking it. obsws_python's EventClient in particular keeps a
                # background thread blocked on recv() alive for as long as it's connected, so
                # retrying every poll without this would pile up leaked connections/threads on
                # every failed attempt for as long as the game stays open, eventually bogging
                # OBS down for real.
                if obs_client:
                    try:
                        obs_client.disconnect()
                    except Exception:
                        pass
                if obs_event_client:
                    try:
                        obs_event_client.disconnect()
                    except Exception:
                        pass
                obs_client, obs_event_client = ensure_obs_ready(
                    config, processes, icon, status, audio_state, recording_state, obs_recovery_state
                )
                runtime_state["obs_client"] = obs_client
                replay_buffer_mode = get_replay_buffer_mode(replay_buffer_config)
                replay_buffer_only = replay_buffer_mode == "only"
                session_started = obs_client and (
                    start_replay_buffer(obs_client) if replay_buffer_only else start_recording(obs_client)
                )
                if session_started:
                    active_name, active_pid = name, pid
                    active_display_name = display_name
                    active_replay_buffer_only = replay_buffer_only
                    recording_state["display_name"] = active_display_name
                    # "recording" here specifically means a continuous file is being written --
                    # replay-buffer-only mode never does that, so it's left False (avoids e.g. the
                    # silent-recording flag misfiring against a buffer that isn't a monitored file).
                    status["recording"] = not replay_buffer_only
                    if replay_buffer_only:
                        set_status(icon, status, f"Replay buffer active: {active_display_name}")
                        notify(icon, notifications_config, "Replay buffer active", active_display_name)
                    else:
                        set_status(icon, status, f"Recording {active_display_name}")
                        notify(icon, notifications_config, "Recording started", active_display_name)
                        write_active_session_marker(active_display_name)
                        if replay_buffer_mode == "with_recording":
                            start_replay_buffer(obs_client)
                    if game_audio_config.get("enabled"):
                        set_game_audio_capture_target(obs_client, game_audio_config["input_name"], name)
                        apply_process_capture_sync_offset(
                            obs_client, game_audio_config["input_name"],
                            obs_config.get("process_audio_capture_sync_offset_ms", DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS),
                        )
                else:
                    obs_recovery_state["last_start_failure"] = now
                    logging.error("Could not get OBS ready to record; will keep retrying while %s runs.", exe or name)
                    status["text"] = "Error - OBS unreachable, see log"
                    icon.title = "OBS Auto Recorder - Error, OBS unreachable"
                    icon.icon = build_tray_image(ERROR_COLOR)
                    notify(icon, notifications_config, "OBS error", "Could not get OBS ready to record.")
            elif status["text"].startswith("Error"):
                set_status(icon, status, "Watching")
        else:
            if not is_process_running(active_pid):
                logging.info("%s has exited.", active_display_name or active_name)
                if obs_client:
                    if active_replay_buffer_only:
                        stop_replay_buffer(obs_client)
                    else:
                        if get_replay_buffer_mode(replay_buffer_config) == "with_recording":
                            stop_replay_buffer(obs_client)
                        kept = stop_recording(obs_client, icon, active_display_name, recording_state, config)
                        if kept:
                            notify(icon, notifications_config, "Recording stopped", active_display_name or active_name)
                active_name, active_pid, active_display_name = None, None, None
                active_replay_buffer_only = False
                reset_recording_state(recording_state)
                status["recording"] = False
                set_status(icon, status, "Watching")

        stop_event.wait(poll_interval)

    if active_name is not None and obs_client:
        logging.info("Quit requested while recording; stopping recording.")
        if active_replay_buffer_only:
            stop_replay_buffer(obs_client)
        else:
            if get_replay_buffer_mode(replay_buffer_config) == "with_recording":
                stop_replay_buffer(obs_client)
            stop_recording(obs_client, icon, active_display_name, recording_state, config)


OVERLAY_SIZE = 26
OVERLAY_MARGIN = 16

METER_WIDTH = 180
METER_ROW_HEIGHT = 18
METER_GAP = 8
METER_LABEL_CHARS = 18
METER_LOW_COLOR = "#2ecc40"
METER_MID_COLOR = "#ffd700"
METER_HIGH_COLOR = "#ff4136"
METER_BG_COLOR = "#1a1a1a"
METER_TRACK_COLOR = "#333333"


def meter_bar_color(peak):
    if peak >= 0.9:
        return METER_HIGH_COLOR
    if peak >= 0.6:
        return METER_MID_COLOR
    return METER_LOW_COLOR


def run_overlay(monitors, overlay_state, audio_state, status, stop_event, mic_boost_config=None):
    # Without this wrapper, an exception anywhere in here (Tk init, widget construction, the
    # poll loop) kills the thread completely silently in a --noconsole build -- no stderr to
    # print a traceback to -- permanently disabling both the overlay AND Settings (which now
    # depends on this same interpreter) with zero trace in the log. Matches watcher_loop's
    # own try/except-wrapping-impl pattern below.
    try:
        _run_overlay_impl(monitors, overlay_state, audio_state, status, stop_event, mic_boost_config or {})
    except Exception:
        logging.exception("Overlay thread crashed unexpectedly; overlay and Settings are unavailable this session.")


def _run_overlay_impl(monitors, overlay_state, audio_state, status, stop_event, mic_boost_config):
    # A Tk() failure right after a relaunch (before the fix to stop inheriting a stale
    # TCL_LIBRARY/TK_LIBRARY from the parent process) is the one failure mode transient enough
    # to be worth retrying rather than just logging once and giving up.
    root = None
    last_exc = None
    for attempt in range(1, 4):
        try:
            root = tk.Tk()
            break
        except Exception as exc:
            last_exc = exc
            logging.warning("Overlay Tk initialization attempt %d/3 failed: %s", attempt, exc)
            time.sleep(1)
    if root is None:
        logging.error(
            "Overlay could not initialize Tk after 3 attempts (%s); the overlay and Settings "
            "editor are unavailable this session. Restarting the app may resolve it.", last_exc,
        )
        return
    root.withdraw()
    # "clam" (rather than the platform default "vista") is the ttk theme for the whole app: vista
    # is drawn by the Windows theme engine itself, which ignores ttk style overrides for a
    # readonly Combobox's text color -- confirmed by testing the clip editor's dark theme, where
    # dropdown text stayed low-contrast under vista no matter how the style was configured. clam
    # is fully Tk-drawn, so custom styles (like the clip editor's dark Combobox/Scale/Progressbar)
    # actually take effect; it also changes the Settings editor's own dropdowns to this same
    # flatter look.
    ttk.Style(root).theme_use("clam")
    # Exposed so the Settings editor can be built as a Toplevel of this same interpreter
    # instead of spinning up a second, independent tk.Tk() on another thread -- PyInstaller's
    # bundled Tcl/Tk isn't reliably safe for that (observed as everything from a silent hang to
    # a hard process crash), whereas a second top-level window on the one interpreter that's
    # already running here is the normal, well-supported way to do this in Tkinter.
    overlay_state["root"] = root

    dot_window = tk.Toplevel(root)
    dot_window.overrideredirect(True)
    dot_window.attributes("-topmost", True)
    try:
        dot_window.attributes("-toolwindow", True)
    except tk.TclError:
        pass
    dot_window.withdraw()

    meter_window = tk.Toplevel(root)
    meter_window.overrideredirect(True)
    meter_window.attributes("-topmost", True)
    try:
        meter_window.attributes("-toolwindow", True)
    except tk.TclError:
        pass
    meter_window.withdraw()
    meter_canvas = tk.Canvas(meter_window, bg=METER_BG_COLOR, highlightthickness=0)
    meter_canvas.pack(fill="both", expand=True)

    dot_shown_index = [None]
    meter_shown_index = [None]
    meter_row_count = [0]

    def poll_dot():
        index = overlay_state["monitor_index"]
        if index != dot_shown_index[0]:
            dot_shown_index[0] = index
            if index is None or index >= len(monitors):
                dot_window.withdraw()
            else:
                mon = monitors[index]
                x = mon["right"] - OVERLAY_SIZE - OVERLAY_MARGIN
                y = mon["top"] + OVERLAY_MARGIN
                dot_window.geometry(f"{OVERLAY_SIZE}x{OVERLAY_SIZE}+{x}+{y}")
                dot_window.deiconify()

        if dot_shown_index[0] is not None:
            dot_window.configure(bg=color_to_hex(current_color(status)))

    def poll_meters():
        # No longer gated on status["recording"] -- OBS streams live input levels continuously
        # once subscribed, independent of whether a file is actually being recorded, so there's
        # no reason to hide the overlay just because recording hasn't started (or has stopped)
        # while OBS is still connected. Naturally shows nothing if there's no active OBS
        # connection yet (audio_state["levels"] stays empty until one exists).
        show = audio_state["enabled"]
        index = overlay_state["monitor_index"] if show else None
        levels = audio_state["levels"]

        # A preview row showing what this reading would become after the mic-boost chain, right
        # under the mic's own real (raw) row -- only while mic_boost isn't actually enabled yet.
        # Once it genuinely IS enabled, OBS's own reading for this input already reports the real
        # post-filter level directly (confirmed live -- see predict_mic_boost_output_mul's own
        # docstring), so a second computed row at that point would just be a redundant, slightly-
        # off approximation of a number already being shown correctly.
        preview_input_name = None if mic_boost_config.get("enabled") else mic_boost_config.get("input_name")
        rows = []
        for name, peak in levels.items():
            rows.append((name, peak))
            if name == preview_input_name:
                noise_gate_config = mic_boost_config.get("noise_gate", {})
                predicted = predict_mic_boost_output_mul(
                    peak, mic_boost_config.get("boost_db", 0.0),
                    noise_gate_enabled=noise_gate_config.get("enabled", False),
                    noise_gate_threshold_db=noise_gate_config.get("threshold_db"),
                )
                rows.append((f"{name} (after boost)", predicted))
        row_count = max(len(rows), 1)

        if index != meter_shown_index[0] or row_count != meter_row_count[0]:
            meter_shown_index[0] = index
            meter_row_count[0] = row_count
            if index is None or index >= len(monitors):
                meter_window.withdraw()
            else:
                mon = monitors[index]
                height = row_count * METER_ROW_HEIGHT
                x = mon["right"] - METER_WIDTH - OVERLAY_MARGIN
                y = mon["top"] + OVERLAY_MARGIN + OVERLAY_SIZE + METER_GAP
                meter_window.geometry(f"{METER_WIDTH}x{height}+{x}+{y}")
                meter_canvas.configure(width=METER_WIDTH, height=height)
                meter_window.deiconify()

        if meter_shown_index[0] is not None:
            meter_canvas.delete("all")
            if not rows:
                meter_canvas.create_text(
                    8, METER_ROW_HEIGHT // 2, anchor="w", fill="#888888", text="No active audio sources"
                )
            else:
                for i, (name, peak) in enumerate(rows):
                    top = i * METER_ROW_HEIGHT
                    label = name if len(name) <= METER_LABEL_CHARS else name[: METER_LABEL_CHARS - 1] + "…"
                    bar_x = 90
                    bar_w = METER_WIDTH - bar_x - 8
                    fill_w = max(0, min(1.0, peak)) * bar_w
                    meter_canvas.create_text(
                        6, top + METER_ROW_HEIGHT // 2, anchor="w", fill="#dddddd", text=label
                    )
                    meter_canvas.create_rectangle(
                        bar_x, top + 3, bar_x + bar_w, top + METER_ROW_HEIGHT - 3,
                        fill=METER_TRACK_COLOR, outline="",
                    )
                    if fill_w > 0:
                        meter_canvas.create_rectangle(
                            bar_x, top + 3, bar_x + fill_w, top + METER_ROW_HEIGHT - 3,
                            fill=meter_bar_color(peak), outline="",
                        )

    def poll():
        if stop_event.is_set():
            root.destroy()
            return
        try:
            poll_dot()
            poll_meters()
        except Exception:
            # Tk's .after() callbacks aren't covered by _run_overlay_impl's own try/except --
            # an uncaught exception here would otherwise just stop this reschedule forever,
            # silently freezing the overlay in whatever state it was last in (no traceback at
            # all in a --noconsole build). Logging and rescheduling anyway means one bad tick
            # (e.g. a monitor rectangle mid-update) never permanently kills the overlay.
            logging.exception("Overlay poll tick failed; will keep retrying.")
        root.after(120, poll)

    root.after(120, poll)
    root.mainloop()


def parse_csv_field(text):
    return [item.strip() for item in text.split(",") if item.strip()]


def format_csv_field(items):
    return ", ".join(items or [])


# Shared dark palette for the Settings editor and the clip editor, so both windows read as the
# same app instead of one being an unrelated light-themed island next to the other's dark one.
DARK_BG = "#2b2b2b"
DARK_FG = "#e6e6e6"
DARK_ENTRY_BG = "#3c3c3c"
DARK_MUTED_FG = "#888888"


def make_scrollable_tab(notebook, title):
    outer = tk.Frame(notebook, bg=DARK_BG)
    notebook.add(outer, text=title)
    canvas = tk.Canvas(outer, highlightthickness=0, bg=DARK_BG)
    scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=DARK_BG)

    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    def on_enter(_event):
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

    def on_leave(_event):
        canvas.unbind_all("<MouseWheel>")

    canvas.bind("<Enter>", on_enter)
    canvas.bind("<Leave>", on_leave)
    return inner


def add_labeled_entry(parent, row, label_text, var, width=36):
    tk.Label(parent, text=label_text, anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    entry = tk.Entry(parent, textvariable=var, width=width, bg=DARK_ENTRY_BG, fg=DARK_FG, insertbackground=DARK_FG)
    entry.grid(row=row, column=1, sticky="we", padx=(0, 10), pady=4)
    return entry


def add_checkbox(parent, row, label_text, var, columnspan=2):
    tk.Checkbutton(
        parent, text=label_text, variable=var, bg=DARK_BG, fg=DARK_FG,
        activebackground=DARK_BG, activeforeground=DARK_FG, selectcolor=DARK_ENTRY_BG,
    ).grid(row=row, column=0, columnspan=columnspan, sticky="w", padx=10, pady=4)


def add_browse_button(parent, row, var, mode="file", filetypes=None):
    filetypes = platform_common.executable_filetypes() if filetypes is None else filetypes

    def browse():
        path = filedialog.askopenfilename(filetypes=filetypes) if mode == "file" else filedialog.askdirectory()
        if path:
            var.set(os.path.normpath(path))

    tk.Button(
        parent, text="Browse...", command=browse, bg=DARK_ENTRY_BG, fg=DARK_FG,
        activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
    ).grid(row=row, column=2, padx=(0, 10), pady=4)


def add_section_label(parent, row, text):
    tk.Label(parent, text=text, font=("Segoe UI", 9, "bold"), bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(14, 2)
    )


def open_process_picker(parent, on_add, multiselect=True, already_selected=None):
    """Small dialog listing current running process names, filterable, with entries
    already present in the caller's list (already_selected, a callable returning a
    lowercase set) visually marked so the picker stays in sync with what's configured."""
    already_selected = already_selected or (lambda: set())
    processes = sorted({name for name, _, _ in get_running_processes()}, key=str.lower)

    picker = tk.Toplevel(parent)
    picker.title("Pick Running Process")
    picker.geometry("340x420")
    picker.transient(parent.winfo_toplevel())
    picker.grab_set()

    tk.Label(picker, text="Filter", anchor="w").pack(fill="x", padx=10, pady=(10, 0))
    filter_var = tk.StringVar()
    filter_entry = tk.Entry(picker, textvariable=filter_var)
    filter_entry.pack(fill="x", padx=10, pady=(0, 6))
    filter_entry.focus_set()

    list_frame = tk.Frame(picker)
    list_frame.pack(fill="both", expand=True, padx=10)
    scrollbar = tk.Scrollbar(list_frame, orient="vertical")
    listbox = tk.Listbox(
        list_frame, selectmode="extended" if multiselect else "browse",
        yscrollcommand=scrollbar.set, exportselection=False,
    )
    scrollbar.config(command=listbox.yview)
    listbox.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    shown = []

    def refresh_list(*_args):
        needle = filter_var.get().lower()
        watching = already_selected()
        listbox.delete(0, "end")
        shown.clear()
        for name in processes:
            if needle not in name.lower():
                continue
            already = name.lower() in watching
            listbox.insert("end", f"{name}  (already watching)" if already else name)
            if already:
                listbox.itemconfig("end", fg="#888888")
            shown.append(name)

    filter_var.trace_add("write", refresh_list)
    refresh_list()

    def add_selected():
        selected = [shown[i] for i in listbox.curselection()]
        if selected:
            on_add(selected)
        picker.destroy()

    button_bar = tk.Frame(picker)
    button_bar.pack(fill="x", padx=10, pady=10)
    tk.Button(button_bar, text="Cancel", command=picker.destroy).pack(side="right")
    tk.Button(button_bar, text="Add Selected" if multiselect else "Select", command=add_selected).pack(
        side="right", padx=8
    )
    listbox.bind("<Double-Button-1>", lambda e: add_selected())
    filter_entry.bind("<Return>", lambda e: add_selected())


# Popular games worth a one-click add -- picked for exe names that are stable and well-known,
# prioritizing ones this app's launcher auto-detection (Steam/Epic/GOG/Xbox) can't find on its
# own (Riot's client, standalone launchers) plus a few other very common titles. Not meant to be
# exhaustive: "Pick Running..." (while the game is open) or typing the exe name by hand always
# still works for anything not listed here. Minecraft: Java Edition needs a window-title rule
# rather than a plain exe entry since javaw.exe is shared by any Java app.

# Only games with at least one realistic launch path this app *can't* already auto-discover
# belong here -- Steam/Epic/GOG/Xbox/Battle.net installs are all found automatically via the
# Launchers tab's own directory scanning, so a Steam-only title (e.g. Counter-Strike 2) would
# just be clutter. A title with its own dedicated launcher (or a distribution platform this app
# has no scanner for, like Riot Client or the EA app) stays, even if it's *also* on one of the
# scanned platforms -- e.g. Warframe (Steam or its own launcher) and Apex Legends (Steam or EA).
#
# Split per OS rather than one shared list: most of these titles have no real macOS/Linux client
# at all (kernel-level anti-cheat, Windows-only engines, etc.) and would just be dead entries that
# silently never match on those platforms. Only titles with a genuine native client elsewhere get
# a platform-specific entry, with that platform's own process name.
_COMMON_GAMES_WINDOWS = [
    {"name": "League of Legends", "process_name": "league of legends.exe"},
    {"name": "Wizard101", "process_name": "WizardGraphicalClient.exe"},
    {"name": "Valorant", "process_name": "VALORANT-Win64-Shipping.exe"},
    {"name": "Warframe", "process_name": "Warframe.x64.exe"},
    {"name": "Minecraft: Java Edition", "process_name": "javaw.exe", "title_contains": "minecraft"},
    {"name": "Apex Legends", "process_name": "r5apex.exe"},
    {"name": "Roblox", "process_name": "RobloxPlayerBeta.exe"},
    {"name": "Genshin Impact", "process_name": "GenshinImpact.exe"},
]

# League of Legends and Roblox both ship real macOS clients; Valorant/Warframe/Apex/Genshin/
# Wizard101 do not (no native Mac build, and several rely on kernel-level anti-cheat that
# explicitly excludes macOS). Process names here are best-effort based on each game's typical
# macOS bundle naming and haven't been confirmed against real hardware yet -- see
# CROSS_PLATFORM_PLAN.md's open macOS unknowns. If one doesn't match, "Pick Running..." (while
# the game is open) or typing the exact process name by hand still works.
_COMMON_GAMES_MACOS = [
    {"name": "League of Legends", "process_name": "LeagueofLegends"},
    {"name": "Minecraft: Java Edition", "process_name": "java", "title_contains": "minecraft"},
    {"name": "Roblox", "process_name": "RobloxPlayer"},
]

# Minecraft: Java Edition is the only one of these titles with a genuine native Linux client
# (it's just a cross-platform Java app) -- none of the rest ship for Linux at all.
_COMMON_GAMES_LINUX = [
    {"name": "Minecraft: Java Edition", "process_name": "java", "title_contains": "minecraft"},
]

if sys.platform == "win32":
    COMMON_GAMES = _COMMON_GAMES_WINDOWS
elif sys.platform == "darwin":
    COMMON_GAMES = _COMMON_GAMES_MACOS
else:
    COMMON_GAMES = _COMMON_GAMES_LINUX


def open_common_games_picker(parent, insert_unique, current_watched_lower, add_window_row, current_window_keys):
    """Lets the user one-click-add from COMMON_GAMES, routing plain-exe entries into the watched
    games list and window-title entries (e.g. Minecraft) into the window-rule editor -- same
    dialog either way, the caller-provided callbacks handle where each kind actually lands."""

    def is_already_added(game):
        if "title_contains" in game:
            return (game["process_name"].lower(), game["title_contains"].lower()) in current_window_keys()
        return game["process_name"].lower() in current_watched_lower()

    picker = tk.Toplevel(parent)
    picker.title("Add Common Game")
    picker.geometry("340x420")
    picker.transient(parent.winfo_toplevel())
    picker.grab_set()

    tk.Label(picker, text="Filter", anchor="w").pack(fill="x", padx=10, pady=(10, 0))
    filter_var = tk.StringVar()
    filter_entry = tk.Entry(picker, textvariable=filter_var)
    filter_entry.pack(fill="x", padx=10, pady=(0, 6))
    filter_entry.focus_set()

    list_frame = tk.Frame(picker)
    list_frame.pack(fill="both", expand=True, padx=10)
    scrollbar = tk.Scrollbar(list_frame, orient="vertical")
    listbox = tk.Listbox(
        list_frame, selectmode="extended", yscrollcommand=scrollbar.set, exportselection=False,
    )
    scrollbar.config(command=listbox.yview)
    listbox.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    shown = []

    def refresh_list(*_args):
        needle = filter_var.get().lower()
        listbox.delete(0, "end")
        shown.clear()
        for game in COMMON_GAMES:
            if needle not in game["name"].lower():
                continue
            already = is_already_added(game)
            listbox.insert("end", f"{game['name']}  (already watching)" if already else game["name"])
            if already:
                listbox.itemconfig("end", fg="#888888")
            shown.append(game)

    filter_var.trace_add("write", refresh_list)
    refresh_list()

    def add_selected():
        for index in listbox.curselection():
            game = shown[index]
            if "title_contains" in game:
                if not is_already_added(game):
                    add_window_row(game["process_name"], game["title_contains"], game["name"])
            else:
                insert_unique(game["process_name"])
        picker.destroy()

    button_bar = tk.Frame(picker)
    button_bar.pack(fill="x", padx=10, pady=10)
    tk.Button(button_bar, text="Cancel", command=picker.destroy).pack(side="right")
    tk.Button(button_bar, text="Add Selected", command=add_selected).pack(side="right", padx=8)
    listbox.bind("<Double-Button-1>", lambda e: add_selected())
    filter_entry.bind("<Return>", lambda e: add_selected())


def build_watched_games_editor(parent, initial_games, on_pick_common=None):
    tk.Label(
        parent,
        text=f"Watched game processes (exact process name, e.g. {platform_common.example_executable_name()})",
        anchor="w",
        bg=DARK_BG, fg=DARK_FG,
    ).pack(anchor="w", padx=10, pady=(10, 2))
    frame = tk.Frame(parent, bg=DARK_BG)
    frame.pack(fill="x", padx=10, pady=(0, 10))
    listbox = tk.Listbox(
        frame, height=8, width=32, exportselection=False, bg=DARK_ENTRY_BG, fg=DARK_FG,
        selectbackground=DARK_FG, selectforeground=DARK_BG, highlightthickness=0,
    )
    listbox.pack(side="left", fill="both", expand=True)
    for g in initial_games:
        listbox.insert("end", g)

    controls = tk.Frame(frame, bg=DARK_BG)
    controls.pack(side="left", fill="y", padx=(10, 0))
    entry_var = tk.StringVar()
    tk.Entry(
        controls, textvariable=entry_var, width=22, bg=DARK_ENTRY_BG, fg=DARK_FG, insertbackground=DARK_FG,
    ).pack(pady=(0, 4))

    def current_watched_lower():
        return {listbox.get(i).lower() for i in range(listbox.size())}

    def insert_unique(value):
        value = value.strip()
        if value and value.lower() not in current_watched_lower():
            listbox.insert("end", value)

    def add_game():
        insert_unique(entry_var.get())
        entry_var.set("")

    def remove_selected():
        for index in reversed(listbox.curselection()):
            listbox.delete(index)

    def pick_from_running():
        open_process_picker(
            parent,
            on_add=lambda names: [insert_unique(n) for n in names],
            multiselect=True,
            already_selected=current_watched_lower,
        )

    def dark_button(parent_, **kwargs):
        return tk.Button(
            parent_, bg=DARK_ENTRY_BG, fg=DARK_FG, activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG, **kwargs
        )

    dark_button(controls, text="Add", command=add_game).pack(fill="x")
    dark_button(controls, text="Remove Selected", command=remove_selected).pack(fill="x", pady=(4, 0))
    dark_button(controls, text="Pick Running...", command=pick_from_running).pack(fill="x", pady=(4, 0))
    if on_pick_common:
        dark_button(
            controls, text="Common Games...", command=lambda: on_pick_common(insert_unique, current_watched_lower)
        ).pack(fill="x", pady=(4, 0))
    return listbox


def build_watched_windows_editor(parent, initial_rows):
    def dark_button(parent_, **kwargs):
        return tk.Button(
            parent_, bg=DARK_ENTRY_BG, fg=DARK_FG, activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG, **kwargs
        )

    tk.Label(
        parent,
        text=(
            "Window-title rules (for games sharing a generic process name, e.g. "
            f"{'javaw.exe' if sys.platform == 'win32' else 'java'})"
        ),
        anchor="w",
        bg=DARK_BG, fg=DARK_FG,
    ).pack(anchor="w", padx=10, pady=(10, 2))

    header = tk.Frame(parent, bg=DARK_BG)
    header.pack(fill="x", padx=10)
    for text, w in (("Process name", 16), ("Title contains", 16), ("Display name", 16)):
        tk.Label(header, text=text, width=w, anchor="w", bg=DARK_BG, fg=DARK_FG).pack(side="left", padx=2)

    container = tk.Frame(parent, bg=DARK_BG)
    container.pack(fill="x", padx=10)
    rows = []

    def add_row(process="", title="", display=""):
        row_frame = tk.Frame(container, bg=DARK_BG)
        row_frame.pack(fill="x", pady=2)
        process_var = tk.StringVar(value=process)
        title_var = tk.StringVar(value=title)
        display_var = tk.StringVar(value=display)
        for var in (process_var, title_var, display_var):
            tk.Entry(
                row_frame, textvariable=var, width=16, bg=DARK_ENTRY_BG, fg=DARK_FG, insertbackground=DARK_FG,
            ).pack(side="left", padx=2)
        entry = {"process": process_var, "title": title_var, "display": display_var}

        def pick():
            open_process_picker(
                parent, on_add=lambda names: process_var.set(names[0]) if names else None, multiselect=False
            )

        dark_button(row_frame, text="Pick...", command=pick).pack(side="left", padx=2)

        def remove():
            row_frame.destroy()
            rows.remove(entry)

        dark_button(row_frame, text="Remove", command=remove).pack(side="left", padx=4)
        rows.append(entry)

    for w in initial_rows:
        add_row(w.get("process_name", ""), w.get("title_contains", ""), w.get("display_name", ""))

    dark_button(parent, text="+ Add Window Rule", command=lambda: add_row()).pack(anchor="w", padx=10, pady=(4, 10))
    return rows, add_row


# Friendly labels for OBS's audio-relevant input kinds, shown in the input picker so multiple
# similarly-named devices (e.g. two microphones) can actually be told apart when picking one --
# raw kind strings like "wasapi_input_capture" mean nothing to most users. Anything not listed
# here just falls back to showing its raw kind string.
INPUT_KIND_LABELS = {
    "wasapi_output_capture": "Desktop Audio",
    "wasapi_input_capture": "Microphone/Aux",
    "wasapi_process_output_capture": "Application Audio Capture",
    "dshow_input": "Video Capture Device",
    "browser_source": "Browser Source",
    "ffmpeg_source": "Media Source",
    "vlc_source": "Media Source (VLC)",
}


def open_obs_input_picker(parent, get_ws_config, on_pick):
    """Connects to OBS with whatever WebSocket settings are currently in the form (not
    necessarily saved yet) and lets the user pick an existing input by name, instead of typing
    it in from memory. Best-effort: if OBS isn't reachable, tells the user and lets them type
    the name in manually instead, same as this field always supported."""
    try:
        ws_config = get_ws_config()
    except Exception:
        ws_config = None

    client = connect_obs(ws_config, retries=1, delay=0) if ws_config else None
    if not client:
        messagebox.showwarning(
            "Can't reach OBS",
            "Could not connect to OBS over its WebSocket using the settings above. Make sure OBS is "
            "running and the host/port/password are correct, or just type the input's exact name in "
            "by hand.",
            parent=parent,
        )
        return

    try:
        by_name = {i["inputName"]: i.get("inputKind", "") for i in client.get_input_list().inputs}
        inputs = sorted(by_name.items(), key=lambda kv: kv[0].lower())
    except Exception as exc:
        messagebox.showwarning("Can't list OBS inputs", str(exc), parent=parent)
        return
    finally:
        client.disconnect()

    if not inputs:
        messagebox.showinfo(
            "No inputs found", "OBS reported no inputs in the current scene collection.", parent=parent
        )
        return

    picker = tk.Toplevel(parent)
    picker.title("Pick OBS Input")
    picker.geometry("360x380")
    picker.transient(parent.winfo_toplevel())
    picker.grab_set()

    list_frame = tk.Frame(picker)
    list_frame.pack(fill="both", expand=True, padx=10, pady=10)
    scrollbar = tk.Scrollbar(list_frame, orient="vertical")
    listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, exportselection=False)
    scrollbar.config(command=listbox.yview)
    listbox.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    for name, kind in inputs:
        listbox.insert("end", f"{name}  —  {INPUT_KIND_LABELS.get(kind, kind)}")

    def select():
        selection = listbox.curselection()
        if selection:
            on_pick(inputs[selection[0]][0])
        picker.destroy()

    button_bar = tk.Frame(picker)
    button_bar.pack(fill="x", padx=10, pady=(0, 10))
    tk.Button(button_bar, text="Cancel", command=picker.destroy).pack(side="right")
    tk.Button(button_bar, text="Select", command=select).pack(side="right", padx=8)
    listbox.bind("<Double-Button-1>", lambda e: select())


def build_custom_keybinds_editor(parent, initial_rows):
    """Row editor for obs.custom_keybinds -- see the CUSTOM_KEYBIND_ACTIONS comment above for why
    this app registers its own system-wide keybinds instead of trying to rebind OBS's."""
    tk.Label(
        parent,
        text=(
            "System-wide keybinds captured by this app itself (not OBS) that call the matching "
            "OBS WebSocket action directly the moment they're pressed -- they work without ever "
            "touching OBS's own Hotkeys settings, but only while OBS is running and connected, and "
            "only while this app is running. \"Split Recording File\" uses the buffer configured "
            "above under Manual Split. A key already claimed by another running app may fail to "
            "register; check the log if a keybind doesn't seem to fire."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).pack(fill="x", padx=10, pady=(10, 6))

    if not platform_common.global_hotkeys_supported():
        # Wayland has no unprivileged API for a system-wide hotkey at all (see
        # CROSS_PLATFORM_PLAN.md §2.3/Phase 3) -- shown up front rather than letting keybinds
        # configured here silently never fire, which would otherwise be the only sign anything's
        # different this session (run_custom_keybind_listener itself only logs one warning, easy
        # to miss outside the log file).
        tk.Label(
            parent,
            text=(
                "Custom keybinds aren't supported under Wayland (no unprivileged API exists for a "
                "system-wide hotkey) -- switch to an X11 session to use this feature. Keybinds "
                "configured below will be saved but won't fire this session."
            ),
            anchor="w", justify="left", wraplength=520, fg="#e0a030", bg=DARK_BG,
        ).pack(fill="x", padx=10, pady=(0, 6))

    def dark_checkbutton(parent_, **kwargs):
        return tk.Checkbutton(
            parent_, bg=DARK_BG, activebackground=DARK_BG, selectcolor=DARK_ENTRY_BG, **kwargs
        )

    # A grid table (not pack) so header labels and row widgets share real column boundaries --
    # packing a Label(width=N) next to a Checkbutton(width=N) or Combobox(width=N) doesn't
    # actually line them up, since a Checkbutton's width includes its indicator box on top of the
    # same character-width unit, so identical widths still render as different pixel widths.
    table = tk.Frame(parent, bg=DARK_BG)
    table.pack(fill="x", padx=10)
    for col, text in enumerate(["On", "Action", "Ctrl", "Alt", "Shift", "Win", "Key", ""]):
        tk.Label(
            table, text=text, anchor="w", font=("Segoe UI", 9, "bold"), bg=DARK_BG, fg=DARK_FG,
        ).grid(row=0, column=col, sticky="w", padx=4, pady=(0, 4))

    rows = []

    def renumber():
        for i, entry in enumerate(rows, start=1):
            for widget in entry["widgets"]:
                widget.grid(row=i)

    def add_row(enabled=True, action="split_record_file", modifiers=None, key="S"):
        modifiers = modifiers if modifiers is not None else ["ctrl", "alt"]
        r = len(rows) + 1

        enabled_var = tk.BooleanVar(value=enabled)
        enabled_cb = dark_checkbutton(table, variable=enabled_var)
        enabled_cb.grid(row=r, column=0, padx=4, pady=1)

        action_label_var = tk.StringVar(value=CUSTOM_KEYBIND_ACTIONS.get(action, action))
        action_combo = ttk.Combobox(
            table, textvariable=action_label_var, values=list(CUSTOM_KEYBIND_ACTIONS.values()),
            state="readonly", width=24, style="Settings.TCombobox",
        )
        action_combo.grid(row=r, column=1, sticky="w", padx=4, pady=1)

        ctrl_var = tk.BooleanVar(value="ctrl" in modifiers)
        alt_var = tk.BooleanVar(value="alt" in modifiers)
        shift_var = tk.BooleanVar(value="shift" in modifiers)
        win_var = tk.BooleanVar(value="win" in modifiers)
        ctrl_cb = dark_checkbutton(table, variable=ctrl_var)
        ctrl_cb.grid(row=r, column=2, padx=4, pady=1)
        alt_cb = dark_checkbutton(table, variable=alt_var)
        alt_cb.grid(row=r, column=3, padx=4, pady=1)
        shift_cb = dark_checkbutton(table, variable=shift_var)
        shift_cb.grid(row=r, column=4, padx=4, pady=1)
        win_cb = dark_checkbutton(table, variable=win_var)
        win_cb.grid(row=r, column=5, padx=4, pady=1)

        key_var = tk.StringVar(value=(key or "S").upper())
        key_combo = ttk.Combobox(
            table, textvariable=key_var, values=CUSTOM_KEYBIND_KEY_OPTIONS, state="readonly", width=4,
            style="Settings.TCombobox",
        )
        key_combo.grid(row=r, column=6, padx=4, pady=1)

        widgets = [enabled_cb, action_combo, ctrl_cb, alt_cb, shift_cb, win_cb, key_combo]

        entry = {
            "enabled": enabled_var, "action_label": action_label_var,
            "ctrl": ctrl_var, "alt": alt_var, "shift": shift_var, "win": win_var,
            "key": key_var, "widgets": widgets,
        }

        def remove():
            for widget in widgets:
                widget.destroy()
            remove_btn.destroy()
            rows.remove(entry)
            renumber()

        remove_btn = tk.Button(
            table, text="Remove", command=remove, bg=DARK_ENTRY_BG, fg=DARK_FG,
            activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
        )
        remove_btn.grid(row=r, column=7, padx=4, pady=1)
        widgets.append(remove_btn)
        rows.append(entry)

    for kb in initial_rows:
        add_row(
            kb.get("enabled", True), kb.get("action", "split_record_file"),
            kb.get("modifiers", []), kb.get("key", "S"),
        )

    tk.Button(
        parent, text="+ Add Keybind", command=lambda: add_row(), bg=DARK_ENTRY_BG, fg=DARK_FG,
        activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
    ).pack(anchor="w", padx=10, pady=(4, 10))
    return rows, add_row


# Curated common apps for the multi-track quick-setup wizard, grouped into the same category
# scheme the app's own author ended up hand-building: voice chat on one track, music on another,
# browsers on a third. Not exhaustive -- anything not listed can still be added by hand via
# "+ Add Track Mapping", this just covers what most people actually run.
COMMON_AUDIO_APPS = [
    {"name": "Discord", "process_name": "Discord.exe", "category": "Voice Chat"},
    {"name": "Slack", "process_name": "slack.exe", "category": "Voice Chat"},
    {"name": "Zoom", "process_name": "Zoom.exe", "category": "Voice Chat"},
    {"name": "Spotify", "process_name": "Spotify.exe", "category": "Music"},
    {"name": "Apple Music", "process_name": "AppleMusic.exe", "category": "Music"},
    # YouTube Music deliberately excluded: it runs as a browser tab for most people, not a
    # standalone process, so it's already covered by the Browser category -- listing it
    # separately either does nothing (no such process to capture) or double-captures the same
    # audio once a browser is also selected.
    {"name": "Chrome", "process_name": "chrome.exe", "category": "Browser"},
    {"name": "Firefox", "process_name": "firefox.exe", "category": "Browser"},
    {"name": "Edge", "process_name": "msedge.exe", "category": "Browser"},
]
QUICK_SETUP_CATEGORY_TRACKS = {"Voice Chat": 4, "Music": 5, "Browser": 6}


def compute_quick_setup_tracks(client, desktop_name, mic_name, game_audio_name, selected_apps):
    """Core logic behind the multi-track quick-setup wizard's Apply button: builds the
    resulting track-mapping list, creating an OBS Application Audio Capture input for any
    selected app that doesn't already exist under that exact name (existing ones are reused,
    never duplicated). Kept separate from the dialog so it's exercisable directly against a
    fake OBS client in tests, without needing a real GUI or OBS connection.

    desktop_name/mic_name/game_audio_name are input names to route to tracks 1(+2)/3, or None
    to skip that track. selected_apps is a list of COMMON_AUDIO_APPS-shaped dicts to route to
    their category's track, creating each one's source first if it isn't already present.

    Also returns app_captures: {input_name: process_name} for every selected app, whether newly
    created here or already existing. OBS can silently rewrite a wasapi_process_output_capture
    input's exe-only "::process_name" window match into a specific, eventually-stale window
    title (e.g. if its Properties dialog is ever opened in OBS itself) -- the caller is expected
    to persist this mapping and re-apply it via set_game_audio_capture_target on every recording
    start (the same self-healing re-point obs.game_audio_capture already gets), so an app's
    isolation can't silently stay broken after drifting once.
    """
    tracks = []
    created = []
    app_captures = {}
    current_inputs = {i["inputName"] for i in client.get_input_list().inputs}
    scene = client.get_current_program_scene().current_program_scene_name

    if desktop_name:
        tracks.append({"input_name": desktop_name, "track": 1})
    if mic_name:
        tracks.append({"input_name": mic_name, "track": 1})
        tracks.append({"input_name": mic_name, "track": 2})
    if game_audio_name:
        tracks.append({"input_name": game_audio_name, "track": 3})

    for app in selected_apps:
        name = app["name"]
        if name not in current_inputs:
            client.create_input(
                scene, name, "wasapi_process_output_capture",
                {"window": f"::{app['process_name']}", "priority": 2}, True,
            )
            created.append(name)
        tracks.append({"input_name": name, "track": QUICK_SETUP_CATEGORY_TRACKS[app["category"]]})
        app_captures[name] = app["process_name"]

    return tracks, created, app_captures


def open_multi_track_quick_setup(parent, get_ws_config, game_audio_config, on_apply):
    """Reproduces the manual multi-track setup this app's own author needed by hand (pick a
    mic, add capture sources for common apps, route everything to a sensible track layout) for
    anyone in a few clicks: desktop+mic -> track 1, mic alone -> track 2, game audio (if already
    configured) -> track 3, voice chat -> 4, music -> 5, browsers -> 6. Reuses an existing OBS
    input by name instead of duplicating it; only creates one for an app that isn't there yet."""
    try:
        ws_config = get_ws_config()
    except Exception:
        ws_config = None
    client = connect_obs(ws_config, retries=1, delay=0) if ws_config else None
    if not client:
        messagebox.showwarning(
            "Can't reach OBS",
            "Could not connect to OBS over its WebSocket using the settings above. Make sure OBS "
            "is running and the host/port/password are correct, then try again.",
            parent=parent,
        )
        return

    try:
        inputs = {i["inputName"]: i.get("inputKind", "") for i in client.get_input_list().inputs}
    except Exception as exc:
        messagebox.showwarning("Can't read OBS state", str(exc), parent=parent)
        return
    finally:
        client.disconnect()

    desktop_options = ["(none)"] + sorted(
        (n for n, k in inputs.items() if k == "wasapi_output_capture"), key=str.lower
    )
    mic_options = ["(none)"] + sorted(
        (n for n, k in inputs.items() if k == "wasapi_input_capture"), key=str.lower
    )

    dialog = tk.Toplevel(parent)
    dialog.title("Quick Multi-Track Setup")
    dialog.geometry("440x580")
    dialog.transient(parent.winfo_toplevel())
    dialog.grab_set()

    tk.Label(
        dialog,
        text=(
            "Sets up separate recording tracks for a mic, desktop audio, game audio, and any "
            "common apps you pick below -- creating their OBS sources if they don't exist yet, "
            "and routing everything into the dedicated multi-track profile."
        ),
        anchor="w", justify="left", wraplength=410,
    ).pack(fill="x", padx=10, pady=(10, 8))

    form = tk.Frame(dialog)
    form.pack(fill="x", padx=10)
    tk.Label(form, text="Desktop audio:", anchor="w", width=14).grid(row=0, column=0, sticky="w", pady=2)
    desktop_var = tk.StringVar(value=desktop_options[1] if len(desktop_options) > 1 else desktop_options[0])
    ttk.Combobox(form, textvariable=desktop_var, values=desktop_options, state="readonly", width=26).grid(
        row=0, column=1, sticky="w", pady=2
    )
    tk.Label(form, text="Microphone:", anchor="w", width=14).grid(row=1, column=0, sticky="w", pady=2)
    mic_var = tk.StringVar(value=mic_options[1] if len(mic_options) > 1 else mic_options[0])
    ttk.Combobox(form, textvariable=mic_var, values=mic_options, state="readonly", width=26).grid(
        row=1, column=1, sticky="w", pady=2
    )

    game_audio_enabled = bool(game_audio_config.get("enabled"))
    game_audio_name = game_audio_config.get("input_name", "Game Audio")
    game_audio_note = (
        f"Game audio ('{game_audio_name}') will be routed to track 3 automatically."
        if game_audio_enabled else
        "Game audio isolation isn't enabled above, so track 3 is left out for now -- enable "
        "\"Game Audio Isolation\" above first if you want it included."
    )
    tk.Label(dialog, text=game_audio_note, anchor="w", justify="left", wraplength=410, fg="#555555").pack(
        fill="x", padx=10, pady=(8, 4)
    )

    app_vars = {}
    for category in ("Voice Chat", "Music", "Browser"):
        track = QUICK_SETUP_CATEGORY_TRACKS[category]
        tk.Label(dialog, text=f"{category}  (track {track})", font=("Segoe UI", 9, "bold"), anchor="w").pack(
            fill="x", padx=10, pady=(8, 2)
        )
        apps_row = tk.Frame(dialog)
        apps_row.pack(fill="x", padx=10)
        for app in COMMON_AUDIO_APPS:
            if app["category"] != category:
                continue
            var = tk.BooleanVar(value=False)
            label = app["name"] if app["name"] in inputs else f"{app['name']} (will create)"
            tk.Checkbutton(apps_row, text=label, variable=var).pack(anchor="w")
            app_vars[app["name"]] = (var, app)

    status_label = tk.Label(dialog, text="", fg="#b00020", anchor="w", justify="left", wraplength=410)
    status_label.pack(fill="x", padx=10, pady=(6, 0))

    def do_apply():
        try:
            apply_ws_config = get_ws_config()
        except Exception:
            apply_ws_config = None
        apply_client = connect_obs(apply_ws_config, retries=1, delay=0) if apply_ws_config else None
        if not apply_client:
            status_label.config(text="Could not connect to OBS -- check the WebSocket settings above and try again.")
            return

        selected_apps = [app for var, app in app_vars.values() if var.get()]
        try:
            tracks, created, app_captures = compute_quick_setup_tracks(
                apply_client,
                desktop_var.get() if desktop_var.get() != "(none)" else None,
                mic_var.get() if mic_var.get() != "(none)" else None,
                game_audio_name if game_audio_enabled else None,
                selected_apps,
            )
        except Exception as exc:
            status_label.config(text=f"Something went wrong talking to OBS: {exc}")
            apply_client.disconnect()
            return
        apply_client.disconnect()

        on_apply(tracks, app_captures)
        dialog.destroy()
        summary = f"Applied {len(tracks)} track mapping(s)."
        if created:
            summary += f" Created new OBS sources: {', '.join(created)}."
        messagebox.showinfo("Quick setup applied", summary, parent=parent)

    button_bar = tk.Frame(dialog)
    button_bar.pack(fill="x", padx=10, pady=10, side="bottom")
    tk.Button(button_bar, text="Cancel", command=dialog.destroy).pack(side="right")
    tk.Button(button_bar, text="Apply", command=do_apply).pack(side="right", padx=8)


def build_multi_track_audio_editor(parent, initial_rows, get_ws_config):
    def dark_button(parent_, **kwargs):
        return tk.Button(
            parent_, bg=DARK_ENTRY_BG, fg=DARK_FG, activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG, **kwargs
        )

    tk.Label(
        parent, text="Which input feeds which recording track, in the dedicated profile below", anchor="w",
        bg=DARK_BG, fg=DARK_FG,
    ).pack(anchor="w", padx=10, pady=(10, 2))

    header = tk.Frame(parent, bg=DARK_BG)
    header.pack(fill="x", padx=10)
    tk.Label(header, text="Input name (exact, as in OBS)", width=30, anchor="w", bg=DARK_BG, fg=DARK_FG).pack(
        side="left", padx=2
    )
    tk.Label(header, text="Track", width=6, anchor="w", bg=DARK_BG, fg=DARK_FG).pack(side="left", padx=2)

    container = tk.Frame(parent, bg=DARK_BG)
    container.pack(fill="x", padx=10)
    rows = []

    def add_row(input_name="", track=1):
        row_frame = tk.Frame(container, bg=DARK_BG)
        row_frame.pack(fill="x", pady=2)
        name_var = tk.StringVar(value=input_name)
        track_var = tk.StringVar(value=str(track))
        tk.Entry(
            row_frame, textvariable=name_var, width=30, bg=DARK_ENTRY_BG, fg=DARK_FG, insertbackground=DARK_FG,
        ).pack(side="left", padx=2)
        ttk.Combobox(
            row_frame, textvariable=track_var, values=[str(i) for i in range(1, 7)],
            state="readonly", width=4, style="Settings.TCombobox",
        ).pack(side="left", padx=2)
        entry = {"input_name": name_var, "track": track_var}

        def pick():
            open_obs_input_picker(parent, get_ws_config, on_pick=lambda name: name_var.set(name))

        dark_button(row_frame, text="Pick...", command=pick).pack(side="left", padx=2)

        def remove():
            row_frame.destroy()
            rows.remove(entry)

        dark_button(row_frame, text="Remove", command=remove).pack(side="left", padx=4)
        entry["frame"] = row_frame
        rows.append(entry)

    def clear_rows():
        for entry in list(rows):
            entry["frame"].destroy()
        rows.clear()

    for t in initial_rows:
        add_row(t.get("input_name", ""), t.get("track", 1))

    dark_button(parent, text="+ Add Track Mapping", command=lambda: add_row()).pack(
        anchor="w", padx=10, pady=(4, 10)
    )
    return rows, add_row, clear_rows


def build_launcher_section(parent, row, title, launcher_config, include_install_dirs):
    add_section_label(parent, row, title)
    row += 1
    enabled_var = tk.BooleanVar(value=launcher_config.get("enabled", True))
    add_checkbox(parent, row, "Enabled", enabled_var)
    row += 1
    install_dirs_var = None
    if include_install_dirs:
        install_dirs_var = tk.StringVar(value=format_csv_field(launcher_config.get("install_dirs", [])))
        add_labeled_entry(parent, row, "Install folders (comma-separated)", install_dirs_var)
        row += 1
    exclude_var = tk.StringVar(value=format_csv_field(launcher_config.get("exclude_keywords", [])))
    add_labeled_entry(parent, row, "Exclude keywords (comma-separated)", exclude_var)
    row += 1
    return row, {"enabled": enabled_var, "install_dirs": install_dirs_var, "exclude_keywords": exclude_var}


EDITOR_STUCK_TIMEOUT_SECONDS = 300


def open_config_editor_window(editor_state, restart_callback, overlay_state, icon):
    if editor_state.get("open"):
        if time.time() - editor_state.get("opened_at", 0) < EDITOR_STUCK_TIMEOUT_SECONDS:
            logging.info("Settings editor is already open.")
            return
        logging.warning(
            "Settings editor has appeared open for over %d seconds without closing; assuming "
            "it's stuck and allowing a new attempt.", EDITOR_STUCK_TIMEOUT_SECONDS,
        )

    overlay_root = overlay_state.get("root")
    if not overlay_root:
        logging.warning("Overlay isn't ready yet; can't open Settings. Try again in a moment.")
        return

    editor_state["open"] = True
    editor_state["opened_at"] = time.time()

    def on_close():
        editor_state["open"] = False
        # pystray only rebuilds the native tray menu right after a menu item is clicked (see the
        # matching comment in the watcher loop) -- without this, "Edit Settings..." stays greyed
        # out until some unrelated menu click happens to refresh it, even though the editor is
        # long closed.
        icon.update_menu()

    def build():
        try:
            _run_config_editor(overlay_root, restart_callback, on_close)
        except Exception:
            logging.exception("Settings editor crashed.")
            on_close()

    # Runs the whole editor (window construction, and everything that happens on it -- Pick...
    # dialogs, Save, etc.) as a Toplevel of the overlay's already-running interpreter, scheduled
    # via .after() so it executes on that interpreter's own thread rather than this caller's
    # (pystray's) thread -- Tkinter widgets need to be built on the thread that owns their
    # interpreter's event loop. This used to spin up a second, fully independent tk.Tk() on its
    # own thread instead; PyInstaller's bundled Tcl/Tk turned out not to reliably support that
    # (a hang at best, a hard process crash -- the "failed to remove temp dir" message being a
    # symptom of that abrupt exit -- at worst). One interpreter, one thread, no more of that.
    overlay_root.after(0, build)


def _run_config_editor(master_root, restart_callback, on_close):
    config = load_config()

    root = tk.Toplevel(master_root)
    root.bind("<Destroy>", lambda event: on_close() if event.widget is root else None)

    root.title("OBS Auto Recorder - Settings")
    root.geometry("620x560")
    root.minsize(520, 420)
    root.configure(bg=DARK_BG)
    root.lift()
    root.focus_force()

    # Named "Settings.TNotebook" (rather than reconfiguring "TNotebook" directly) so a future
    # ttk widget elsewhere that hasn't opted into the dark theme doesn't inherit this by accident.
    style = ttk.Style()
    style.configure("Settings.TNotebook", background=DARK_BG, borderwidth=0)
    style.configure(
        "Settings.TNotebook.Tab", background=DARK_ENTRY_BG, foreground=DARK_FG, padding=(10, 4),
    )
    style.map(
        "Settings.TNotebook.Tab",
        background=[("selected", DARK_BG)], foreground=[("selected", DARK_FG)],
    )
    style.configure(
        "Settings.TCombobox", fieldbackground=DARK_ENTRY_BG, background=DARK_BG, foreground=DARK_FG,
        arrowcolor=DARK_FG,
    )
    style.map(
        "Settings.TCombobox",
        fieldbackground=[("readonly", DARK_ENTRY_BG), ("disabled", DARK_ENTRY_BG), ("!disabled", DARK_ENTRY_BG)],
        foreground=[("readonly", DARK_FG), ("disabled", DARK_MUTED_FG), ("!disabled", DARK_FG)],
        selectbackground=[("readonly", DARK_ENTRY_BG), ("!disabled", DARK_ENTRY_BG)],
        selectforeground=[("readonly", DARK_FG), ("!disabled", DARK_FG)],
        arrowcolor=[("readonly", DARK_FG), ("!disabled", DARK_FG)],
    )
    style.configure("Settings.Horizontal.TProgressbar", background="#f5a623", troughcolor=DARK_ENTRY_BG)

    notebook = ttk.Notebook(root, style="Settings.TNotebook")
    notebook.pack(fill="both", expand=True)

    # --- General ---
    general_tab = make_scrollable_tab(notebook, "General")
    general_tab.columnconfigure(1, weight=1)
    poll_interval_var = tk.StringVar(value=str(config.get("poll_interval_seconds", 1.5)))
    add_labeled_entry(general_tab, 0, "Poll interval (seconds)", poll_interval_var)
    subfolders_var = tk.BooleanVar(value=config.get("organize_into_game_subfolders", False))
    add_checkbox(general_tab, 1, "Organize recordings into per-game subfolders", subfolders_var)
    log_file_var = tk.StringVar(value=config.get("log_file", "") or "")
    add_labeled_entry(general_tab, 2, "Log file", log_file_var)

    is_frozen = getattr(sys, "frozen", False)
    startup_var = tk.BooleanVar(value=is_startup_shortcut_enabled())
    startup_label = "Launch automatically at login"
    if not is_frozen:
        startup_label += " (only available from the built app)"
    startup_checkbox = tk.Checkbutton(
        general_tab, text=startup_label, variable=startup_var, bg=DARK_BG, fg=DARK_FG,
        activebackground=DARK_BG, activeforeground=DARK_FG, selectcolor=DARK_ENTRY_BG,
        disabledforeground=DARK_MUTED_FG,
    )
    startup_checkbox.grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=4)
    if not is_frozen:
        startup_checkbox.config(state="disabled")

    add_section_label(general_tab, 4, "Overlay")
    overlay_config = config.get("overlay", {})
    settings_monitors = get_monitor_rects()
    monitor_labels = ["Off"] + [m["label"] for m in settings_monitors]
    configured_monitor_label = overlay_config.get("default_monitor_label") or "Off"
    if configured_monitor_label not in monitor_labels:
        configured_monitor_label = "Off"
    default_monitor_var = tk.StringVar(value=configured_monitor_label)
    tk.Label(general_tab, text="Default monitor", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=5, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        general_tab, textvariable=default_monitor_var, values=monitor_labels, state="readonly", width=36,
        style="Settings.TCombobox",
    ).grid(row=5, column=1, sticky="w", pady=4)
    tk.Label(
        general_tab,
        text=(
            "    Which monitor the overlay (recording status dot, audio mixer levels) appears on "
            "when the app starts. \"Off\" leaves it hidden at startup, same as before this setting "
            "existed -- you can still turn it on any time from the tray menu's Overlay Monitor list."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=6, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))

    start_with_overlay_var = tk.BooleanVar(value=overlay_config.get("start_with_overlay", False))
    add_checkbox(general_tab, 7, "Start with audio mixer levels shown", start_with_overlay_var)
    tk.Label(
        general_tab,
        text=(
            "    Same as toggling \"Show Audio Mixer Levels\" from the tray menu, but applied "
            "automatically at launch -- picks a monitor for it if a default monitor isn't set above."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=8, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))

    # --- Games ---
    games_tab = make_scrollable_tab(notebook, "Watched Games")

    # build_watched_windows_editor() (and the add_window_row it returns) doesn't exist yet at the
    # point the games editor above it needs to wire up its "Common Games..." button -- filled in
    # right after both editors are built, below. Only called once the button is actually clicked.
    window_editor_ref = {"add_row": None, "rows": None}

    def on_pick_common(insert_unique, current_watched_lower):
        def current_window_keys():
            return {
                (row["process"].get().strip().lower(), row["title"].get().strip().lower())
                for row in window_editor_ref["rows"]
            }

        open_common_games_picker(
            games_tab, insert_unique, current_watched_lower, window_editor_ref["add_row"], current_window_keys
        )

    games_listbox = build_watched_games_editor(games_tab, config.get("watched_games", []), on_pick_common)
    window_rows, window_add_row = build_watched_windows_editor(games_tab, config.get("watched_windows", []))
    window_editor_ref["add_row"] = window_add_row
    window_editor_ref["rows"] = window_rows

    # --- Launchers ---
    launchers_tab = make_scrollable_tab(notebook, "Launchers")
    launchers_tab.columnconfigure(1, weight=1)
    row = 0
    row, steam_vars = build_launcher_section(launchers_tab, row, "Steam", config.get("steam", {}), False)
    steam_drives_var = tk.StringVar(value=format_csv_field(config.get("steam", {}).get("allowed_drives", [])))
    # The filter itself is a plain path-prefix check (see normalize_allowed_library_roots), so it
    # means the same thing on every OS -- just phrased with each OS's own example: bare drive
    # letters on Windows, full folder paths (mount points) elsewhere.
    allowed_roots_label = (
        "Allowed drives (comma-separated, e.g. C, D)" if sys.platform == "win32"
        else "Allowed library folders (comma-separated full paths, optional)"
    )
    add_labeled_entry(launchers_tab, row, allowed_roots_label, steam_drives_var)
    row += 1
    row, epic_vars = build_launcher_section(launchers_tab, row, "Epic Games", config.get("epic", {}), False)
    row, gog_vars = build_launcher_section(launchers_tab, row, "GOG Galaxy", config.get("gog", {}), False)
    row, xbox_vars = build_launcher_section(launchers_tab, row, "Xbox / PC Game Pass", config.get("xbox", {}), True)
    row, battlenet_vars = build_launcher_section(launchers_tab, row, "Battle.net", config.get("battlenet", {}), True)

    # --- OBS ---
    obs_tab = make_scrollable_tab(notebook, "OBS")
    obs_tab.columnconfigure(1, weight=1)
    obs_config = config.get("obs", {})
    row = 0
    obs_process_var = tk.StringVar(value=obs_config.get("process_name", "obs64.exe"))
    add_labeled_entry(obs_tab, row, "Process name", obs_process_var)
    row += 1
    obs_path_var = tk.StringVar(value=obs_config.get("path", ""))
    add_labeled_entry(obs_tab, row, "OBS executable path", obs_path_var)
    add_browse_button(obs_tab, row, obs_path_var)
    row += 1
    obs_launch_args_var = tk.StringVar(value=format_csv_field(obs_config.get("launch_args", [])))
    add_labeled_entry(obs_tab, row, "Launch args (comma-separated)", obs_launch_args_var)
    row += 1
    obs_startup_wait_var = tk.StringVar(value=str(obs_config.get("startup_wait_seconds", 8)))
    add_labeled_entry(obs_tab, row, "Startup wait (seconds)", obs_startup_wait_var)
    row += 1
    output_folder_var = tk.StringVar(value=obs_config.get("output_folder", "") or "")
    add_labeled_entry(obs_tab, row, "Recording output folder (optional)", output_folder_var)
    add_browse_button(obs_tab, row, output_folder_var, mode="dir")
    row += 1
    recording_format_var = tk.StringVar(value=obs_config.get("recording_format", "") or "")
    tk.Label(obs_tab, text="Recording format (optional)", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        obs_tab, textvariable=recording_format_var, values=RECORDING_FORMAT_OPTIONS, width=16,
        style="Settings.TCombobox",
    ).grid(row=row, column=1, sticky="w", pady=4)
    row += 1
    tk.Label(
        obs_tab,
        text=(
            "    Leave blank to use whatever OBS already has set. mkv/hybrid_mp4 are the "
            "reliable choices if multi-track audio is on; you can also type in any other format "
            "code OBS supports."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1
    configured_resolution = obs_config.get("recording_resolution") or OBS_RESOLUTION_OPTIONS[0]
    if configured_resolution not in OBS_RESOLUTION_OPTIONS:
        configured_resolution = OBS_RESOLUTION_OPTIONS[0]
    recording_resolution_var = tk.StringVar(value=configured_resolution)
    tk.Label(obs_tab, text="Recording resolution", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        obs_tab, textvariable=recording_resolution_var, values=OBS_RESOLUTION_OPTIONS,
        state="readonly", width=24, style="Settings.TCombobox",
    ).grid(row=row, column=1, sticky="w", pady=4)
    row += 1
    tk.Label(
        obs_tab,
        text=(
            "    OBS's actual Output (Scaled) Resolution -- this is what a recording is really "
            "encoded at, independent of the canvas size. A named preset keeps the canvas's own "
            "aspect ratio."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1

    ws_config = obs_config.get("websocket", {})
    add_section_label(obs_tab, row, "WebSocket")
    row += 1
    ws_host_var = tk.StringVar(value=ws_config.get("host", "localhost"))
    add_labeled_entry(obs_tab, row, "Host", ws_host_var)
    row += 1
    ws_port_var = tk.StringVar(value=str(ws_config.get("port", 4455)))
    add_labeled_entry(obs_tab, row, "Port", ws_port_var)
    row += 1
    ws_password_var = tk.StringVar(value=ws_config.get("password", ""))
    add_labeled_entry(obs_tab, row, "Password", ws_password_var)
    row += 1

    auto_split_config = obs_config.get("auto_split", {})
    add_section_label(obs_tab, row, "Automatic Split Matching")
    row += 1
    auto_split_enabled_var = tk.BooleanVar(value=auto_split_config.get("enabled", False))
    add_checkbox(obs_tab, row, "OBS's automatic file splitting is enabled", auto_split_enabled_var)
    row += 1
    auto_split_by_var = tk.StringVar(value=auto_split_config.get("by", "time"))
    tk.Label(obs_tab, text="Split by", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        obs_tab, textvariable=auto_split_by_var, values=["time", "size"], state="readonly", width=10,
        style="Settings.TCombobox",
    ).grid(row=row, column=1, sticky="w", pady=4)
    row += 1
    auto_split_minutes_var = tk.StringVar(value=str(auto_split_config.get("minutes", 20)))
    add_labeled_entry(obs_tab, row, "Minutes (if split by time)", auto_split_minutes_var)
    row += 1
    auto_split_tolerance_seconds_var = tk.StringVar(value=str(auto_split_config.get("tolerance_seconds", 8)))
    add_labeled_entry(obs_tab, row, "Tolerance seconds", auto_split_tolerance_seconds_var)
    row += 1
    auto_split_megabytes_var = tk.StringVar(value=str(auto_split_config.get("megabytes", 4096)))
    add_labeled_entry(obs_tab, row, "Megabytes (if split by size)", auto_split_megabytes_var)
    row += 1
    auto_split_tolerance_megabytes_var = tk.StringVar(value=str(auto_split_config.get("tolerance_megabytes", 50)))
    add_labeled_entry(obs_tab, row, "Tolerance megabytes", auto_split_tolerance_megabytes_var)
    row += 1

    manual_split_config = obs_config.get("manual_split", {})
    add_section_label(obs_tab, row, "Manual Split (tray menu)")
    row += 1
    tk.Label(
        obs_tab,
        text=(
            "    Adds a \"Split Recording File\" item to the tray menu that splits the current "
            "recording on demand, after an optional delay -- separate from OBS's own split hotkey."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1
    manual_split_enabled_var = tk.BooleanVar(value=manual_split_config.get("enabled", False))
    add_checkbox(obs_tab, row, "Show \"Split Recording File\" in the tray menu", manual_split_enabled_var)
    row += 1
    manual_split_buffer_var = tk.StringVar(value=str(manual_split_config.get("buffer_seconds", 0)))
    add_labeled_entry(obs_tab, row, "Buffer seconds before splitting", manual_split_buffer_var)
    row += 1

    recovery_config = obs_config.get("recovery", {})
    add_section_label(obs_tab, row, "OBS Health Recovery")
    row += 1
    memory_limit_var = tk.StringVar(value=str(recovery_config.get("memory_limit_gb", DEFAULT_OBS_MEMORY_LIMIT_GB)))
    add_labeled_entry(obs_tab, row, "Memory limit (GB)", memory_limit_var)
    row += 1
    recovery_cooldown_var = tk.StringVar(
        value=str(recovery_config.get("cooldown_seconds", DEFAULT_OBS_RECOVERY_COOLDOWN_SECONDS))
    )
    add_labeled_entry(obs_tab, row, "Restart cooldown (seconds)", recovery_cooldown_var)
    row += 1

    game_audio_config = obs_config.get("game_audio_capture", {})
    add_section_label(obs_tab, row, "Game Audio Isolation")
    row += 1
    game_audio_enabled_var = tk.BooleanVar(value=game_audio_config.get("enabled", False))
    add_checkbox(obs_tab, row, "Repoint Application Audio Capture at the detected game", game_audio_enabled_var)
    row += 1
    game_audio_input_var = tk.StringVar(value=game_audio_config.get("input_name", "Game Audio"))
    add_labeled_entry(obs_tab, row, "Input source name", game_audio_input_var)
    row += 1
    process_capture_sync_offset_var = tk.StringVar(
        value=str(obs_config.get("process_audio_capture_sync_offset_ms", DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS))
    )
    add_labeled_entry(obs_tab, row, "Audio capture sync offset (ms)", process_capture_sync_offset_var, width=10)
    row += 1
    calibrate_button = tk.Button(
        obs_tab, text="Calibrate Audio Sync...", bg=DARK_ENTRY_BG, fg=DARK_FG,
        activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
    )
    calibrate_button.grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 4))
    row += 1
    tk.Label(
        obs_tab,
        text=(
            "    Applied to Game Audio and every app capture below (Discord, Spotify, ...), never "
            "to Desktop Audio -- confirmed live that OBS's per-process audio capture runs "
            "noticeably behind plain device capture on Windows, which is audible as a "
            "doubled/echoed sound in any player that mixes more than one track together "
            "(Premiere, Discord's mobile app). The default is a reasonable starting point, but "
            "the exact amount varies by machine; \"Calibrate Audio Sync\" "
            "measures your own machine's real offset instead of guessing -- it briefly shows a "
            "small player window and plays a short clicking sound while OBS records a ~25 second "
            "test clip, so only run it while OBS is idle (not already recording something real)."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1

    def run_calibration_worker(ws_config, target_input):
        client = connect_obs(ws_config, retries=1, delay=0)

        def finish(result_ms, error_message):
            calibrate_button.config(state="normal", text="Calibrate Audio Sync...")
            if error_message:
                messagebox.showwarning("Can't calibrate", error_message, parent=obs_tab)
            elif result_ms is None:
                messagebox.showwarning(
                    "Calibration inconclusive",
                    "Could not confidently measure the sync offset, so the current value was left "
                    "unchanged. Make sure OBS is idle and ffplay.exe is installed alongside ffmpeg, "
                    "then try again -- check the log for details.",
                    parent=obs_tab,
                )
            else:
                process_capture_sync_offset_var.set(str(result_ms))
                messagebox.showinfo(
                    "Calibration complete",
                    f"Measured offset: {result_ms}ms. The field above has been updated -- click "
                    "Save for it to take effect.",
                    parent=obs_tab,
                )

        if not client:
            obs_tab.after(0, finish, None, (
                "Could not connect to OBS over its WebSocket using the settings above. Make sure "
                "OBS is running and the host/port/password are correct, then try again."
            ))
            return
        ffmpeg_path = resolve_ffmpeg_path("ffmpeg")
        if not ffmpeg_path:
            client.disconnect()
            obs_tab.after(0, finish, None, "ffmpeg isn't installed -- install it from Settings > Post-Processing first.")
            return
        try:
            result_ms = run_audio_sync_calibration(client, ffmpeg_path, target_input)
        finally:
            client.disconnect()
        obs_tab.after(0, finish, result_ms, None)

    def start_calibration():
        target_input = game_audio_input_var.get().strip() or "Game Audio"
        proceed = messagebox.askyesno(
            "Calibrate Audio Sync",
            f"This measures '{target_input}'s real audio latency on this machine by briefly "
            "showing a small player window and playing a short clicking sound while OBS records "
            "a ~25 second test clip.\n\nMake sure OBS is idle (not already recording something "
            "real) before continuing -- it refuses to run otherwise.\n\nContinue?",
            parent=obs_tab,
        )
        if not proceed:
            return
        calibrate_button.config(state="disabled", text="Calibrating...")
        threading.Thread(
            target=run_calibration_worker, args=(get_current_ws_config(), target_input), daemon=True,
        ).start()

    calibrate_button.config(command=start_calibration)

    mic_boost_config = obs_config.get("mic_boost", {})
    add_section_label(obs_tab, row, "Microphone Boost")
    row += 1
    mic_boost_enabled_var = tk.BooleanVar(value=mic_boost_config.get("enabled", False))
    add_checkbox(obs_tab, row, "Boost a quiet microphone live, in OBS itself", mic_boost_enabled_var)
    row += 1
    mic_boost_input_var = tk.StringVar(value=mic_boost_config.get("input_name", ""))
    add_labeled_entry(obs_tab, row, "Microphone input source name", mic_boost_input_var)

    def pick_mic_boost_input():
        open_obs_input_picker(obs_tab, get_current_ws_config, on_pick=lambda name: mic_boost_input_var.set(name))

    dark_button(obs_tab, text="Pick...", command=pick_mic_boost_input).grid(row=row, column=2, padx=(0, 10), pady=4)
    row += 1
    mic_boost_db_var = tk.StringVar(value=str(mic_boost_config.get("boost_db", 0.0)))
    add_labeled_entry(obs_tab, row, "Boost amount (dB)", mic_boost_db_var, width=10)
    row += 1
    tk.Label(
        obs_tab,
        text=(
            "    Applies a Compressor (for makeup gain that doesn't push already-loud moments as "
            "hard as quiet ones) plus a Limiter (a hard safety ceiling) directly to this OBS input "
            "-- live, so every future recording captures it boosted from the start. This is "
            "separate from the clip editor's own Track Routing gain (Settings > Clip Editor > "
            "Default Track Gains, or the dialog itself): that one only re-encodes an already-"
            "recorded FILE per trim and can't do anything about a source that was captured too "
            "quiet in the first place -- confirmed live that a genuinely under-recorded mic "
            "(e.g. -67dBFS average) can't be fixed by a flat gain at any single stage, since its "
            "existing peaks hit the digital ceiling long before the quiet parts catch up. The two "
            "can be used together."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1

    mic_noise_gate_config = mic_boost_config.get("noise_gate", {})
    mic_noise_gate_enabled_var = tk.BooleanVar(value=mic_noise_gate_config.get("enabled", False))
    add_checkbox(obs_tab, row, "Also add a noise gate ahead of the boost, to cut background noise", mic_noise_gate_enabled_var)
    row += 1
    mic_noise_gate_threshold_var = tk.StringVar(
        value=str(mic_noise_gate_config.get("threshold_db", DEFAULT_MIC_BOOST_NOISE_GATE_THRESHOLD_DB))
    )
    add_labeled_entry(obs_tab, row, "Noise gate threshold (dB)", mic_noise_gate_threshold_var, width=10)
    row += 1
    tk.Label(
        obs_tab,
        text=(
            "    Silences the mic whenever it's quieter than this threshold, before the boost "
            "above amplifies whatever's left -- lower (more negative) is more permissive, higher "
            "cuts out more. Placed first in the chain regardless of when it was added, so it "
            "gates the RAW signal rather than the already-boosted one. Freely toggleable here "
            "without losing its threshold -- unchecking it disables the filter in OBS rather "
            "than removing it, so re-checking it later remembers this value."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1

    multi_track_config = obs_config.get("multi_track_audio", {})
    # Keyed by input name so re-running Quick Setup (or hand-editing tracks afterward) never
    # loses a previously-recorded app -> process mapping; collect_config() below reads this back
    # out into multi_track_audio.app_captures for sync_multi_track_audio to re-point every
    # recording start, self-healing the Discord/Spotify/etc. drift described where it's used.
    app_captures_state = {
        ac["input_name"]: ac["process_name"]
        for ac in multi_track_config.get("app_captures", [])
        if ac.get("input_name") and ac.get("process_name")
    }
    add_section_label(obs_tab, row, "Multi-Track Audio (Advanced)")
    row += 1
    tk.Label(
        obs_tab,
        text=(
            "Routes selected inputs (mic, desktop audio, isolated game audio, etc.) to their own "
            "recording track, so you get separate audio per source for editing later. Applied only to "
            "a dedicated OBS profile below, created automatically and cloned from your current "
            "profile's recording path/quality/format -- your existing profile is never touched."
        ),
        anchor="w", justify="left", wraplength=520, bg=DARK_BG, fg=DARK_FG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6))
    row += 1
    multi_track_enabled_var = tk.BooleanVar(value=multi_track_config.get("enabled", False))
    add_checkbox(obs_tab, row, "Record selected inputs to separate audio tracks", multi_track_enabled_var)
    row += 1
    multi_track_profile_var = tk.StringVar(
        value=multi_track_config.get("profile_name", DEFAULT_MULTI_TRACK_PROFILE_NAME)
    )
    add_labeled_entry(obs_tab, row, "Dedicated OBS profile name", multi_track_profile_var)
    row += 1

    def get_current_ws_config():
        try:
            port = int(float(ws_port_var.get()))
        except ValueError:
            port = 4455
        return {"host": ws_host_var.get().strip() or "localhost", "port": port, "password": ws_password_var.get()}

    def open_quick_setup():
        def on_quick_setup_apply(tracks, app_captures):
            multi_track_clear_rows()
            for t in tracks:
                multi_track_add_row(t["input_name"], t["track"])
            multi_track_enabled_var.set(True)
            app_captures_state.update(app_captures)

        open_multi_track_quick_setup(
            obs_tab, get_current_ws_config,
            {"enabled": game_audio_enabled_var.get(), "input_name": game_audio_input_var.get().strip() or "Game Audio"},
            on_quick_setup_apply,
        )

    tk.Button(
        obs_tab, text="Quick Setup...", command=open_quick_setup, bg=DARK_ENTRY_BG, fg=DARK_FG,
        activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
    ).grid(row=row, column=0, sticky="w", padx=10, pady=(0, 6))
    row += 1

    # --- Custom Keybinds ---
    keybinds_tab = make_scrollable_tab(notebook, "Custom Keybinds")
    custom_keybind_rows, _ = build_custom_keybinds_editor(keybinds_tab, obs_config.get("custom_keybinds", []))

    multi_track_list_frame = tk.Frame(obs_tab, bg=DARK_BG)
    multi_track_list_frame.grid(row=row, column=0, columnspan=3, sticky="we")
    multi_track_rows, multi_track_add_row, multi_track_clear_rows = build_multi_track_audio_editor(
        multi_track_list_frame, multi_track_config.get("tracks", []), get_current_ws_config
    )
    row += 1

    replay_buffer_config = obs_config.get("replay_buffer", {})
    add_section_label(obs_tab, row, "Replay Buffer")
    row += 1
    current_replay_buffer_mode = get_replay_buffer_mode(replay_buffer_config)
    replay_buffer_mode_var = tk.StringVar(value=REPLAY_BUFFER_MODE_LABELS[current_replay_buffer_mode])
    tk.Label(obs_tab, text="Mode", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        obs_tab, textvariable=replay_buffer_mode_var, values=list(REPLAY_BUFFER_MODE_LABELS.values()),
        state="readonly", width=34, style="Settings.TCombobox",
    ).grid(row=row, column=1, sticky="w", pady=4)
    row += 1
    tk.Label(
        obs_tab,
        text=(
            "    \"With full recording\" keeps the buffer running alongside a normal full "
            "recording, same as before. \"Replay buffer only\" skips the full recording entirely -- "
            "nothing is saved unless you trigger Save Replay Buffer (tray menu or a custom keybind)."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1
    replay_buffer_seconds_var = tk.StringVar(
        value=str(replay_buffer_config.get("max_seconds", DEFAULT_REPLAY_BUFFER_SECONDS))
    )
    add_labeled_entry(obs_tab, row, "Replay buffer length (seconds)", replay_buffer_seconds_var, width=8)
    row += 1
    tk.Label(
        obs_tab,
        text=(
            "    Sets OBS's own \"Maximum Replay Time\" (Settings → Output → Replay Buffer) "
            "automatically. Restart OBS once after turning this on (or changing the length) -- OBS "
            "only builds the replay buffer when a profile loads, so a live change here won't take "
            "effect until then."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1

    # --- Cleanup & Guards ---
    cleanup_tab = make_scrollable_tab(notebook, "Cleanup & Guards")
    cleanup_tab.columnconfigure(1, weight=1)
    disk_guard_config = config.get("disk_space_guard", {})
    row = 0
    add_section_label(cleanup_tab, row, "Disk Space Guard")
    row += 1
    disk_guard_enabled_var = tk.BooleanVar(value=disk_guard_config.get("enabled", False))
    add_checkbox(cleanup_tab, row, "Skip starting a recording if disk space is low", disk_guard_enabled_var)
    row += 1
    disk_guard_min_gb_var = tk.StringVar(
        value=str(disk_guard_config.get("minimum_free_gb", DEFAULT_DISK_SPACE_MINIMUM_GB))
    )
    add_labeled_entry(cleanup_tab, row, "Minimum free space (GB)", disk_guard_min_gb_var)
    row += 1
    disk_guard_path_var = tk.StringVar(value=disk_guard_config.get("path", "") or "")
    add_labeled_entry(cleanup_tab, row, "Drive/folder to check (optional)", disk_guard_path_var)
    add_browse_button(cleanup_tab, row, disk_guard_path_var, mode="dir")
    row += 1

    short_clip_config = config.get("cleanup", {}).get("delete_short_clips", {})
    add_section_label(cleanup_tab, row, "Delete Short Clips")
    row += 1
    short_clip_enabled_var = tk.BooleanVar(value=short_clip_config.get("enabled", False))
    add_checkbox(cleanup_tab, row, "Delete recordings shorter than the minimum below", short_clip_enabled_var)
    row += 1
    short_clip_minimum_var = tk.StringVar(value=str(short_clip_config.get("minimum_seconds", 20)))
    add_labeled_entry(cleanup_tab, row, "Minimum session length (seconds)", short_clip_minimum_var)
    row += 1

    silent_config = config.get("cleanup", {}).get("flag_silent_recordings", {})
    add_section_label(cleanup_tab, row, "Flag Silent Recordings")
    row += 1
    silent_enabled_var = tk.BooleanVar(value=silent_config.get("enabled", False))
    add_checkbox(cleanup_tab, row, "Warn and tag recordings with no detected audio", silent_enabled_var)
    row += 1
    silent_threshold_var = tk.StringVar(value=str(silent_config.get("peak_threshold", 0.02)))
    add_labeled_entry(cleanup_tab, row, "Peak threshold (0.0 - 1.0)", silent_threshold_var)
    row += 1
    silent_warn_after_var = tk.StringVar(value=str(silent_config.get("warn_after_seconds", 30)))
    add_labeled_entry(cleanup_tab, row, "Warn after (seconds)", silent_warn_after_var)
    row += 1

    storage_config = config.get("storage_management", {})
    add_section_label(cleanup_tab, row, "Storage Management")
    row += 1
    storage_enabled_var = tk.BooleanVar(value=storage_config.get("enabled", False))
    add_checkbox(cleanup_tab, row, "Delete oldest clips once free space drops below the reserve", storage_enabled_var)
    row += 1
    storage_reserved_gb_var = tk.StringVar(
        value=str(storage_config.get("reserved_free_gb", DEFAULT_STORAGE_RESERVED_FREE_GB))
    )
    add_labeled_entry(cleanup_tab, row, "Reserve free space (GB)", storage_reserved_gb_var)
    row += 1
    storage_watch_folder_var = tk.StringVar(value=storage_config.get("watch_folder", "") or "")
    add_labeled_entry(cleanup_tab, row, "Clip folder to manage (optional, defaults to output folder)", storage_watch_folder_var)
    add_browse_button(cleanup_tab, row, storage_watch_folder_var, mode="dir")
    row += 1

    # --- Post-processing & Notifications ---
    post_tab = make_scrollable_tab(notebook, "Post-Processing")
    post_tab.columnconfigure(1, weight=1)
    transcode_config = config.get("post_record_transcode", {})
    row = 0

    # The actual controls and the "go install ffmpeg" prompt occupy the same post_tab row and
    # toggle which one's visible, rather than sitting side by side -- there's nothing useful to
    # configure here until ffmpeg actually exists somewhere on the PC, and showing a page full of
    # ffmpeg-flag jargon for a feature that can't currently run is more confusing than helpful for
    # most users. The Tk variables below are still created either way (from whatever was already
    # in config.json) so a value set before ffmpeg went missing -- or before it's installed yet --
    # is preserved on Save even while its controls are hidden.
    transcode_panel_row = row
    row += 1
    transcode_options_frame = tk.Frame(post_tab, bg=DARK_BG)
    transcode_options_frame.grid(row=transcode_panel_row, column=0, columnspan=3, sticky="we")
    transcode_options_frame.columnconfigure(1, weight=1)
    ffmpeg_missing_frame = tk.Frame(post_tab, bg=DARK_BG)
    ffmpeg_missing_frame.grid(row=transcode_panel_row, column=0, columnspan=3, sticky="we")

    opt_row = 0
    add_section_label(transcode_options_frame, opt_row, "Post-Record Transcode (ffmpeg)")
    opt_row += 1
    transcode_enabled_var = tk.BooleanVar(value=transcode_config.get("enabled", False))
    add_checkbox(transcode_options_frame, opt_row, "Run finished recordings through ffmpeg", transcode_enabled_var)
    opt_row += 1
    transcode_ffmpeg_path_var = tk.StringVar(value=transcode_config.get("ffmpeg_path", "ffmpeg"))
    add_labeled_entry(transcode_options_frame, opt_row, "ffmpeg path", transcode_ffmpeg_path_var)
    add_browse_button(transcode_options_frame, opt_row, transcode_ffmpeg_path_var)
    opt_row += 1

    def detect_ffmpeg():
        found = find_ffmpeg()
        if found:
            transcode_ffmpeg_path_var.set(found)
            messagebox.showinfo("ffmpeg found", f"Found ffmpeg at:\n{found}", parent=post_tab)
        else:
            messagebox.showwarning(
                "ffmpeg not found",
                "Couldn't find ffmpeg on this PC (checked PATH and common install locations like "
                "winget/Chocolatey/Scoop). Install it -- e.g. run `winget install ffmpeg` in a "
                "terminal, or download it from ffmpeg.org -- then try again, or use Browse... to "
                "point this field at ffmpeg.exe manually.",
                parent=post_tab,
            )

    tk.Button(
        transcode_options_frame, text="Auto-detect ffmpeg", command=detect_ffmpeg, bg=DARK_ENTRY_BG, fg=DARK_FG,
        activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
    ).grid(row=opt_row, column=0, sticky="w", padx=10, pady=(0, 6))
    opt_row += 1
    transcode_args_var = tk.StringVar(
        value=" ".join(transcode_config.get("args", ["-map", "0", "-c:v", "libx264", "-crf", "23", "-c:a", "aac"]))
    )
    add_labeled_entry(transcode_options_frame, opt_row, "ffmpeg args (space-separated)", transcode_args_var, width=44)
    opt_row += 1
    transcode_output_extension_var = tk.StringVar(value=transcode_config.get("output_extension", "") or "")
    tk.Label(transcode_options_frame, text="Output extension (optional)", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=opt_row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        transcode_options_frame, textvariable=transcode_output_extension_var,
        values=TRANSCODE_OUTPUT_EXTENSION_OPTIONS, width=16, style="Settings.TCombobox",
    ).grid(row=opt_row, column=1, sticky="w", pady=4)
    opt_row += 1
    tk.Label(
        transcode_options_frame,
        text="    Leave blank to keep the original recording's extension.",
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=opt_row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    opt_row += 1

    def use_mkv_to_mp4_preset():
        transcode_args_var.set(" ".join(MKV_TO_MP4_PRESERVE_TRACKS_ARGS))
        transcode_output_extension_var.set(MKV_TO_MP4_PRESERVE_TRACKS_EXTENSION)

    tk.Button(
        transcode_options_frame, text="Use MKV → MP4 preset (preserve every audio track)",
        command=use_mkv_to_mp4_preset, bg=DARK_ENTRY_BG, fg=DARK_FG,
        activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
    ).grid(row=opt_row, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 4))
    opt_row += 1
    tk.Label(
        transcode_options_frame,
        text=(
            "    A pure remux (-map 0 -c copy): every audio track carried over byte-for-byte, no "
            "re-encoding, no quality loss -- just repackaged into MP4. Overwrites the ffmpeg args "
            "and output extension above; leave the suffix/delete-original settings as you like."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=opt_row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    opt_row += 1
    transcode_suffix_var = tk.StringVar(value=transcode_config.get("suffix", "_compressed"))
    add_labeled_entry(transcode_options_frame, opt_row, "Output filename suffix", transcode_suffix_var)
    opt_row += 1
    transcode_delete_original_var = tk.BooleanVar(value=transcode_config.get("delete_original", False))
    add_checkbox(
        transcode_options_frame, opt_row, "Delete original after a successful transcode",
        transcode_delete_original_var,
    )
    opt_row += 1

    # --- Audio sync shift: independent of the transcode above, its own ffmpeg pass fixing a
    # recording made before DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS (or a live calibration)
    # was applied at record time, whose isolated audio track(s) still sound a few ms out of sync
    # with the rest -- see build_audio_track_filter_args.
    audio_sync_shift_config = config.get("audio_sync_shift", {})
    add_section_label(transcode_options_frame, opt_row, "Audio Sync Shift (ffmpeg)")
    opt_row += 1
    audio_sync_shift_enabled_var = tk.BooleanVar(value=audio_sync_shift_config.get("enabled", False))
    add_checkbox(
        transcode_options_frame, opt_row, "Shift track(s) to fix out-of-sync audio on every finished recording",
        audio_sync_shift_enabled_var,
    )
    opt_row += 1
    audio_sync_shift_tracks_var = tk.StringVar(
        value=",".join(str(t) for t in audio_sync_shift_config.get("tracks", []))
    )
    add_labeled_entry(transcode_options_frame, opt_row, "Track(s) to shift (comma-separated)", audio_sync_shift_tracks_var, width=10)
    opt_row += 1
    audio_sync_shift_ms_var = tk.StringVar(value=str(audio_sync_shift_config.get("shift_ms") or ""))
    add_labeled_entry(transcode_options_frame, opt_row, "Shift by (ms)", audio_sync_shift_ms_var, width=10)
    opt_row += 1
    tk.Label(
        transcode_options_frame,
        text=(
            "    Track numbers match OBS's own track numbering (1-6). Positive ms delays those "
            "tracks, negative advances them. Video is always kept as-is; only audio is "
            "re-encoded. Runs as its own pass on every finished recording, independent of the "
            "transcode above -- use the clip editor's own \"Fix audio sync\" option instead for a "
            "one-off fix on a single existing clip."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=opt_row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    opt_row += 1
    audio_sync_shift_suffix_var = tk.StringVar(value=audio_sync_shift_config.get("suffix", "_synced"))
    add_labeled_entry(transcode_options_frame, opt_row, "Output filename suffix", audio_sync_shift_suffix_var)
    opt_row += 1
    audio_sync_shift_delete_original_var = tk.BooleanVar(value=audio_sync_shift_config.get("delete_original", False))
    add_checkbox(
        transcode_options_frame, opt_row, "Delete original after a successful audio sync shift",
        audio_sync_shift_delete_original_var,
    )
    opt_row += 1

    add_section_label(ffmpeg_missing_frame, 0, "Post-Record Transcode (ffmpeg)")
    tk.Label(
        ffmpeg_missing_frame,
        text=(
            "ffmpeg isn't installed, so post-record transcoding (e.g. converting MKV recordings to "
            "MP4) isn't available yet. Install it below, then this panel switches to the full "
            "options automatically -- no need to reopen Settings."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=1, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))

    def reveal_transcode_options_if_ffmpeg_found():
        if resolve_ffmpeg_path(transcode_ffmpeg_path_var.get()):
            ffmpeg_missing_frame.grid_remove()
            transcode_options_frame.grid()
            return True
        return False

    def install_ffmpeg_via_winget():
        winget_install_button.config(state="disabled", text="Installing ffmpeg...")

        def worker():
            result = winget_install_ffmpeg()

            def finish():
                success, reason = result
                if not (success and reveal_transcode_options_if_ffmpeg_found()):
                    winget_install_button.config(state="normal", text="Install ffmpeg via winget")
                    messagebox.showwarning(
                        "Install didn't finish",
                        f"Couldn't install ffmpeg automatically ({reason or 'still not found after install'}). "
                        "Try again, or use the manual download link instead.",
                        parent=post_tab,
                    )

            post_tab.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    if has_winget():
        winget_install_button = tk.Button(
            ffmpeg_missing_frame, text="Install ffmpeg via winget", command=install_ffmpeg_via_winget,
            bg=DARK_ENTRY_BG, fg=DARK_FG, activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
        )
        winget_install_button.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 4))

    ffmpeg_download_link = tk.Label(
        ffmpeg_missing_frame, text="Or download it manually from ffmpeg.org ↗", fg="#5b9dff", bg=DARK_BG,
        font=("Segoe UI", 9, "underline"), cursor="hand2",
    )
    ffmpeg_download_link.grid(row=3, column=0, sticky="w", padx=10, pady=(0, 8))
    ffmpeg_download_link.bind("<Button-1>", lambda _event: webbrowser.open(FFMPEG_DOWNLOAD_URL))

    if resolve_ffmpeg_path(transcode_ffmpeg_path_var.get()):
        ffmpeg_missing_frame.grid_remove()
    else:
        transcode_options_frame.grid_remove()

    add_section_label(post_tab, row, "Notifications")
    row += 1
    notifications_enabled_var = tk.BooleanVar(value=config.get("notifications", {}).get("enabled", False))
    add_checkbox(post_tab, row, "Show desktop notifications for key events", notifications_enabled_var)
    row += 1

    # --- Clip Editor ---
    clip_editor_tab = make_scrollable_tab(notebook, "Clip Editor")
    clip_editor_tab.columnconfigure(1, weight=1)
    clip_editor_config = config.get("clip_editor", {})
    row = 0
    add_section_label(clip_editor_tab, row, "Trimmed Clip Output")
    row += 1
    clip_output_folder_var = tk.StringVar(value=clip_editor_config.get("output_folder", "") or "")
    add_labeled_entry(clip_editor_tab, row, "Output folder (optional)", clip_output_folder_var)
    add_browse_button(clip_editor_tab, row, clip_output_folder_var, mode="dir")
    row += 1
    tk.Label(
        clip_editor_tab,
        text="    Leave blank to save trimmed clips in the same folder as the source recording.",
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1
    clip_output_suffix_var = tk.StringVar(value=clip_editor_config.get("output_suffix", "_trimmed"))
    add_labeled_entry(clip_editor_tab, row, "Output filename suffix", clip_output_suffix_var, width=16)
    row += 1
    clip_delete_original_var = tk.BooleanVar(value=clip_editor_config.get("delete_original_after_trim", False))
    add_checkbox(
        clip_editor_tab, row, "Delete the original recording after a successful trim",
        clip_delete_original_var,
    )
    row += 1
    clip_auto_fix_sync_var = tk.BooleanVar(value=clip_editor_config.get("auto_fix_audio_sync", False))
    add_checkbox(
        clip_editor_tab, row, "Automatically check \"Fix audio track sync\" when opening a clip to trim",
        clip_auto_fix_sync_var,
    )
    row += 1
    configured_trim_mode = clip_editor_config.get("trim_mode", "precise")
    if configured_trim_mode not in CLIP_EDITOR_TRIM_MODE_OPTIONS:
        configured_trim_mode = "precise"
    trim_mode_var = tk.StringVar(value=CLIP_EDITOR_TRIM_MODE_LABELS[configured_trim_mode])
    tk.Label(clip_editor_tab, text="Trim mode", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        clip_editor_tab, textvariable=trim_mode_var, values=list(CLIP_EDITOR_TRIM_MODE_LABELS.values()),
        state="readonly", width=36, style="Settings.TCombobox",
    ).grid(row=row, column=1, sticky="w", pady=4)
    row += 1
    tk.Label(
        clip_editor_tab,
        text=(
            "    Precise re-encodes, so it's slower, but the trimmed clip always starts cleanly. "
            "Fast is a lossless, near-instant stream copy, but can leave a broken leading frame "
            "at the cut point on some recordings -- confirmed to show up as a glitch in strict "
            "players (VLC) even when more forgiving ones (Premiere) hide it. Most people want "
            "Precise; Fast is here for when export speed genuinely matters more than that risk."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1
    clip_target_size_var = tk.StringVar(value=str(clip_editor_config.get("default_target_size_mb") or ""))
    add_labeled_entry(clip_editor_tab, row, "Default target size (MB, blank = off)", clip_target_size_var, width=10)
    row += 1
    tk.Label(
        clip_editor_tab,
        text=(
            "    Pre-fills and pre-checks the editor's own \"Limit size to\" option with this "
            "value (e.g. to fit a specific upload limit) -- still freely changeable per trim."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1
    configured_preview_quality = clip_editor_config.get("preview_quality", CLIP_EDITOR_DEFAULT_PREVIEW_QUALITY)
    if configured_preview_quality not in CLIP_EDITOR_PREVIEW_QUALITY_OPTIONS:
        configured_preview_quality = CLIP_EDITOR_DEFAULT_PREVIEW_QUALITY
    clip_preview_quality_var = tk.StringVar(value=configured_preview_quality)
    tk.Label(clip_editor_tab, text="Default preview quality", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        clip_editor_tab, textvariable=clip_preview_quality_var, values=CLIP_EDITOR_PREVIEW_QUALITY_OPTIONS,
        state="readonly", width=28, style="Settings.TCombobox",
    ).grid(row=row, column=1, sticky="w", pady=4)
    row += 1
    tk.Label(
        clip_editor_tab,
        text=(
            "    Only affects the editor's own preview playback -- never the trimmed output, "
            "which is always encoded separately by ffmpeg regardless of this. Lower settings "
            "trade decode quality for smoother playback if the preview is stuttering; also "
            "changeable per session from the editor's own \"Preview\" dropdown."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1
    configured_default_format = clip_editor_config.get("default_output_format", CLIP_EDITOR_OUTPUT_FORMATS[0])
    if configured_default_format not in CLIP_EDITOR_OUTPUT_FORMATS:
        configured_default_format = CLIP_EDITOR_OUTPUT_FORMATS[0]
    clip_default_format_var = tk.StringVar(value=configured_default_format)
    tk.Label(clip_editor_tab, text="Default export format", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        clip_editor_tab, textvariable=clip_default_format_var, values=CLIP_EDITOR_OUTPUT_FORMATS,
        state="readonly", width=16, style="Settings.TCombobox",
    ).grid(row=row, column=1, sticky="w", pady=4)
    row += 1
    tk.Label(
        clip_editor_tab,
        text=(
            "    Trims default to this container -- e.g. record in MKV but always want trimmed "
            "clips as MP4 without transcoding the whole recording first. The editor's own "
            "dropdown can still override this per trim."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1
    configured_default_quality = clip_editor_config.get("default_quality", CLIP_EDITOR_QUALITY_OPTIONS[0])
    if configured_default_quality not in CLIP_EDITOR_QUALITY_OPTIONS:
        configured_default_quality = CLIP_EDITOR_QUALITY_OPTIONS[0]
    clip_default_quality_var = tk.StringVar(value=configured_default_quality)
    tk.Label(clip_editor_tab, text="Default resolution", anchor="w", bg=DARK_BG, fg=DARK_FG).grid(
        row=row, column=0, sticky="w", padx=(10, 6), pady=4
    )
    ttk.Combobox(
        clip_editor_tab, textvariable=clip_default_quality_var, values=CLIP_EDITOR_QUALITY_OPTIONS,
        state="readonly", width=16, style="Settings.TCombobox",
    ).grid(row=row, column=1, sticky="w", pady=4)
    row += 1
    tk.Label(
        clip_editor_tab,
        text=(
            "    \"Same as source\" keeps the fast, lossless stream-copy trim whenever possible. "
            "Downscaling forces ffmpeg to re-encode (slower, smaller file) even when \"Precise\" "
            "isn't checked. The editor's own dropdown can still override this per trim."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1

    add_section_label(clip_editor_tab, row, "Default Track Gains (dB)")
    row += 1
    tk.Label(
        clip_editor_tab,
        text=(
            "    Pre-fills the Track Routing dialog's own per-track gain boxes with these "
            "values whenever it's opened -- still freely changeable (or clearable) per trim from "
            "there. Boosts (positive) or attenuates (negative) a source only when it's routed "
            "into that one output track (the row); the same source can have a different default "
            "-- or none -- on each output it's also routed to. Leave blank or 0 for no default. "
            "This changes the exported FILE itself (via ffmpeg) once a trim actually runs -- it's "
            "unrelated to the clip editor's own \"Preview\" volume slider, which only ever affects "
            "what you hear while scrubbing a clip, never the exported output."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=row, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    row += 1

    # A fixed 6x6 grid rather than one sized to any particular recording's real track count --
    # Settings has no specific file open to probe, and 6 is OBS's own hard cap on simultaneous
    # audio tracks (see e.g. CALIBRATION_TARGET_TRACK and the multi_track_audio tab's own track
    # dropdown above, both hardcoded to range(1, 7) for the same reason). Keyed by track NUMBER,
    # same convention as track_name_hints/multi_track_audio.tracks -- this app's own multi-track
    # routing assigns each source to a fixed, stable track number, so a number-keyed default
    # reliably means the same source every time, unlike e.g. a source's on-disk stream index.
    DEFAULT_GAIN_GRID_TRACKS = 6
    default_gains_config = clip_editor_config.get("default_gains_db") or {}
    gain_track_hints = track_name_hints(obs_config)

    def default_gain_text(dest, src):
        value = default_gains_config.get(str(dest), {}).get(str(src))
        return "" if not value else str(value)

    default_gain_vars = {
        dest: {
            src: tk.StringVar(value=default_gain_text(dest, src))
            for src in range(1, DEFAULT_GAIN_GRID_TRACKS + 1)
        }
        for dest in range(1, DEFAULT_GAIN_GRID_TRACKS + 1)
    }
    gain_header_row = row
    for src in range(1, DEFAULT_GAIN_GRID_TRACKS + 1):
        hint = gain_track_hints.get(src)
        header_text = f"{src}\n({hint})" if hint else str(src)
        tk.Label(
            clip_editor_tab, text=header_text, bg=DARK_BG, fg=DARK_FG, justify="center",
            font=("Segoe UI", 8),
        ).grid(row=gain_header_row, column=src, padx=2, pady=(0, 2))
    row += 1
    for dest in range(1, DEFAULT_GAIN_GRID_TRACKS + 1):
        tk.Label(clip_editor_tab, text=f"-> Track {dest}:", bg=DARK_BG, fg=DARK_FG, anchor="w").grid(
            row=row, column=0, sticky="w", padx=(10, 4), pady=1
        )
        for src in range(1, DEFAULT_GAIN_GRID_TRACKS + 1):
            tk.Entry(
                clip_editor_tab, textvariable=default_gain_vars[dest][src], width=4,
                bg=DARK_ENTRY_BG, fg=DARK_FG, insertbackground=DARK_FG, justify="center",
            ).grid(row=row, column=src, padx=1, pady=1)
        row += 1

    add_section_label(clip_editor_tab, row, "Video Preview (VLC)")
    row += 1

    # Same found/missing toggle-panel pattern as Post-Processing's ffmpeg section above: VLC
    # detection only gates the VLC-specific controls, not the whole tab, since output folder and
    # delete-original are meaningful to configure even before VLC is installed.
    vlc_panel_row = row
    row += 1
    vlc_found_frame = tk.Frame(clip_editor_tab, bg=DARK_BG)
    vlc_found_frame.grid(row=vlc_panel_row, column=0, columnspan=3, sticky="we")
    vlc_missing_frame = tk.Frame(clip_editor_tab, bg=DARK_BG)
    vlc_missing_frame.grid(row=vlc_panel_row, column=0, columnspan=3, sticky="we")

    tk.Label(
        vlc_found_frame,
        text="VLC install found. Override its location only if you have more than one installed:",
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    clip_vlc_path_var = tk.StringVar(value=clip_editor_config.get("vlc_path", "") or "")
    add_labeled_entry(vlc_found_frame, 1, "VLC install folder (optional)", clip_vlc_path_var)
    add_browse_button(vlc_found_frame, 1, clip_vlc_path_var, mode="dir")

    tk.Label(
        vlc_missing_frame,
        text=(
            "VLC isn't installed, so the clip editor's video preview won't be available yet. "
            "Install it below -- this panel switches over automatically once it's found, no need "
            "to reopen Settings."
        ),
        anchor="w", justify="left", wraplength=520, fg=DARK_MUTED_FG, bg=DARK_BG,
    ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))

    def reveal_vlc_options_if_found():
        if resolve_vlc_path(clip_vlc_path_var.get()):
            vlc_missing_frame.grid_remove()
            vlc_found_frame.grid()
            return True
        return False

    def install_vlc_via_winget():
        winget_install_vlc_button.config(state="disabled", text="Installing VLC...")

        def worker():
            result = winget_install_vlc()

            def finish():
                success, reason = result
                if not (success and reveal_vlc_options_if_found()):
                    winget_install_vlc_button.config(state="normal", text="Install VLC via winget")
                    messagebox.showwarning(
                        "Install didn't finish",
                        f"Couldn't install VLC automatically ({reason or 'still not found after install'}). "
                        "Try again, or use the manual download link instead.",
                        parent=clip_editor_tab,
                    )

            clip_editor_tab.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    if has_winget():
        winget_install_vlc_button = tk.Button(
            vlc_missing_frame, text="Install VLC via winget", command=install_vlc_via_winget,
            bg=DARK_ENTRY_BG, fg=DARK_FG, activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
        )
        winget_install_vlc_button.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 4))

    vlc_download_link = tk.Label(
        vlc_missing_frame, text="Or download it manually from videolan.org ↗", fg="#5b9dff", bg=DARK_BG,
        font=("Segoe UI", 9, "underline"), cursor="hand2",
    )
    vlc_download_link.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 8))
    vlc_download_link.bind("<Button-1>", lambda _event: webbrowser.open(VLC_DOWNLOAD_URL))

    if resolve_vlc_path(clip_vlc_path_var.get()):
        vlc_missing_frame.grid_remove()
    else:
        vlc_found_frame.grid_remove()

    # The selected tab shows its full label; every other tab shrinks to an abbreviation so all
    # eight can still fit across the tab strip without the notebook needing to be much wider.
    # ttk.Notebook doesn't do this on its own -- it just re-measures each tab's width from
    # whatever text is currently set, so swapping text on <<NotebookTabChanged>> is what actually
    # drives the resize.
    TAB_SHORT_LABELS = {
        "General": "Gen",
        "Watched Games": "Games",
        "Launchers": "Launch",
        "OBS": "OBS",
        "Custom Keybinds": "Keybinds",
        "Cleanup & Guards": "Cleanup",
        "Post-Processing": "Post-Proc",
        "Clip Editor": "Clip",
    }
    tab_full_titles = {tab_id: notebook.tab(tab_id, "text") for tab_id in notebook.tabs()}

    def update_tab_labels(_event=None):
        selected = notebook.select()
        for tab_id in notebook.tabs():
            full = tab_full_titles[tab_id]
            notebook.tab(tab_id, text=full if tab_id == selected else TAB_SHORT_LABELS.get(full, full))

    notebook.bind("<<NotebookTabChanged>>", update_tab_labels)
    update_tab_labels()

    # --- Save / Cancel ---
    status_label = tk.Label(root, text="", fg="#ef4444", bg=DARK_BG, anchor="w")
    status_label.pack(fill="x", padx=10)

    def collect_config():
        errors = []

        def read_float(var, field_name, default):
            try:
                return float(var.get())
            except ValueError:
                errors.append(f"'{field_name}' must be a number")
                return default

        def read_int(var, field_name, default):
            try:
                return int(float(var.get()))
            except ValueError:
                errors.append(f"'{field_name}' must be a whole number")
                return default

        new_config = json.loads(json.dumps(config))

        overlay = new_config.setdefault("overlay", {})
        chosen_monitor_label = default_monitor_var.get()
        if chosen_monitor_label and chosen_monitor_label != "Off":
            overlay["default_monitor_label"] = chosen_monitor_label
        else:
            overlay.pop("default_monitor_label", None)
        if start_with_overlay_var.get():
            overlay["start_with_overlay"] = True
        else:
            overlay.pop("start_with_overlay", None)

        new_config["watched_games"] = list(games_listbox.get(0, "end"))
        new_config["watched_windows"] = []
        for row_vars in window_rows:
            process = row_vars["process"].get().strip()
            title = row_vars["title"].get().strip()
            if not process or not title:
                continue
            entry = {"process_name": process, "title_contains": title}
            display = row_vars["display"].get().strip()
            if display:
                entry["display_name"] = display
            new_config["watched_windows"].append(entry)

        new_config["poll_interval_seconds"] = read_float(
            poll_interval_var, "Poll interval", new_config.get("poll_interval_seconds", 1.5)
        )
        new_config["organize_into_game_subfolders"] = subfolders_var.get()
        new_config["log_file"] = log_file_var.get().strip() or None

        steam = new_config.setdefault("steam", {})
        steam["enabled"] = steam_vars["enabled"].get()
        steam["allowed_drives"] = parse_csv_field(steam_drives_var.get())
        steam["exclude_keywords"] = parse_csv_field(steam_vars["exclude_keywords"].get())

        epic = new_config.setdefault("epic", {})
        epic["enabled"] = epic_vars["enabled"].get()
        epic["exclude_keywords"] = parse_csv_field(epic_vars["exclude_keywords"].get())

        gog = new_config.setdefault("gog", {})
        gog["enabled"] = gog_vars["enabled"].get()
        gog["exclude_keywords"] = parse_csv_field(gog_vars["exclude_keywords"].get())

        xbox = new_config.setdefault("xbox", {})
        xbox["enabled"] = xbox_vars["enabled"].get()
        xbox["install_dirs"] = parse_csv_field(xbox_vars["install_dirs"].get())
        xbox["exclude_keywords"] = parse_csv_field(xbox_vars["exclude_keywords"].get())

        battlenet = new_config.setdefault("battlenet", {})
        battlenet["enabled"] = battlenet_vars["enabled"].get()
        battlenet["install_dirs"] = parse_csv_field(battlenet_vars["install_dirs"].get())
        battlenet["exclude_keywords"] = parse_csv_field(battlenet_vars["exclude_keywords"].get())

        obs = new_config.setdefault("obs", {})
        obs["process_name"] = obs_process_var.get().strip() or obs.get("process_name", "obs64.exe")
        obs["path"] = obs_path_var.get().strip()
        obs["launch_args"] = parse_csv_field(obs_launch_args_var.get())
        obs["startup_wait_seconds"] = read_int(
            obs_startup_wait_var, "Startup wait", obs.get("startup_wait_seconds", 8)
        )
        output_folder_value = output_folder_var.get().strip()
        if output_folder_value:
            obs["output_folder"] = output_folder_value
        else:
            obs.pop("output_folder", None)

        recording_format_value = recording_format_var.get().strip()
        if recording_format_value:
            obs["recording_format"] = recording_format_value
        else:
            obs.pop("recording_format", None)
        # Always written, even when left on "Match canvas" -- that's a real, active choice (set
        # output = canvas resolution) rather than a "leave OBS's own value untouched" sentinel,
        # unlike recording_format's blank-Entry default. Once Settings has been saved once, this
        # is honored on every subsequent launch, same as every other field in this dialog.
        obs["recording_resolution"] = recording_resolution_var.get()

        ws = obs.setdefault("websocket", {})
        ws["host"] = ws_host_var.get().strip() or "localhost"
        ws["port"] = read_int(ws_port_var, "WebSocket port", ws.get("port", 4455))
        ws["password"] = ws_password_var.get()

        auto_split = obs.setdefault("auto_split", {})
        auto_split["enabled"] = auto_split_enabled_var.get()
        auto_split["by"] = auto_split_by_var.get()
        auto_split["minutes"] = read_int(auto_split_minutes_var, "Split minutes", auto_split.get("minutes", 20))
        auto_split["tolerance_seconds"] = read_int(
            auto_split_tolerance_seconds_var, "Split tolerance seconds", auto_split.get("tolerance_seconds", 8)
        )
        auto_split["megabytes"] = read_int(
            auto_split_megabytes_var, "Split megabytes", auto_split.get("megabytes", 4096)
        )
        auto_split["tolerance_megabytes"] = read_int(
            auto_split_tolerance_megabytes_var, "Split tolerance megabytes", auto_split.get("tolerance_megabytes", 50)
        )

        manual_split = obs.setdefault("manual_split", {})
        manual_split["enabled"] = manual_split_enabled_var.get()
        manual_split["buffer_seconds"] = read_int(
            manual_split_buffer_var, "Manual split buffer seconds", manual_split.get("buffer_seconds", 0)
        )

        custom_keybinds = []
        for row_vars in custom_keybind_rows:
            modifiers = [
                name for name, var in (
                    ("ctrl", row_vars["ctrl"]), ("alt", row_vars["alt"]),
                    ("shift", row_vars["shift"]), ("win", row_vars["win"]),
                )
                if var.get()
            ]
            action = CUSTOM_KEYBIND_ACTIONS_BY_LABEL.get(row_vars["action_label"].get(), "split_record_file")
            custom_keybinds.append({
                "enabled": row_vars["enabled"].get(),
                "action": action,
                "modifiers": modifiers,
                "key": row_vars["key"].get(),
            })
        obs["custom_keybinds"] = custom_keybinds

        recovery = obs.setdefault("recovery", {})
        recovery["memory_limit_gb"] = read_float(
            memory_limit_var, "Memory limit", recovery.get("memory_limit_gb", DEFAULT_OBS_MEMORY_LIMIT_GB)
        )
        recovery["cooldown_seconds"] = read_int(
            recovery_cooldown_var,
            "Recovery cooldown",
            recovery.get("cooldown_seconds", DEFAULT_OBS_RECOVERY_COOLDOWN_SECONDS),
        )

        game_audio = obs.setdefault("game_audio_capture", {})
        game_audio["enabled"] = game_audio_enabled_var.get()
        game_audio["input_name"] = game_audio_input_var.get().strip() or "Game Audio"
        obs["process_audio_capture_sync_offset_ms"] = read_int(
            process_capture_sync_offset_var, "Audio capture sync offset", DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS,
        )

        mic_boost = obs.setdefault("mic_boost", {})
        mic_boost["enabled"] = mic_boost_enabled_var.get()
        mic_boost["input_name"] = mic_boost_input_var.get().strip()
        mic_boost["boost_db"] = read_float(mic_boost_db_var, "Microphone boost amount", mic_boost.get("boost_db", 0.0))
        if mic_boost_enabled_var.get() and not mic_boost["input_name"]:
            errors.append("\"Microphone input source name\" is required when Microphone Boost is enabled")
        mic_noise_gate = mic_boost.setdefault("noise_gate", {})
        mic_noise_gate["enabled"] = mic_noise_gate_enabled_var.get()
        mic_noise_gate["threshold_db"] = read_float(
            mic_noise_gate_threshold_var, "Noise gate threshold",
            mic_noise_gate.get("threshold_db", DEFAULT_MIC_BOOST_NOISE_GATE_THRESHOLD_DB),
        )

        multi_track_audio = obs.setdefault("multi_track_audio", {})
        multi_track_audio["enabled"] = multi_track_enabled_var.get()
        multi_track_audio["profile_name"] = (
            multi_track_profile_var.get().strip() or DEFAULT_MULTI_TRACK_PROFILE_NAME
        )
        multi_track_tracks = []
        for row_vars in multi_track_rows:
            name = row_vars["input_name"].get().strip()
            if not name:
                continue
            try:
                track_num = int(row_vars["track"].get())
            except ValueError:
                continue
            multi_track_tracks.append({"input_name": name, "track": track_num})
        multi_track_audio["tracks"] = multi_track_tracks
        multi_track_audio["app_captures"] = [
            {"input_name": name, "process_name": process_name}
            for name, process_name in app_captures_state.items()
        ]

        replay_buffer = obs.setdefault("replay_buffer", {})
        replay_buffer["mode"] = REPLAY_BUFFER_MODE_LABELS_BY_LABEL.get(replay_buffer_mode_var.get(), "off")
        replay_buffer.pop("enabled", None)  # superseded by "mode" -- drop so it can't contradict it
        replay_buffer["max_seconds"] = read_int(
            replay_buffer_seconds_var, "Replay buffer length (seconds)", DEFAULT_REPLAY_BUFFER_SECONDS
        )

        disk_guard = new_config.setdefault("disk_space_guard", {})
        disk_guard["enabled"] = disk_guard_enabled_var.get()
        disk_guard["minimum_free_gb"] = read_float(
            disk_guard_min_gb_var,
            "Minimum free space",
            disk_guard.get("minimum_free_gb", DEFAULT_DISK_SPACE_MINIMUM_GB),
        )
        disk_guard_path_value = disk_guard_path_var.get().strip()
        if disk_guard_path_value:
            disk_guard["path"] = disk_guard_path_value
        else:
            disk_guard.pop("path", None)

        cleanup = new_config.setdefault("cleanup", {})
        short_clip = cleanup.setdefault("delete_short_clips", {})
        short_clip["enabled"] = short_clip_enabled_var.get()
        short_clip["minimum_seconds"] = read_int(
            short_clip_minimum_var, "Short clip minimum seconds", short_clip.get("minimum_seconds", 20)
        )

        silent = cleanup.setdefault("flag_silent_recordings", {})
        silent["enabled"] = silent_enabled_var.get()
        silent["peak_threshold"] = read_float(
            silent_threshold_var, "Silent peak threshold", silent.get("peak_threshold", 0.02)
        )
        silent["warn_after_seconds"] = read_int(
            silent_warn_after_var, "Silent warn-after seconds", silent.get("warn_after_seconds", 30)
        )

        storage = new_config.setdefault("storage_management", {})
        storage["enabled"] = storage_enabled_var.get()
        storage["reserved_free_gb"] = read_float(
            storage_reserved_gb_var,
            "Reserve free space",
            storage.get("reserved_free_gb", DEFAULT_STORAGE_RESERVED_FREE_GB),
        )
        storage_watch_folder_value = storage_watch_folder_var.get().strip()
        if storage_watch_folder_value:
            storage["watch_folder"] = storage_watch_folder_value
        else:
            storage.pop("watch_folder", None)

        transcode = new_config.setdefault("post_record_transcode", {})
        transcode["enabled"] = transcode_enabled_var.get()
        transcode["ffmpeg_path"] = transcode_ffmpeg_path_var.get().strip() or "ffmpeg"
        transcode["args"] = transcode_args_var.get().split()
        output_extension_value = transcode_output_extension_var.get().strip()
        if output_extension_value:
            transcode["output_extension"] = output_extension_value
        else:
            transcode.pop("output_extension", None)
        transcode["suffix"] = transcode_suffix_var.get()
        transcode["delete_original"] = transcode_delete_original_var.get()

        audio_sync_shift = new_config.setdefault("audio_sync_shift", {})
        audio_sync_shift["enabled"] = audio_sync_shift_enabled_var.get()
        # Shares the transcode's own ffmpeg path rather than asking the user to configure/find
        # ffmpeg a second time for a feature that lives in this same panel.
        audio_sync_shift["ffmpeg_path"] = transcode["ffmpeg_path"]
        audio_sync_shift["tracks"] = parse_track_numbers(audio_sync_shift_tracks_var.get())
        if audio_sync_shift_ms_var.get().strip():
            shift_ms_value = read_int(audio_sync_shift_ms_var, "Audio sync shift", None)
            if shift_ms_value is not None:
                audio_sync_shift["shift_ms"] = shift_ms_value
        else:
            audio_sync_shift.pop("shift_ms", None)
        audio_sync_shift["suffix"] = audio_sync_shift_suffix_var.get()
        audio_sync_shift["delete_original"] = audio_sync_shift_delete_original_var.get()

        notifications = new_config.setdefault("notifications", {})
        notifications["enabled"] = notifications_enabled_var.get()

        clip_editor = new_config.setdefault("clip_editor", {})
        clip_output_folder_value = clip_output_folder_var.get().strip()
        if clip_output_folder_value:
            clip_editor["output_folder"] = clip_output_folder_value
        else:
            clip_editor.pop("output_folder", None)
        clip_editor["output_suffix"] = clip_output_suffix_var.get()
        clip_editor["delete_original_after_trim"] = clip_delete_original_var.get()
        clip_editor["auto_fix_audio_sync"] = clip_auto_fix_sync_var.get()
        default_gains_out = {}
        for gain_dest, gain_sources in default_gain_vars.items():
            dest_gains_out = {}
            for gain_src, gain_var in gain_sources.items():
                text = gain_var.get().strip()
                if not text:
                    continue
                try:
                    value = float(text)
                except ValueError:
                    errors.append(f"Default Track Gains: 'Track {gain_dest}' column {gain_src} must be a number")
                    continue
                if value:
                    dest_gains_out[str(gain_src)] = value
            if dest_gains_out:
                default_gains_out[str(gain_dest)] = dest_gains_out
        if default_gains_out:
            clip_editor["default_gains_db"] = default_gains_out
        else:
            clip_editor.pop("default_gains_db", None)
        clip_editor["trim_mode"] = CLIP_EDITOR_TRIM_MODE_LABELS_BY_LABEL.get(trim_mode_var.get(), "precise")
        if clip_target_size_var.get().strip():
            target_size_value = read_float(clip_target_size_var, "Default target size", None)
            if target_size_value is not None and target_size_value > 0:
                clip_editor["default_target_size_mb"] = target_size_value
            elif target_size_value is not None:
                errors.append("'Default target size' must be a positive number of MB")
        else:
            clip_editor.pop("default_target_size_mb", None)
        clip_editor["preview_quality"] = clip_preview_quality_var.get()
        clip_editor["default_output_format"] = clip_default_format_var.get()
        clip_editor["default_quality"] = clip_default_quality_var.get()
        clip_vlc_path_value = clip_vlc_path_var.get().strip()
        if clip_vlc_path_value:
            clip_editor["vlc_path"] = clip_vlc_path_value
        else:
            clip_editor.pop("vlc_path", None)

        return new_config, errors

    def do_save(and_restart):
        new_config, errors = collect_config()
        if errors:
            status_label.config(text="; ".join(errors))
            return
        if not new_config.get("obs", {}).get("path"):
            status_label.config(text="'OBS executable path' is required")
            return

        # Auto-calibrate the moment Game Audio Isolation is actually turned on (not on every
        # save -- only the OFF -> ON transition), so the process-capture latency is already
        # corrected the first time it would otherwise cause an audible doubling,
        # without the user needing to know this measurement exists at all, let alone run it by
        # hand. Synchronous (blocks the Settings window for ~25s) rather than a background
        # thread deliberately: "Save and Restart" kills this process right after do_save
        # returns, which would kill an async calibration thread mid-run before it could finish.
        game_audio_just_enabled = (
            new_config.get("obs", {}).get("game_audio_capture", {}).get("enabled")
            and not config.get("obs", {}).get("game_audio_capture", {}).get("enabled")
        )
        if game_audio_just_enabled:
            status_label.config(
                fg="#f5a623",
                text="Calibrating audio sync for Game Audio -- ~25s, plays a brief test sound...",
            )
            root.update_idletasks()
            target_input = new_config["obs"]["game_audio_capture"].get("input_name") or "Game Audio"
            client = connect_obs(get_current_ws_config(), retries=1, delay=0)
            result_ms = None
            if client:
                ffmpeg_path = resolve_ffmpeg_path("ffmpeg")
                if ffmpeg_path:
                    try:
                        result_ms = run_audio_sync_calibration(client, ffmpeg_path, target_input)
                    finally:
                        client.disconnect()
                else:
                    logging.warning("Could not auto-calibrate audio sync offset: ffmpeg isn't installed.")
            else:
                logging.warning("Could not auto-calibrate audio sync offset: OBS is not reachable.")
            if result_ms is not None:
                new_config["obs"]["process_audio_capture_sync_offset_ms"] = result_ms
                process_capture_sync_offset_var.set(str(result_ms))
                logging.info(
                    "Auto-calibrated audio sync offset to %sms after enabling Game Audio Isolation.", result_ms,
                )
            else:
                logging.warning(
                    "Could not auto-calibrate audio sync offset after enabling Game Audio Isolation; "
                    "using the configured default. Use \"Calibrate Audio Sync...\" to retry.",
                )
            status_label.config(fg="#ef4444", text="")

        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(new_config, f, indent=4)
        except OSError as exc:
            messagebox.showerror("Save failed", f"Could not write config.json:\n{exc}", parent=root)
            return
        if is_frozen:
            set_startup_shortcut_enabled(startup_var.get())
        logging.info("config.json updated via the Settings editor.")
        if and_restart:
            root.destroy()
            restart_callback()
        else:
            messagebox.showinfo(
                "Settings saved",
                "Settings saved. A restart may be necessary for some settings to take effect.",
                parent=root,
            )
            root.destroy()

    def dark_settings_button(**kwargs):
        return tk.Button(
            button_bar, bg=DARK_ENTRY_BG, fg=DARK_FG, activebackground=DARK_ENTRY_BG, activeforeground=DARK_FG,
            **kwargs
        )

    button_bar = tk.Frame(root, bg=DARK_BG)
    button_bar.pack(fill="x", padx=10, pady=10)
    dark_settings_button(text="Cancel", command=root.destroy).pack(side="right")
    dark_settings_button(text="Save", command=lambda: do_save(False)).pack(side="right", padx=8)
    dark_settings_button(text="Save and Restart", command=lambda: do_save(True)).pack(side="right")


CLIP_EDITOR_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".webm", ".avi"}
CLIP_EDITOR_MAX_RECENT_RECORDINGS = 30
# First entry means "keep the source file's own extension" -- do_trim() checks for it by identity
# rather than treating it as a real container, so it must stay first.
CLIP_EDITOR_OUTPUT_FORMATS = ["Same as source", ".mp4", ".mkv", ".mov", ".avi", ".webm"]
# First entry means "don't force a re-encode for quality's sake" -- trim_clip() still stream-copies
# in fast mode when quality is left at this default, exactly like it always has. Picking a specific
# resolution forces a real ffmpeg re-encode (even in fast, non-precise mode) since a stream copy
# can't rescale video at all -- get_quality_scale_height() below resolves the trade-off.
CLIP_EDITOR_QUALITY_OPTIONS = ["Same as source", "1080p", "720p", "480p"]
CLIP_EDITOR_QUALITY_HEIGHTS = {"1080p": 1080, "720p": 720, "480p": 480}
# "trim_mode" internal values (stored in config) -- Precise is the default, applied whenever the
# key is absent/unrecognized, since most people would rather wait slightly longer than risk the
# broken-leading-frame glitch Fast (lossless stream copy) can leave at the cut point. Fast lives
# only in Settings now, not as a per-trim choice in the editor itself, for exactly that reason.
CLIP_EDITOR_TRIM_MODE_OPTIONS = ["precise", "fast"]
CLIP_EDITOR_TRIM_MODE_LABELS = {
    "precise": "Precise (recommended -- re-encodes, always starts cleanly)",
    "fast": "Fast (stream copy -- quicker, may glitch at the cut point)",
}
CLIP_EDITOR_TRIM_MODE_LABELS_BY_LABEL = {v: k for k, v in CLIP_EDITOR_TRIM_MODE_LABELS.items()}
# The libx264 CRF used whenever a re-encode happens -- whether forced by "Precise", by a chosen
# resolution, or both -- since resolution and encode quality are independent knobs and this app
# only exposes the former; this is the same value build_trim_command always used for its one re-
# encode path before resolution scaling existed.
CLIP_EDITOR_DEFAULT_CRF = 18


def get_quality_scale_height(quality_choice):
    """Resolves a CLIP_EDITOR_QUALITY_OPTIONS choice to a target output height in pixels (passed
    to ffmpeg as -vf scale=-2:HEIGHT, which keeps the source aspect ratio), or None for "Same as
    source" -- None specifically means "no explicit resize," which is what lets build_trim_command
    still choose a lossless stream copy when nothing else forces a re-encode."""
    return CLIP_EDITOR_QUALITY_HEIGHTS.get(quality_choice)


def list_recent_recordings(folder, limit=CLIP_EDITOR_MAX_RECENT_RECORDINGS):
    """Lists up to `limit` video files under folder, newest first -- recursive, since
    organize_into_game_subfolders nests recordings one level deeper per game."""
    if not folder or not os.path.isdir(folder):
        return []
    found = []
    for root_dir, _dirs, files in os.walk(folder):
        for name in files:
            if os.path.splitext(name)[1].lower() not in CLIP_EDITOR_VIDEO_EXTENSIONS:
                continue
            path = os.path.join(root_dir, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            found.append((path, mtime))
    found.sort(key=lambda item: item[1], reverse=True)
    return [path for path, _mtime in found[:limit]]


def validate_trim_range(start_seconds, end_seconds, duration_seconds=None):
    """Returns None if [start_seconds, end_seconds) is a sane trim range, otherwise an error
    message fit to show directly in the editor's status label. duration_seconds is optional
    (VLC doesn't always know a clip's length immediately after loading it) -- skipped when not
    yet available rather than blocking the user on a check that can't be performed yet."""
    if start_seconds < 0:
        return "Start time can't be negative."
    if end_seconds <= start_seconds:
        return "End time must be after the start time."
    if duration_seconds and start_seconds > duration_seconds:
        return "Start time is past the end of the clip."
    if duration_seconds and end_seconds > duration_seconds + 0.5:  # small slack for UI rounding
        return "End time is past the end of the clip."
    return None


def is_file_being_recorded(path, recording_state):
    """True if `path` is the exact file OBS is currently writing to -- the clip editor refuses
    to open this one, rather than letting someone try to trim a file that's still growing."""
    active_path = recording_state.get("current_path")
    if not active_path:
        return False
    return os.path.normpath(active_path) == os.path.normpath(path)


def open_clip_editor_window(editor_state, config, recording_state, overlay_state, icon):
    if editor_state.get("open"):
        if time.time() - editor_state.get("opened_at", 0) < EDITOR_STUCK_TIMEOUT_SECONDS:
            logging.info("Clip editor is already open.")
            return
        logging.warning(
            "Clip editor has appeared open for over %d seconds without closing; assuming it's "
            "stuck and allowing a new attempt.", EDITOR_STUCK_TIMEOUT_SECONDS,
        )

    overlay_root = overlay_state.get("root")
    if not overlay_root:
        logging.warning("Overlay isn't ready yet; can't open the clip editor. Try again in a moment.")
        return

    editor_state["open"] = True
    editor_state["opened_at"] = time.time()
    logging.info("Opening the clip editor.")

    def on_close():
        editor_state["open"] = False
        # pystray only rebuilds the native tray menu right after a menu item is clicked (see the
        # matching comment in the watcher loop) -- without this, "Edit Clips..." stays greyed out
        # until some unrelated menu click happens to refresh it, even though the editor is long
        # closed.
        icon.update_menu()

    def build():
        try:
            _run_clip_editor(overlay_root, config, recording_state, on_close, icon)
        except Exception:
            logging.exception("Clip editor crashed.")
            on_close()

    # Same reasoning as open_config_editor_window: built as a Toplevel of the overlay's already-
    # running interpreter, scheduled via .after() rather than a second independent tk.Tk() on its
    # own thread, which PyInstaller's bundled Tcl/Tk doesn't reliably support.
    overlay_root.after(0, build)


def _open_vlc_missing_window(master_root, on_close):
    """Shown instead of the real editor when VLC can't be found -- unlike the Settings tab's
    equivalent panel, there's no usable editor at all without it (no preview, no way to pick a
    start/end by watching the video), so this is the whole window rather than one section of it."""
    root = tk.Toplevel(master_root)
    root.bind("<Destroy>", lambda event: on_close() if event.widget is root else None)
    root.title("OBS Auto Recorder - Clip Editor")
    root.geometry("460x220")

    frame = tk.Frame(root, padx=16, pady=16)
    frame.pack(fill="both", expand=True)
    tk.Label(
        frame,
        text=(
            "VLC isn't installed, so the clip editor's video preview isn't available. Install it "
            "below, then try Edit Clips... again."
        ),
        anchor="w", justify="left", wraplength=420,
    ).pack(fill="x", pady=(0, 12))

    status_label = tk.Label(frame, text="", fg="#b00020", anchor="w", justify="left", wraplength=420)
    status_label.pack(fill="x", pady=(0, 8))

    def install_via_winget():
        install_button.config(state="disabled", text="Installing VLC...")
        logging.info("Clip editor: installing VLC via winget...")

        def worker():
            result = winget_install_vlc()

            def finish():
                success, reason = result
                if success and find_vlc():
                    logging.info("Clip editor: VLC installed successfully via winget.")
                    status_label.config(fg="#15803d", text="VLC installed -- close this window and try Edit Clips... again.")
                    install_button.pack_forget()
                else:
                    logging.warning(
                        "Clip editor: VLC install via winget did not finish (%s).",
                        reason or "VLC still not found after install",
                    )
                    install_button.config(state="normal", text="Install VLC via winget")
                    status_label.config(text=f"Install didn't finish ({reason or 'VLC still not found after install'}).")

            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    if has_winget():
        install_button = tk.Button(frame, text="Install VLC via winget", command=install_via_winget)
        install_button.pack(anchor="w", pady=(0, 8))

    download_link = tk.Label(
        frame, text="Or download it manually from videolan.org ↗", fg="#2563eb",
        font=("Segoe UI", 9, "underline"), cursor="hand2",
    )
    download_link.pack(anchor="w")
    download_link.bind("<Button-1>", lambda _event: webbrowser.open(VLC_DOWNLOAD_URL))

    tk.Button(root, text="Close", command=root.destroy).pack(anchor="e", padx=16, pady=16)


def _run_clip_editor(master_root, config, recording_state, on_close, icon):
    clip_editor_config = config.get("clip_editor", {})
    obs_config = config.get("obs", {})
    notifications_config = config.get("notifications", {})

    vlc_dir = resolve_vlc_path(clip_editor_config.get("vlc_path", ""))
    vlc_module = import_vlc_module() if vlc_dir else None
    if not vlc_module:
        logging.warning("Clip editor: VLC not found; showing the install-VLC prompt instead of the real editor.")
        _open_vlc_missing_window(master_root, on_close)
        return

    # Shares the same dark palette as the Settings editor (DARK_BG etc.) so the timeline doesn't
    # sit in a visibly different-colored strip against a lighter window -- there's deliberately
    # no seam between the timeline canvas and the frames around it. Local aliases here just keep
    # the rest of this function's many references short.
    EDITOR_BG = DARK_BG
    EDITOR_FG = DARK_FG
    ENTRY_BG = DARK_ENTRY_BG
    SEEKER_COLOR = "#f5a623"
    START_MARKER_COLOR = "#22c55e"
    END_MARKER_COLOR = "#ef4444"
    CHAPTER_MARKER_COLOR = "#3b82f6"
    TEMP_TIMESTAMP_COLOR = "#e5e7eb"
    # Reused everywhere this window shows status text, so the editor doesn't mix these with a
    # second, uncoordinated red/green/gray palette -- success/error/warning always match the
    # timeline's own start/end/seeker colors, and secondary text always matches the ruler's gray.
    MUTED_TEXT_COLOR = DARK_MUTED_FG

    root = tk.Toplevel(master_root)
    root.bind("<Destroy>", lambda event: on_close() if event.widget is root else None)
    root.title("OBS Auto Recorder - Clip Editor")
    root.geometry("1260x600")
    root.minsize(760, 420)
    root.configure(bg=EDITOR_BG)
    root.lift()
    root.focus_force()
    logging.info("Clip editor opened.")

    # Custom style names (rather than reconfiguring "TCombobox"/etc. directly) so this doesn't
    # leak into the Settings editor's own comboboxes, which stay on the app's normal light theme.
    style = ttk.Style()
    style.configure(
        "ClipEditor.TCombobox", fieldbackground=ENTRY_BG, background=EDITOR_BG, foreground=EDITOR_FG,
        arrowcolor=EDITOR_FG,
    )
    style.map(
        "ClipEditor.TCombobox",
        fieldbackground=[("readonly", ENTRY_BG), ("disabled", ENTRY_BG), ("!disabled", ENTRY_BG)],
        foreground=[("readonly", EDITOR_FG), ("disabled", MUTED_TEXT_COLOR), ("!disabled", EDITOR_FG)],
        selectbackground=[("readonly", ENTRY_BG), ("!disabled", ENTRY_BG)],
        selectforeground=[("readonly", EDITOR_FG), ("!disabled", EDITOR_FG)],
        arrowcolor=[("readonly", EDITOR_FG), ("!disabled", EDITOR_FG)],
    )
    style.configure("ClipEditor.Horizontal.TScale", background=EDITOR_BG, troughcolor=ENTRY_BG)
    style.configure("ClipEditor.Horizontal.TProgressbar", background=SEEKER_COLOR, troughcolor=ENTRY_BG)

    instance = create_vlc_instance_with_logging(vlc_module)
    player = instance.media_player_new()
    space_bar_stop_event = threading.Event()

    def release_file_lock():
        # A plain read-only handle, kept open only so Windows blocks a delete of whatever's
        # currently loaded (e.g. storage management's cleanup, or the user deleting it by hand)
        # while the editor has it open -- confirmed live that VLC's own file access does NOT do
        # this (a file it has open can still be deleted out from under it, mid-playback, without
        # error), and confirmed a second plain handle here coexists fine alongside VLC's.
        handle = state.get("file_handle")
        if handle:
            try:
                handle.close()
            except Exception:
                pass
            state["file_handle"] = None

    def acquire_file_lock(path):
        try:
            state["file_handle"] = open(path, "rb")
        except OSError as exc:
            state["file_handle"] = None
            logging.warning("Clip editor: could not lock %s against deletion while open: %s", os.path.basename(path), exc)

    def cleanup():
        space_bar_stop_event.set()
        release_file_lock()
        try:
            player.stop()
            instance.release()
        except Exception:
            pass

    def close_editor():
        # Stop playback (and release the VLC instance) BEFORE the window's video-output HWND
        # actually gets torn down -- relying on <Destroy> alone lets VLC keep trying to render
        # into an HWND Windows has already destroyed, which spams a cascade of native
        # SwapChain/CreateWindow errors instead of shutting down cleanly (confirmed by deliberately
        # closing the editor while a clip was still playing and watching libvlc's own log fill
        # with "Could not create the SwapChain" / "video output creation failed" messages until
        # cleanup() ran first here).
        logging.info("Clip editor closed.")
        cleanup()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_editor)
    root.bind("<Destroy>", lambda event: on_close() if event.widget is root else None)

    def dark_button(parent, **kwargs):
        return tk.Button(parent, bg=ENTRY_BG, fg=EDITOR_FG, activebackground=ENTRY_BG, activeforeground=EDITOR_FG, **kwargs)

    def fixed_size_button(parent, text, width_px, height_px, command, font=("Segoe UI", 11)):
        # tk.Button's own width/height options are in text-grid units, not pixels, so identical
        # single-character icons can still render as visibly different sizes depending on glyph
        # width -- wrapping in a fixed-size container with propagation disabled is the reliable
        # way to get genuinely equal-sized buttons regardless of what each one's icon looks like.
        container = tk.Frame(parent, width=width_px, height=height_px, bg=EDITOR_BG)
        container.pack_propagate(False)
        btn = dark_button(container, text=text, font=font, command=command)
        btn.pack(fill="both", expand=True)
        return container, btn

    # --- Open file row (audio track picker lives here too, right-justified against it) ---
    open_row = tk.Frame(root, bg=EDITOR_BG)
    open_row.pack(fill="x", padx=10, pady=(10, 4))
    dark_button(
        open_row, text="📂", font=("Segoe UI", 11), command=lambda: browse_for_file(),
    ).pack(side="left")

    recent_recordings = list_recent_recordings(config.get("obs", {}).get("output_folder"))
    recent_var = tk.StringVar()
    if recent_recordings:
        recent_combo = ttk.Combobox(
            open_row, textvariable=recent_var,
            values=[os.path.basename(p) for p in recent_recordings],
            state="readonly", width=40, style="ClipEditor.TCombobox",
        )
        recent_combo.pack(side="left", padx=(8, 0))

        def on_recent_selected(_event):
            index = recent_combo.current()
            if 0 <= index < len(recent_recordings):
                load_file(recent_recordings[index])

        recent_combo.bind("<<ComboboxSelected>>", on_recent_selected)

    audio_track_var = tk.StringVar()
    audio_track_ids = []
    audio_track_combo = ttk.Combobox(
        open_row, textvariable=audio_track_var, state="readonly", width=28, values=[],
        style="ClipEditor.TCombobox",
    )
    audio_track_combo.pack(side="right")
    tk.Label(open_row, text="Audio track:", bg=EDITOR_BG, fg=EDITOR_FG).pack(side="right", padx=(0, 6))

    configured_preview_quality = clip_editor_config.get("preview_quality", CLIP_EDITOR_DEFAULT_PREVIEW_QUALITY)
    if configured_preview_quality not in CLIP_EDITOR_PREVIEW_QUALITY_OPTIONS:
        configured_preview_quality = CLIP_EDITOR_DEFAULT_PREVIEW_QUALITY
    preview_quality_var = tk.StringVar(value=configured_preview_quality)
    preview_quality_combo = ttk.Combobox(
        open_row, textvariable=preview_quality_var, values=CLIP_EDITOR_PREVIEW_QUALITY_OPTIONS,
        state="readonly", width=24, style="ClipEditor.TCombobox",
    )
    preview_quality_combo.pack(side="right", padx=(0, 16))
    tk.Label(open_row, text="Preview:", bg=EDITOR_BG, fg=EDITOR_FG).pack(side="right", padx=(0, 6))

    def on_preview_quality_selected(_event):
        # Media options only take effect if set before set_media() -- changing this mid-playback
        # can't be applied retroactively to the media object already handed to the player, so the
        # only way to actually apply a new choice is to reopen the current file from scratch.
        # load_file() always resets to 0 and pauses there (its own pause_once_playing logic), so
        # both the previous position and play state have to be restored afterward once the newly
        # reopened media has actually finished loading -- same retry-until-ready pattern that
        # logic itself already uses, since there's no signal for "the new media is ready" besides
        # polling for a real length.
        if not state["path"]:
            return
        path = state["path"]
        reopen_seconds = player.get_time() / 1000
        was_playing = player.is_playing()
        load_file(path)

        def restore_position(attempts=0):
            if not root.winfo_exists():
                return
            if player.get_length() > 0:
                player.set_time(int(reopen_seconds * 1000))
                if was_playing:
                    player.play()
                update_play_pause_icon()
                draw_timeline()
            elif attempts < 50:
                root.after(100, lambda: restore_position(attempts + 1))

        root.after(150, restore_position)

    preview_quality_combo.bind("<<ComboboxSelected>>", on_preview_quality_selected)

    def refresh_audio_tracks():
        try:
            descriptions = player.audio_get_track_description()
        except Exception:
            descriptions = []
        # id -1 is libvlc's own "Disable" pseudo-track (mutes audio entirely) -- kept in the list
        # since it's a real, selectable option, not filtered out.
        audio_track_ids[:] = [track_id for track_id, _name in descriptions]
        names = [
            (name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name))
            for _track_id, name in descriptions
        ]
        audio_track_combo["values"] = names
        current_id = player.audio_get_track()
        if current_id in audio_track_ids:
            audio_track_combo.current(audio_track_ids.index(current_id))
        state["tracks_loaded"] = True

    def on_audio_track_selected(_event):
        index = audio_track_combo.current()
        if 0 <= index < len(audio_track_ids):
            player.audio_set_track(audio_track_ids[index])

    audio_track_combo.bind("<<ComboboxSelected>>", on_audio_track_selected)

    # --- Video preview ---
    video_frame = tk.Frame(root, bg="black")
    video_frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))
    root.update_idletasks()
    platform_common.embed_video_player(player, video_frame)

    # --- Timeline (click/drag anywhere to seek; start/end markers drawn in their own colors so
    # they're never confused with the playback seeker; scroll to zoom, right-drag to pan once
    # zoomed in, for frame-level accuracy on longer recordings) ---
    TIMELINE_HEIGHT = 56
    TRACK_Y = 16
    RULER_TICK_TOP = TRACK_Y + 6
    RULER_LABEL_Y = TIMELINE_HEIGHT - 4
    MIN_VIEW_SECONDS = 0.5
    TICK_INTERVALS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600]

    time_label = tk.Label(root, text="00:00:00.000 / 00:00:00.000", anchor="e", bg=EDITOR_BG, fg=EDITOR_FG)
    time_label.pack(fill="x", padx=10, pady=(0, 2))

    timeline_canvas = tk.Canvas(root, height=TIMELINE_HEIGHT, bg=EDITOR_BG, highlightthickness=0)
    timeline_canvas.pack(fill="x", padx=10, pady=(0, 4))

    # The visible [start, end) window into the clip -- always the full clip until the user zooms
    # in, at which point this shrinks and the ruler/markers/seeker are all drawn relative to it
    # instead of the full duration.
    view_state = {"start": 0.0, "end": 0.0, "initialized": False}
    pan_state = {"active": False, "start_x": 0, "start_view_start": 0.0}
    # Set only when the Play button is actually pressed (see toggle_play_pause), never by a
    # timeline click -- a click always just seeks and plays from there.
    temp_timestamp_state = {"seconds": None}

    def reset_view():
        view_state["start"] = 0.0
        view_state["end"] = state["duration"] or 0.0
        view_state["initialized"] = True

    def canvas_x_to_seconds(x):
        width = timeline_canvas.winfo_width()
        span = view_state["end"] - view_state["start"]
        if width <= 0 or span <= 0:
            return 0.0
        frac = min(max(x / width, 0.0), 1.0)
        return view_state["start"] + frac * span

    def seconds_to_canvas_x(seconds):
        # Deliberately not clamped to [0, width] -- callers need to tell an off-screen marker
        # (which should be skipped) apart from one that's legitimately at the very edge.
        width = timeline_canvas.winfo_width()
        span = view_state["end"] - view_state["start"]
        if width <= 0 or span <= 0:
            return 0
        return (seconds - view_state["start"]) / span * width

    def choose_tick_interval(span, width):
        if span <= 0 or width <= 0:
            return TICK_INTERVALS[0]
        target_count = max(width / 90, 1)
        raw_interval = span / target_count
        for interval in TICK_INTERVALS:
            if interval >= raw_interval:
                return interval
        return TICK_INTERVALS[-1]

    def draw_ruler(width, span):
        interval = choose_tick_interval(span, width)
        first_tick = math.floor(view_state["start"] / interval) * interval
        t = first_tick
        # A few extra iterations beyond view_state["end"] is cheap insurance against float drift
        # accumulating across many small `interval` steps on a long, deeply zoomed-in clip.
        guard = 0
        while t <= view_state["end"] + interval and guard < 500:
            x = seconds_to_canvas_x(t)
            if 0 <= x <= width:
                timeline_canvas.create_line(x, RULER_TICK_TOP, x, RULER_TICK_TOP + 6, fill="#888888")
                label = format_timestamp(max(t, 0))
                if interval >= 1:
                    label = label.split(".")[0]
                timeline_canvas.create_text(
                    x, RULER_LABEL_Y, text=label, fill="#aaaaaa", font=("Segoe UI", 7), anchor="s"
                )
            t += interval
            guard += 1

    def draw_timeline():
        timeline_canvas.delete("all")
        width = timeline_canvas.winfo_width()
        if width <= 1:
            return
        # The empty track is always visible (even with nothing loaded yet) -- only the ruler
        # numbers and the colored markers/seeker need an actual clip with a known duration.
        timeline_canvas.create_rectangle(0, TRACK_Y - 3, width, TRACK_Y + 3, fill="#555555", outline="")
        if not state["duration"]:
            return
        if not view_state["initialized"]:
            reset_view()
        span = view_state["end"] - view_state["start"]
        draw_ruler(width, span)
        try:
            x = seconds_to_canvas_x(parse_timestamp(start_var.get()))
            if 0 <= x <= width:
                timeline_canvas.create_line(x, 0, x, TRACK_Y + 3, fill=START_MARKER_COLOR, width=3)
        except ValueError:
            pass
        try:
            x = seconds_to_canvas_x(parse_timestamp(end_var.get()))
            if 0 <= x <= width:
                timeline_canvas.create_line(x, 0, x, TRACK_Y + 3, fill=END_MARKER_COLOR, width=3)
        except ValueError:
            pass
        x = seconds_to_canvas_x(player.get_time() / 1000)
        if 0 <= x <= width:
            timeline_canvas.create_line(x, 0, x, TRACK_Y + 3, fill=SEEKER_COLOR, width=2)
        for marker_seconds in state["markers"]:
            mx = seconds_to_canvas_x(marker_seconds)
            if 0 <= mx <= width:
                timeline_canvas.create_line(
                    mx, TRACK_Y - 3, mx, TRACK_Y + 3, fill=CHAPTER_MARKER_COLOR, width=3
                )
                timeline_canvas.create_polygon(
                    mx - 5, TRACK_Y - 3, mx + 5, TRACK_Y - 3, mx, TRACK_Y - 10,
                    fill=CHAPTER_MARKER_COLOR, outline="",
                )
        if temp_timestamp_state["seconds"] is not None:
            tx = seconds_to_canvas_x(temp_timestamp_state["seconds"])
            if 0 <= tx <= width:
                timeline_canvas.create_line(
                    tx, 0, tx, TRACK_Y + 3, fill=TEMP_TIMESTAMP_COLOR, width=2, dash=(3, 2)
                )

    def refresh_markers():
        # OBS chapter markers ("markers" in the app's own terminology) -- only ever present on a
        # Hybrid MP4 recording; get_full_chapter_descriptions returns None until VLC has finished
        # parsing the media (mirrors the audio-track-count pattern above), so this is retried from
        # poll() until it stops being None rather than assumed to succeed on the first call.
        try:
            descriptions = player.get_full_chapter_descriptions(-1)
        except Exception:
            descriptions = None
        if descriptions is None:
            return
        # Confirmed live: OBS's own Hybrid MP4 output always carries an implicit chapter at
        # time_offset 0 (named "Start") on top of any the user actually places -- not a real
        # marker, just a container-format artifact every such recording has, so it's filtered
        # out here rather than shown as a confusing phantom marker at the very start of a clip
        # that never had "Add Marker" pressed at all.
        state["markers"] = sorted(d.time_offset / 1000 for d in descriptions if d.time_offset > 0)
        state["markers_loaded"] = True
        draw_timeline()

    def find_marker_near_x(x, tolerance=6):
        for marker_seconds in state["markers"]:
            if abs(seconds_to_canvas_x(marker_seconds) - x) <= tolerance:
                return marker_seconds
        return None

    def jump_to_marker(marker_seconds):
        duration = state["duration"] or 0.0
        start_seconds = max(0.0, marker_seconds - 30)
        start_var.set(format_timestamp(start_seconds))
        end_var.set(format_timestamp(min(duration, marker_seconds + 5) if duration else marker_seconds + 5))
        player.set_time(int(start_seconds * 1000))
        draw_timeline()
        status_label.config(
            fg=CHAPTER_MARKER_COLOR,
            text=f"Marker at {format_timestamp(marker_seconds)} -- range set to 30s before, 5s after. Adjust as needed.",
        )

    def seek_to_canvas_x(x):
        if not state["path"] or not state["duration"]:
            return
        player.set_time(int(canvas_x_to_seconds(x) * 1000))
        draw_timeline()

    EDGE_HIT_TOLERANCE = 6

    def find_edge_near_x(x):
        # End is checked first -- on a short clip where Start and End sit close together,
        # grabbing whichever handle is currently on top (End is drawn after Start) matches
        # what the user would actually see under the cursor.
        try:
            end_x = seconds_to_canvas_x(parse_timestamp(end_var.get()))
            if abs(end_x - x) <= EDGE_HIT_TOLERANCE:
                return "end"
        except ValueError:
            pass
        try:
            start_x = seconds_to_canvas_x(parse_timestamp(start_var.get()))
            if abs(start_x - x) <= EDGE_HIT_TOLERANCE:
                return "start"
        except ValueError:
            pass
        return None

    drag_state = {"target": None}

    def on_timeline_press(x):
        drag_state["target"] = None
        if not state["duration"]:
            return
        edge = find_edge_near_x(x)
        if edge:
            drag_state["target"] = edge
            return
        marker_seconds = find_marker_near_x(x)
        if marker_seconds is not None:
            jump_to_marker(marker_seconds)
            return
        # Only while actually playing -- scrubbing through the timeline while paused (the normal
        # way to find a point) needs every click to seek and show that frame immediately, same
        # as always -- and now also starts playback from there, so a plain click always both
        # seeks and plays. Marking a candidate point happens separately, only when Play is
        # actually pressed (see toggle_play_pause).
        drag_state["target"] = "seek"
        seek_to_canvas_x(x)
        player.play()
        reclaim_focus_after_play()
        update_play_pause_icon()

    def on_timeline_drag(x):
        target = drag_state["target"]
        if target == "seek":
            seek_to_canvas_x(x)
            return
        if target not in ("start", "end"):
            return
        duration = state["duration"] or 0.0
        seconds = canvas_x_to_seconds(x)
        if target == "start":
            try:
                end_seconds = parse_timestamp(end_var.get())
            except ValueError:
                end_seconds = duration
            start_var.set(format_timestamp(max(0.0, min(seconds, end_seconds))))
        else:
            try:
                start_seconds = parse_timestamp(start_var.get())
            except ValueError:
                start_seconds = 0.0
            end_var.set(format_timestamp(max(start_seconds, min(seconds, duration))))
        draw_timeline()

    def on_timeline_release(_x):
        drag_state["target"] = None

    def zoom_view(factor, center_seconds):
        duration = state["duration"]
        if not duration:
            return
        span = view_state["end"] - view_state["start"]
        if span <= 0:
            return
        new_span = max(MIN_VIEW_SECONDS, min(span / factor, duration))
        left_frac = (center_seconds - view_state["start"]) / span
        new_start = center_seconds - left_frac * new_span
        new_start = max(0.0, min(new_start, duration - new_span))
        view_state["start"] = new_start
        view_state["end"] = new_start + new_span
        draw_timeline()

    def on_timeline_wheel(event):
        if not state["duration"]:
            return
        factor = 1.25 if event.delta > 0 else (1 / 1.25)
        zoom_view(factor, canvas_x_to_seconds(event.x))

    def on_timeline_horizontal_wheel(event):
        # A horizontal tilt-wheel/trackpad swipe reaches Tk as <Shift-MouseWheel> on Windows
        # (and Shift+plain-wheel is the standard fallback for a mouse without one) -- pans the
        # visible window instead of zooming, since zoom already owns the plain wheel.
        duration = state["duration"]
        if not duration:
            return
        span = view_state["end"] - view_state["start"]
        if span <= 0:
            return
        step = span * 0.1
        direction = -1 if event.delta > 0 else 1
        new_start = max(0.0, min(view_state["start"] + direction * step, duration - span))
        view_state["start"] = new_start
        view_state["end"] = new_start + span
        draw_timeline()

    def zoom_in_button():
        if not state["duration"]:
            return
        mid = (view_state["start"] + view_state["end"]) / 2
        zoom_view(1.5, mid)

    def zoom_out_button():
        if not state["duration"]:
            return
        mid = (view_state["start"] + view_state["end"]) / 2
        zoom_view(1 / 1.5, mid)

    def zoom_reset_button():
        reset_view()
        draw_timeline()

    def on_pan_press(event):
        pan_state["active"] = True
        pan_state["start_x"] = event.x
        pan_state["start_view_start"] = view_state["start"]

    def on_pan_drag(event):
        duration = state["duration"]
        width = timeline_canvas.winfo_width()
        if not pan_state["active"] or not duration or width <= 0:
            return
        span = view_state["end"] - view_state["start"]
        dx_seconds = (event.x - pan_state["start_x"]) / width * span
        new_start = max(0.0, min(pan_state["start_view_start"] - dx_seconds, duration - span))
        view_state["start"] = new_start
        view_state["end"] = new_start + span
        draw_timeline()

    def on_pan_release(_event):
        pan_state["active"] = False

    def ensure_playhead_visible():
        # Only auto-scrolls during actual playback -- otherwise this would fight a deliberate pan
        # or scrub made while paused, snapping straight back to the playhead the instant the user
        # releases the mouse (confirmed live: panning while paused had no visible effect at all
        # until this guard was added).
        duration = state["duration"]
        if not duration or pan_state["active"] or not player.is_playing():
            return
        span = view_state["end"] - view_state["start"]
        current = player.get_time() / 1000
        if current < view_state["start"] or current > view_state["end"]:
            new_start = max(0.0, min(current - span / 2, duration - span))
            view_state["start"] = new_start
            view_state["end"] = new_start + span

    timeline_canvas.bind("<Button-1>", lambda event: on_timeline_press(event.x))
    timeline_canvas.bind("<B1-Motion>", lambda event: on_timeline_drag(event.x))
    timeline_canvas.bind("<ButtonRelease-1>", on_timeline_release)
    timeline_canvas.bind("<Button-3>", on_pan_press)
    timeline_canvas.bind("<B3-Motion>", on_pan_drag)
    timeline_canvas.bind("<ButtonRelease-3>", on_pan_release)
    timeline_canvas.bind("<MouseWheel>", on_timeline_wheel)
    timeline_canvas.bind("<Shift-MouseWheel>", on_timeline_horizontal_wheel)
    timeline_canvas.bind("<Configure>", lambda event: draw_timeline())

    # --- Controls row: zoom (left), transport (centered), volume (right) ---
    controls_row = tk.Frame(root, bg=EDITOR_BG)
    controls_row.pack(fill="x", padx=10, pady=(4, 12))
    controls_row.columnconfigure(0, weight=1)
    controls_row.columnconfigure(1, weight=0)
    controls_row.columnconfigure(2, weight=1)

    ZOOM_BUTTON_SIZE = 32
    TRANSPORT_BUTTON_WIDTH = 50
    TRANSPORT_BUTTON_HEIGHT = 32

    zoom_group = tk.Frame(controls_row, bg=EDITOR_BG)
    zoom_group.grid(row=0, column=0, sticky="w")
    zoom_in_container, _ = fixed_size_button(zoom_group, "➕", ZOOM_BUTTON_SIZE, ZOOM_BUTTON_SIZE, zoom_in_button)
    zoom_in_container.pack(side="left")
    zoom_reset_container, _ = fixed_size_button(
        zoom_group, "↺", ZOOM_BUTTON_SIZE, ZOOM_BUTTON_SIZE, zoom_reset_button,
    )
    zoom_reset_container.pack(side="left", padx=(6, 0))
    zoom_out_container, _ = fixed_size_button(zoom_group, "➖", ZOOM_BUTTON_SIZE, ZOOM_BUTTON_SIZE, zoom_out_button)
    zoom_out_container.pack(side="left", padx=(6, 0))

    transport_buttons = tk.Frame(controls_row, bg=EDITOR_BG)
    transport_buttons.grid(row=0, column=1)
    rewind_container, _ = fixed_size_button(
        transport_buttons, "⏪", TRANSPORT_BUTTON_WIDTH, TRANSPORT_BUTTON_HEIGHT, lambda: seek_relative(-5),
    )
    rewind_container.pack(side="left")
    play_pause_container, play_pause_button = fixed_size_button(
        transport_buttons, "▶", TRANSPORT_BUTTON_WIDTH, TRANSPORT_BUTTON_HEIGHT, lambda: toggle_play_pause(),
    )
    play_pause_container.pack(side="left", padx=6)
    stop_container, _ = fixed_size_button(
        transport_buttons, "⏹", TRANSPORT_BUTTON_WIDTH, TRANSPORT_BUTTON_HEIGHT, lambda: stop_to_target(),
    )
    stop_container.pack(side="left")
    forward_container, _ = fixed_size_button(
        transport_buttons, "⏩", TRANSPORT_BUTTON_WIDTH, TRANSPORT_BUTTON_HEIGHT, lambda: seek_relative(5),
    )
    forward_container.pack(side="left", padx=(6, 0))

    def stop_to_target():
        # Priority: wherever the last timeline click left a temporary marker: else Start (the
        # green marker, which is 0:00 by default on a freshly opened file, satisfying "go to the
        # beginning of the clip" as the natural last resort without needing a separate check).
        if not state["path"]:
            return
        if temp_timestamp_state["seconds"] is not None:
            target = temp_timestamp_state["seconds"]
        else:
            try:
                target = parse_timestamp(start_var.get())
            except ValueError:
                target = 0.0
        if player.is_playing():
            player.pause()
        player.set_time(int(max(0.0, target) * 1000))
        update_play_pause_icon()
        draw_timeline()

    def update_play_pause_icon():
        play_pause_button.config(text="⏸" if player.is_playing() else "▶")

    volume_group = tk.Frame(controls_row, bg=EDITOR_BG)
    volume_group.grid(row=0, column=2, sticky="e")
    # Explicitly labeled "Preview" -- this only ever calls player.audio_set_volume() on the VLC
    # instance showing THIS window's playback; it never reaches ffmpeg or the exported file at
    # all. Confirmed live this reads as ambiguous against Track Routing's per-track dB gain below
    # (which DOES change the exported file) -- a bare speaker icon gave no hint they're two
    # completely separate things, one temporary/for-your-ears-right-now, the other permanent and
    # baked into the output.
    tk.Label(
        volume_group, text="🔊 Preview:", font=("Segoe UI", 9), bg=EDITOR_BG, fg=EDITOR_FG,
    ).pack(side="left", padx=(0, 6))
    volume_var = tk.IntVar(value=100)

    def on_volume_change(value):
        try:
            player.audio_set_volume(int(float(value)))
        except Exception:
            pass

    ttk.Scale(
        volume_group, from_=0, to=100, orient="horizontal", variable=volume_var, command=on_volume_change,
        length=140, style="ClipEditor.Horizontal.TScale",
    ).pack(side="left")

    # --- Start/End controls (quality picker lives on this same line as the Precise preset) ---
    range_row = tk.Frame(root, bg=EDITOR_BG)
    range_row.pack(fill="x", padx=10, pady=4)
    start_var = tk.StringVar(value="00:00:00.000")
    end_var = tk.StringVar(value="00:00:00.000")
    dark_button(range_row, text="Start", command=lambda: set_start()).pack(side="left")
    tk.Entry(
        range_row, textvariable=start_var, width=14, bg=ENTRY_BG, fg=EDITOR_FG, insertbackground=EDITOR_FG,
    ).pack(side="left", padx=(4, 16))
    dark_button(range_row, text="End", command=lambda: set_end()).pack(side="left")
    tk.Entry(
        range_row, textvariable=end_var, width=14, bg=ENTRY_BG, fg=EDITOR_FG, insertbackground=EDITOR_FG,
    ).pack(side="left", padx=(4, 16))
    default_quality = clip_editor_config.get("default_quality", CLIP_EDITOR_QUALITY_OPTIONS[0])
    if default_quality not in CLIP_EDITOR_QUALITY_OPTIONS:
        default_quality = CLIP_EDITOR_QUALITY_OPTIONS[0]
    tk.Label(range_row, text="Resolution:", bg=EDITOR_BG, fg=EDITOR_FG).pack(side="left", padx=(16, 0))
    quality_var = tk.StringVar(value=default_quality)
    ttk.Combobox(
        range_row, textvariable=quality_var, values=CLIP_EDITOR_QUALITY_OPTIONS, state="readonly", width=16,
        style="ClipEditor.TCombobox",
    ).pack(side="left", padx=(6, 0))

    # Forces a re-encode targeting an explicit bitrate (see compute_target_video_bitrate_kbps)
    # instead of CRF -- e.g. so a clip comes out under a specific upload-size limit. Settings'
    # own default target size (if any) pre-fills and pre-checks this; still freely overridable
    # per trim.
    default_target_size = clip_editor_config.get("default_target_size_mb")
    limit_size_var = tk.BooleanVar(value=bool(default_target_size))
    target_size_var = tk.StringVar(value=str(default_target_size) if default_target_size else "")
    tk.Checkbutton(
        range_row, text="Limit size to", variable=limit_size_var,
        bg=EDITOR_BG, fg=EDITOR_FG, activebackground=EDITOR_BG, activeforeground=EDITOR_FG,
        selectcolor=ENTRY_BG,
    ).pack(side="left", padx=(16, 0))
    tk.Entry(
        range_row, textvariable=target_size_var, width=6, bg=ENTRY_BG, fg=EDITOR_FG, insertbackground=EDITOR_FG,
    ).pack(side="left", padx=(4, 4))
    tk.Label(range_row, text="MB", bg=EDITOR_BG, fg=EDITOR_FG).pack(side="left")

    start_var.trace_add("write", lambda *_args: draw_timeline())
    end_var.trace_add("write", lambda *_args: draw_timeline())

    # --- Output format (same line as Resolution) + Close/Trim (same line as Output format) ---
    default_format = clip_editor_config.get("default_output_format", CLIP_EDITOR_OUTPUT_FORMATS[0])
    if default_format not in CLIP_EDITOR_OUTPUT_FORMATS:
        default_format = CLIP_EDITOR_OUTPUT_FORMATS[0]
    tk.Label(range_row, text="Output format:", bg=EDITOR_BG, fg=EDITOR_FG).pack(side="left", padx=(16, 0))
    format_var = tk.StringVar(value=default_format)
    ttk.Combobox(
        range_row, textvariable=format_var, values=CLIP_EDITOR_OUTPUT_FORMATS, state="readonly", width=16,
        style="ClipEditor.TCombobox",
    ).pack(side="left", padx=(6, 0))

    trim_button = dark_button(range_row, text="✂ Trim Clip", command=lambda: do_trim())
    trim_button.pack(side="right")
    close_button = dark_button(range_row, text="✕ Close", command=close_editor)
    close_button.pack(side="right", padx=(0, 8))

    # --- Audio tracks row: "Fix audio track sync" checkbox (left) + "Track Routing..." button
    # (right), both on one line. A prior version of the sync fix reapplied one fixed, configured
    # offset to every process-capture track at trim time -- removed after it was confirmed live
    # to double-correct a recording that already had the live per-record fix baked in (see
    # sync_multi_track_audio/apply_process_capture_sync_offset, run on every OBS-ready check),
    # producing a NEW ~27ms misalignment that wasn't there before. This version instead measures
    # the ACTUAL clip's own audio when Trim Clip is pressed -- independently per track, since real
    # process-capture tracks were confirmed live to each lag Desktop Audio by a genuinely
    # different amount within the same recording (Discord's own jitter-buffer pipeline adds
    # latency a locally-rendered game never has) -- so it stays correct whether or not the live
    # fix already applied, and whatever it applies is grounded in this specific clip rather than a
    # single shared guess. See measure_clip_audio_sync_shifts_ms.
    audio_tracks_row = tk.Frame(root, bg=EDITOR_BG)
    audio_tracks_row.pack(fill="x", padx=10, pady=4)
    fix_sync_tracks = compute_process_capture_tracks(obs_config)
    fix_sync_reference_track = compute_reference_track(obs_config)
    fix_sync_var = tk.BooleanVar(value=clip_editor_config.get("auto_fix_audio_sync", False))
    fix_sync_checkbox = tk.Checkbutton(
        audio_tracks_row, text="Fix audio track sync", variable=fix_sync_var,
        bg=EDITOR_BG, fg=EDITOR_FG, activebackground=EDITOR_BG, activeforeground=EDITOR_FG,
        selectcolor=ENTRY_BG,
    )
    fix_sync_checkbox.pack(side="left")
    if fix_sync_tracks and fix_sync_reference_track:
        tk.Label(
            audio_tracks_row,
            text=f"  (measures & aligns track(s) {', '.join(str(t) for t in fix_sync_tracks)} when trimmed)",
            bg=EDITOR_BG, fg=MUTED_TEXT_COLOR,
        ).pack(side="left")
    else:
        fix_sync_checkbox.config(state="disabled")
        tk.Label(
            audio_tracks_row, text="  (not configured in Settings)", bg=EDITOR_BG, fg=MUTED_TEXT_COLOR,
        ).pack(side="left")

    # --- Track Routing: consolidate multiple source tracks into fewer output tracks (e.g. mix
    # Spotify and Firefox together), drop a source entirely (e.g. remove Desktop Audio), and mute
    # an output track -- all via a checkbox matrix, mirroring the OBS-side Quick Multi-Track Setup
    # dialog's role (open_multi_track_quick_setup) but operating on an already-recorded file's
    # tracks instead of live OBS inputs. Subsumes the simpler old per-track mute checkboxes: an
    # output track with every source unchecked is just excluded from the file, and a
    # checked-but-muted output track stays present but silent -- see
    # build_audio_routing_filter_args. State (routing_state) is rebuilt from scratch, and any
    # open dialog closed, every time a different file loads, since track count varies per
    # recording and stale routing for a since-replaced file's track layout could silently apply
    # to the wrong tracks.
    track_name_hints_map = track_name_hints(obs_config)
    routing_state = {"track_count": 0, "routing_vars": {}, "mute_vars": {}, "gain_vars": {}}
    routing_dialog_state = {"window": None}

    def rebuild_routing_state(track_count):
        if routing_dialog_state["window"] is not None:
            try:
                if routing_dialog_state["window"].winfo_exists():
                    routing_dialog_state["window"].destroy()
            except tk.TclError:
                pass
            routing_dialog_state["window"] = None
        routing_state["track_count"] = track_count
        routing_state["routing_vars"] = {
            dest: {src: tk.BooleanVar(value=(src == dest)) for src in range(1, track_count + 1)}
            for dest in range(1, track_count + 1)
        }
        routing_state["mute_vars"] = {dest: tk.BooleanVar(value=False) for dest in range(1, track_count + 1)}
        # Pre-fills from Settings > Clip Editor > "Default Track Gains (dB)" (same {dest: {src:
        # gain}} shape, string-keyed since it round-trips through JSON) -- still freely editable
        # or clearable per trim from here, this is just the dialog's own starting point.
        default_gains = clip_editor_config.get("default_gains_db") or {}
        routing_state["gain_vars"] = {
            dest: {
                src: tk.StringVar(value=str(default_gains.get(str(dest), {}).get(str(src)) or ""))
                for src in range(1, track_count + 1)
            }
            for dest in range(1, track_count + 1)
        }
        track_routing_button.config(state="normal" if track_count else "disabled")

    def probe_track_count_for_routing(path):
        ffmpeg_path = resolve_ffmpeg_path("ffmpeg")
        count = probe_audio_stream_count(ffmpeg_path, path) if ffmpeg_path else None

        def apply():
            if root.winfo_exists() and state["path"] == path:  # guards against a stale probe
                rebuild_routing_state(count or 0)

        try:
            root.after(0, apply)
        except tk.TclError:
            pass

    def open_track_routing_dialog():
        n = routing_state["track_count"]
        if not n:
            return
        if routing_dialog_state["window"] is not None:
            try:
                if routing_dialog_state["window"].winfo_exists():
                    routing_dialog_state["window"].lift()
                    return
            except tk.TclError:
                pass

        dialog = tk.Toplevel(root)
        dialog.title("Track Routing")
        dialog.configure(bg=EDITOR_BG)
        dialog.transient(root)
        routing_dialog_state["window"] = dialog

        def on_dialog_close():
            routing_dialog_state["window"] = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", on_dialog_close)

        tk.Label(
            dialog,
            text=(
                "Check which source track(s) (columns) feed into each output track (rows) below "
                "-- check several under one output to mix them together, leave a source "
                "unchecked everywhere to drop it from the export entirely, or check \"Mute\" to "
                "keep an output track present but silent. The dB box under a checked source boosts "
                "(positive) or attenuates (negative) just that source for that one output -- the "
                "same source can have a different gain (or none) on each output it's routed to; "
                "leave blank or 0 for no change. This is baked into the exported FILE itself by "
                "ffmpeg -- separate from (and unaffected by) the \"Preview\" volume slider above, "
                "which only ever changes what you hear while scrubbing here, never the output."
            ),
            bg=EDITOR_BG, fg=EDITOR_FG, anchor="w", justify="left", wraplength=90 + 46 * n,
        ).grid(row=0, column=0, columnspan=n + 2, sticky="w", padx=10, pady=(10, 8))

        header_row = 1
        for src in range(1, n + 1):
            hint = track_name_hints_map.get(src)
            header_text = f"{src}\n({hint})" if hint else str(src)
            tk.Label(dialog, text=header_text, bg=EDITOR_BG, fg=EDITOR_FG, justify="center").grid(
                row=header_row, column=src, padx=4, pady=(0, 4)
            )
        tk.Label(dialog, text="Mute", bg=EDITOR_BG, fg=EDITOR_FG).grid(
            row=header_row, column=n + 1, padx=(14, 10)
        )

        for dest in range(1, n + 1):
            row = header_row + dest
            tk.Label(dialog, text=f"Output {dest}:", bg=EDITOR_BG, fg=EDITOR_FG, anchor="w").grid(
                row=row, column=0, sticky="w", padx=(10, 4), pady=2
            )
            for src in range(1, n + 1):
                # A small Frame per cell keeps the checkbox and its gain entry stacked in the
                # same grid position, rather than needing a whole extra row/column pair per
                # source just for gain -- the entry stays visible (not shown/hidden on check
                # state) since that would need extra trace callbacks for no real benefit: a
                # gain typed under an unchecked source is simply never read (see do_trim's
                # gains_db build, which only looks at gain for sources actually routed
                # somewhere).
                cell = tk.Frame(dialog, bg=EDITOR_BG)
                cell.grid(row=row, column=src, pady=2, padx=1)
                # indicatoron=False + a real width/height renders as a solid block that swaps
                # its WHOLE background color between bg (off) and selectcolor (on), rather than
                # a native tiny indicator square with a checkmark drawn inside it -- confirmed
                # live that the native indicator's checkmark was hard to see against this dark
                # theme (a dark glyph on a dark selectcolor fill has very little contrast); a
                # full color swap is unambiguous regardless of theme.
                tk.Checkbutton(
                    cell, variable=routing_state["routing_vars"][dest][src],
                    indicatoron=False, width=2, height=1,
                    bg=ENTRY_BG, fg=EDITOR_FG, activebackground=ENTRY_BG, activeforeground=EDITOR_FG,
                    selectcolor=START_MARKER_COLOR,
                ).pack()
                tk.Entry(
                    cell, textvariable=routing_state["gain_vars"][dest][src], width=4,
                    bg=ENTRY_BG, fg=EDITOR_FG, insertbackground=EDITOR_FG, justify="center",
                ).pack(pady=(2, 0))
            tk.Checkbutton(
                dialog, variable=routing_state["mute_vars"][dest],
                indicatoron=False, width=2, height=1,
                bg=ENTRY_BG, fg=EDITOR_FG, activebackground=ENTRY_BG, activeforeground=EDITOR_FG,
                selectcolor=END_MARKER_COLOR,
            ).grid(row=row, column=n + 1, padx=(14, 10), pady=2)

        default_gains = clip_editor_config.get("default_gains_db") or {}

        def reset_to_defaults():
            for dest in range(1, n + 1):
                for src in range(1, n + 1):
                    routing_state["routing_vars"][dest][src].set(src == dest)
                    routing_state["gain_vars"][dest][src].set(
                        str(default_gains.get(str(dest), {}).get(str(src)) or "")
                    )
                routing_state["mute_vars"][dest].set(False)

        button_row = header_row + n + 1
        dark_button(dialog, text="Reset to defaults", command=reset_to_defaults).grid(
            row=button_row, column=0, columnspan=3, sticky="w", padx=10, pady=(8, 10)
        )
        dark_button(dialog, text="Close", command=on_dialog_close).grid(
            row=button_row, column=n - 1, columnspan=3, sticky="e", padx=10, pady=(8, 10)
        )

    track_routing_button = dark_button(audio_tracks_row, text="Track Routing...", command=open_track_routing_dialog)
    track_routing_button.pack(side="left", padx=(16, 0))
    track_routing_button.config(state="disabled")

    # --- Status + trim progress ---
    status_label = tk.Label(
        root, text="", fg=END_MARKER_COLOR, bg=EDITOR_BG, anchor="w", justify="left", wraplength=780,
    )
    status_label.pack(fill="x", padx=10, pady=(8, 0))

    progress = ttk.Progressbar(root, mode="indeterminate", style="ClipEditor.Horizontal.TProgressbar")

    state = {
        "path": None, "duration": 0.0, "tracks_loaded": False, "file_handle": None,
        "markers": [], "markers_loaded": False,
    }

    def browse_for_file():
        path = filedialog.askopenfilename(
            title="Open a recording", filetypes=[("Video files", "*.mp4 *.mkv *.mov *.flv *.ts *.webm *.avi"), ("All files", "*.*")],
        )
        if path:
            load_file(path)

    def load_file(path):
        if is_file_being_recorded(path, recording_state):
            logging.warning("Clip editor: refused to open %s -- it's still being recorded.", os.path.basename(path))
            status_label.config(
                fg=SEEKER_COLOR,
                text="That recording is still in progress -- wait for it to finish before trimming it.",
            )
            return
        release_file_lock()
        player.stop()
        media = instance.media_new(path)
        for option in CLIP_EDITOR_PREVIEW_QUALITY_MEDIA_OPTIONS.get(preview_quality_var.get(), []):
            media.add_option(option)
        player.set_media(media)
        player.audio_set_volume(volume_var.get())
        player.play()
        reclaim_focus_after_play()
        acquire_file_lock(path)

        # Pausing immediately after play() races VLC's own async open/buffer state -- called
        # this early, pause() is liable to be silently dropped, leaving the clip playing all the
        # way through instead of stopping on its first frame like a freshly-opened file should.
        # Poll (on the Tk thread, not a VLC event callback, so there's nothing here that needs to
        # worry about calling back into Tkinter from a non-Tk thread) until playback has actually
        # started, then pause; gives up after ~5s so a genuinely broken file doesn't poll forever.
        def pause_once_playing(attempts=0):
            if not root.winfo_exists():
                return
            if player.get_state() == vlc_module.State.Playing:
                player.pause()
                update_play_pause_icon()
            elif attempts < 50:
                root.after(100, lambda: pause_once_playing(attempts + 1))

        root.after(50, pause_once_playing)

        state["path"] = path
        state["duration"] = 0.0
        state["tracks_loaded"] = False
        state["markers"] = []
        state["markers_loaded"] = False
        temp_timestamp_state["seconds"] = None
        view_state["initialized"] = False
        audio_track_ids.clear()
        audio_track_combo.set("")
        audio_track_combo["values"] = []
        start_var.set(format_timestamp(0))
        end_var.set(format_timestamp(0))
        status_label.config(text="")
        root.title(f"OBS Auto Recorder - Clip Editor - {os.path.basename(path)}")
        logging.info("Clip editor: opened %s", os.path.basename(path))
        rebuild_routing_state(0)
        threading.Thread(target=probe_track_count_for_routing, args=(path,), daemon=True).start()

    def toggle_play_pause():
        if not state["path"]:
            return
        if player.is_playing():
            player.pause()
        else:
            # Marks wherever playback is about to resume from as the candidate point Start/End
            # pick up next -- the one deliberate moment ("I'm choosing to play from here") that
            # actually means something, as opposed to every incidental timeline click.
            temp_timestamp_state["seconds"] = player.get_time() / 1000
            draw_timeline()
            player.play()
            reclaim_focus_after_play()
        update_play_pause_icon()

    def toggle_play_pause_if_not_typing():
        # A space bar press meant to type an actual space into, say, the output suffix field
        # shouldn't ALSO toggle playback -- only Tk itself can safely ask which of its own
        # widgets currently has focus, which is exactly why this check lives here (on the Tk
        # thread, via root.after) rather than in the OS-level hook that triggers it -- see
        # run_clip_editor_space_bar_listener.
        #
        # Entry only, NOT Combobox (unlike reclaim_focus_now's own similar-looking check below,
        # which is answering a different question -- "is the user actively using this dropdown,
        # so don't yank focus away from it" -- and is deliberately left alone). Confirmed live:
        # every Combobox in this editor is read-only (never accepts typed text, so space could
        # never mean "type a space" there in the first place), yet root.focus_get() kept
        # reporting one of them as focused on EVERY single space bar press, permanently blocking
        # the toggle -- because Tk's own focus bookkeeping is a purely internal record of whatever
        # Tk widget had focus last, and clicking into the embedded VLC video (a foreign HWND Tk
        # has no knowledge of at all) never updates it. In practice that leftover value was
        # essentially permanent, not a fleeting one worth guarding against.
        # ttk.Combobox is itself a SUBCLASS of ttk.Entry (confirmed directly: ttk.Combobox.__mro__
        # includes both tkinter.ttk.Entry and tkinter.Entry) -- a previous version of this check
        # excluded ttk.Combobox from the isinstance tuple thinking that was enough, but
        # isinstance(some_combobox, ttk.Entry) was ALREADY True on its own via that inheritance,
        # so removing it from the tuple changed nothing at all; confirmed live, this is exactly
        # why every space bar press kept logging the same "Combobox currently has focus" skip
        # after that fix had supposedly shipped. Excluding it explicitly (not just omitting it
        # from the tuple) is what actually works.
        widget = root.focus_get()
        if isinstance(widget, (tk.Entry, ttk.Entry)) and not isinstance(widget, ttk.Combobox):
            logging.info("Clip editor: space bar ignored -- %r currently has focus.", widget)
            return
        logging.info("Clip editor: space bar toggling play/pause (focus_get()=%r).", widget)
        toggle_play_pause()

    def on_space_bar_pressed():
        # Called directly from the space bar hook's raw OS callback thread -- root.after(0, ...)
        # is the thread-safe hand-off back to Tk (the same pattern set_status/etc. already use
        # from background threads elsewhere in this editor), never call Tk/VLC from here directly.
        try:
            root.after(0, toggle_play_pause_if_not_typing)
        except tk.TclError:
            pass

    def start_space_bar_listener():
        try:
            editor_window_handle = platform_common.resolve_editor_top_level_window(root.winfo_id())
        except Exception:
            logging.exception("Clip editor: could not resolve the editor window for the space bar hotkey.")
            return
        logging.info(
            "Clip editor: resolved editor window %s (root.winfo_id()=%s) for the space bar hotkey.",
            editor_window_handle, root.winfo_id(),
        )
        threading.Thread(
            target=run_clip_editor_space_bar_listener,
            args=(editor_window_handle, on_space_bar_pressed, space_bar_stop_event),
            daemon=True,
        ).start()

    def reclaim_focus_now():
        widget = root.focus_get()
        if not isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
            root.focus_set()

    def reclaim_focus_on_click(_event):
        # The video preview is embedded via a raw native HWND (set_hwnd), which Tk doesn't own
        # at all -- once real Win32 keyboard focus lands there, <space> above never fires again,
        # since the keystroke never reaches Tk's own event loop in the first place. Reclaiming on
        # every left-click anywhere in the window (bound once here, at the toplevel, rather than
        # on each individual button/canvas) covers clicking away from the video. This
        # deliberately does NOT run on a timer: an earlier version re-forced focus every 200ms
        # unconditionally, which visibly stuttered the video itself -- repeatedly yanking window
        # focus while VLC's D3D11 hardware decoder is actively rendering is a well-known way to
        # disrupt it.
        reclaim_focus_now()

    root.bind("<Button-1>", reclaim_focus_on_click, add="+")

    def reclaim_focus_after_play():
        # A click on the video itself was confirmed to steal focus even before this fix existed
        # -- but libvlc's own player also appears to (re)grab it shortly after playback actually
        # starts rendering, regardless of what triggered play(), which would silently undo an
        # immediate reclaim done in the same call. One short delayed retry after every play()
        # call specifically (not a repeating timer, so no risk of the stutter above) catches that.
        root.after(150, reclaim_focus_now)

    def seek_relative(seconds):
        if not state["path"]:
            return
        new_time = player.get_time() + seconds * 1000
        length = player.get_length()
        if length > 0:
            new_time = min(new_time, length)
        player.set_time(max(0, new_time))
        draw_timeline()

    def consume_temp_timestamp():
        # "Temporary": once Start or End actually uses it, it's gone -- otherwise a later,
        # unrelated Start/End press would silently reuse a click from a while ago instead of
        # falling back to the live playhead like it always used to.
        seconds = temp_timestamp_state["seconds"]
        if seconds is not None:
            temp_timestamp_state["seconds"] = None
            draw_timeline()
            return seconds
        return player.get_time() / 1000

    def set_start():
        if state["path"]:
            start_var.set(format_timestamp(consume_temp_timestamp()))

    def set_end():
        if state["path"]:
            end_var.set(format_timestamp(consume_temp_timestamp()))

    def do_trim():
        if not state["path"]:
            logging.warning("Clip editor: Trim Clip clicked with no recording open.")
            status_label.config(fg=END_MARKER_COLOR, text="Open a recording first.")
            return
        try:
            start_seconds = parse_timestamp(start_var.get())
            end_seconds = parse_timestamp(end_var.get())
        except ValueError as exc:
            logging.warning("Clip editor: could not start trim -- %s", exc)
            status_label.config(fg=END_MARKER_COLOR, text=str(exc))
            return
        error = validate_trim_range(start_seconds, end_seconds, state["duration"] or None)
        if error:
            logging.warning("Clip editor: could not start trim -- %s", error)
            status_label.config(fg=END_MARKER_COLOR, text=error)
            return

        ffmpeg_path = resolve_ffmpeg_path("ffmpeg")
        if not ffmpeg_path:
            logging.error("Clip editor: could not start trim -- ffmpeg not found.")
            status_label.config(
                fg=END_MARKER_COLOR,
                text="ffmpeg isn't installed -- install it from Settings > Post-Processing, then try again.",
            )
            return

        format_choice = format_var.get()
        output_ext = None if format_choice == CLIP_EDITOR_OUTPUT_FORMATS[0] else format_choice
        output_path = compute_trim_output_path(
            state["path"], clip_editor_config.get("output_folder") or None, output_ext=output_ext,
            suffix=clip_editor_config.get("output_suffix", "_trimmed"),
        )
        delete_original = clip_editor_config.get("delete_original_after_trim", False)
        # Precise (re-encode) is the default -- a plain stream-copy ("Fast") trim can leave a
        # broken leading frame at the cut point on OBS's open-GOP encodes (confirmed live: shows
        # as a glitch in strict decoders like VLC, even though more forgiving ones like Premiere
        # hide it). Most users would rather wait a bit longer than risk that, so Fast is now an
        # opt-in default tucked into Settings instead of a per-trim choice in the editor itself.
        precise = clip_editor_config.get("trim_mode", "precise") != "fast"
        scale_height = get_quality_scale_height(quality_var.get())
        target_size_mb = None
        if limit_size_var.get():
            try:
                target_size_mb = float(target_size_var.get())
                if target_size_mb <= 0:
                    raise ValueError("must be positive")
            except ValueError:
                logging.warning("Clip editor: could not start trim -- invalid target size %r.", target_size_var.get())
                status_label.config(fg=END_MARKER_COLOR, text="Target size must be a positive number of MB.")
                return
        n = routing_state["track_count"]
        audio_routing = None
        muted_destinations = None
        if n:
            routing = {}
            for dest in range(1, n + 1):
                sources = [src for src, var in routing_state["routing_vars"][dest].items() if var.get()]
                if sources:
                    routing[dest] = sources
            muted = [dest for dest, var in routing_state["mute_vars"].items() if var.get()]
            identity_routing = {t: [t] for t in range(1, n + 1)}
            if routing != identity_routing or muted:
                audio_routing = routing
                muted_destinations = muted
        gains_db = None
        if n:
            gains = {}
            invalid_gain_cells = []
            for dest in range(1, n + 1):
                dest_gains = {}
                for src, var in routing_state["gain_vars"][dest].items():
                    text = var.get().strip()
                    if not text:
                        continue
                    try:
                        value = float(text)
                    except ValueError:
                        invalid_gain_cells.append(f"Output {dest}, track {src}")
                        continue
                    if value:
                        dest_gains[src] = value
                if dest_gains:
                    gains[dest] = dest_gains
            if invalid_gain_cells:
                logging.warning(
                    "Clip editor: could not start trim -- invalid gain (dB) value for %s.",
                    ", ".join(invalid_gain_cells),
                )
                status_label.config(
                    fg=END_MARKER_COLOR, text=f"Invalid gain (dB) value for: {', '.join(invalid_gain_cells)}.",
                )
                return
            if gains:
                gains_db = gains
        source_path = state["path"]

        # Measures a representative window starting at the trim's own start point -- but NOT
        # capped to the trim's own (possibly very short) duration. Confirmed live: a ~5-second
        # trim gave the correlation algorithm too little audio to confidently match against on
        # ANY track, silently falling back to the configured default for all of them (which is
        # itself known to be inaccurate for at least some tracks, e.g. Discord's own jitter-
        # buffer pipeline needs a completely different correction than a locally-rendered game).
        # The underlying per-track lag is a property of the RECORDING (WASAPI capture mechanics),
        # not of whatever range happens to get exported, so measuring further into the source
        # file than just the trimmed selection is exactly as valid -- same principle as "if
        # latency is constant, measure once and apply everywhere" already relied on elsewhere
        # here. AUDIO_SYNC_FIX_MIN_MEASUREMENT_SECONDS gives the majority-agreement check (see
        # measure_waveform_lag_ms) a real chance regardless of how short the actual export is.
        fix_sync_enabled = fix_sync_var.get() and bool(fix_sync_tracks) and bool(fix_sync_reference_track)
        measure_start = start_seconds
        measure_duration = max(end_seconds - start_seconds, AUDIO_SYNC_FIX_MIN_MEASUREMENT_SECONDS)
        measure_duration = min(measure_duration, AUDIO_SYNC_FIX_MAX_MEASUREMENT_SECONDS)
        source_duration = state.get("duration") or 0
        if source_duration > 0:
            measure_duration = min(measure_duration, max(source_duration - measure_start, 0))
        configured_shift_ms = obs_config.get(
            "process_audio_capture_sync_offset_ms", DEFAULT_PROCESS_AUDIO_CAPTURE_SYNC_OFFSET_MS,
        )

        def set_status(text, color=MUTED_TEXT_COLOR):
            def update():
                if root.winfo_exists():
                    status_label.config(fg=color, text=text)
            try:
                root.after(0, update)
            except tk.TclError:
                pass

        # Fully releasing VLC's hold on the source file -- not just pausing it -- before handing
        # that same file to ffmpeg (for measuring and/or trimming). Confirmed live: the identical
        # measurement window (same file, same offsets, same tracks), run through this exact code
        # path, returned a confident result when reproduced standalone but an empty one inside the
        # real app, even though extraction itself reported success both times -- consistent with a
        # paused-but-still-open VLC input continuing to prefetch/read the file in the background
        # and quietly corrupting or truncating what ffmpeg reads back, in a way that a bare
        # pause() (which leaves VLC's demuxer/input open) doesn't rule out. player.stop() actually
        # closes that input. The preview's position is restored afterward (see finish() below),
        # reopening the file the same way load_file()/on_preview_quality_selected() already do --
        # but always left PAUSED there regardless of whether it was playing before this started,
        # rather than resuming autoplay, since a freshly exported clip is exactly the moment
        # someone's about to go check the output file, not keep watching the source play through.
        reopen_seconds = player.get_time() / 1000 if state["path"] else 0.0
        player.stop()
        update_play_pause_icon()

        set_status(f"Trimming to {os.path.basename(output_path)}...")
        trim_button.config(state="disabled")
        # Label-only -- closing the editor while a trim is running doesn't actually stop the
        # ffmpeg subprocess (see finish()'s "editor was closed while this trim was still running"
        # comment below); "Cancel" just tells the user honestly that leaving now means abandoning
        # the trim's own window rather than seeing it finish, not that it aborts the encode.
        close_button.config(text="✕ Cancel")
        progress.config(mode="indeterminate")
        progress.pack(fill="x", padx=10, pady=(0, 6), before=status_label)
        progress.start(12)

        def set_progress_determinate(fraction):
            def update():
                if root.winfo_exists():
                    if str(progress["mode"]) != "determinate":
                        progress.stop()
                        progress.config(mode="determinate", maximum=100)
                    progress["value"] = max(0.0, min(1.0, fraction)) * 100
            try:
                root.after(0, update)
            except tk.TclError:
                pass

        def on_trim_progress(phase_text, fraction):
            set_status(f"{phase_text}... {int(round(fraction * 100))}%")
            set_progress_determinate(fraction)

        def worker():
            audio_shift_ms_by_track = None
            if fix_sync_enabled:
                set_status("Measuring audio sync...")
                # Guards on_progress below against updating the status label with a stale
                # "Measuring track N..." message if the measurement thread is abandoned (see the
                # watchdog below) but keeps running in the background and eventually gets around
                # to calling it anyway -- by then the trim itself may already be running or done,
                # and overwriting THAT status with old measurement progress would be confusing.
                measurement_abandoned = {"value": False}

                def on_progress(index, total, track):
                    if not measurement_abandoned["value"]:
                        set_status(f"Measuring audio sync -- track {track} ({index + 1} of {total})...")

                # Run the measurement on its OWN thread and give up waiting on it past a hard,
                # unconditional ceiling -- confirmed live (twice) that measure_clip_audio_sync_
                # shifts_ms's own internal timeouts (each ffmpeg extraction's subprocess.run
                # timeout, plus an overall budget checked between tracks) were not enough to keep
                # this from appearing stuck in the real packaged app even on a short clip, for
                # reasons never conclusively root-caused (every standalone reproduction outside
                # the app ran in 1-3 seconds). Thread.join(timeout=...) doesn't depend on
                # subprocess/OS process semantics being correct the way those internal timeouts
                # do, so it's a genuinely independent backstop: if the measurement thread hasn't
                # finished by AUDIO_SYNC_MEASUREMENT_HARD_TIMEOUT_SECONDS, this simply stops
                # waiting and proceeds with the configured fallback for every track. The
                # measurement thread itself is left to finish (or not) in the background --
                # Python can't forcibly kill a thread, but it's a daemon thread, so it can never
                # block the app from closing, and its result is just discarded if it does
                # eventually land.
                measurement_result = {}

                def measure_worker():
                    # Explicitly caught and logged -- confirmed live that an uncaught exception in
                    # a background thread is otherwise swallowed with NO trace whatsoever in this
                    # app's packaged (--noconsole) build: Python's default thread-exception hook
                    # writes to sys.stderr, which this build has none of, so it goes nowhere. This
                    # was the real explanation for repeated real-world "empty result, no warnings
                    # logged at all" reports that never reproduced standalone -- there was a real
                    # exception happening every time, just one nothing ever surfaced.
                    try:
                        measurement_result["value"] = measure_clip_audio_sync_shifts_ms(
                            ffmpeg_path, source_path, fix_sync_reference_track, fix_sync_tracks,
                            measure_start, measure_duration, progress_callback=on_progress,
                        )
                    except Exception:
                        logging.exception(
                            "Clip editor: audio sync measurement for %s crashed -- falling back "
                            "to the configured default for every track.",
                            os.path.basename(source_path),
                        )
                        measurement_result["value"] = {}

                measure_thread = threading.Thread(target=measure_worker, daemon=True)
                measure_thread.start()
                measure_thread.join(timeout=AUDIO_SYNC_MEASUREMENT_HARD_TIMEOUT_SECONDS)
                if measure_thread.is_alive():
                    measurement_abandoned["value"] = True
                    logging.warning(
                        "Clip editor: audio sync measurement for %s did not finish within %.0fs -- "
                        "giving up on it and using the configured %sms default for every track "
                        "instead. (It may still finish in the background; its result is discarded.)",
                        os.path.basename(source_path), AUDIO_SYNC_MEASUREMENT_HARD_TIMEOUT_SECONDS,
                        configured_shift_ms,
                    )
                    measured = {}
                else:
                    measured = measurement_result.get("value", {})
                    logging.info(
                        "Clip editor: audio sync measurement for %s: %s (configured fallback %sms for "
                        "any track not confidently measured).",
                        os.path.basename(source_path), measured, configured_shift_ms,
                    )
                # Falls back to the app's own configured/calibrated offset for any track that
                # wasn't confidently measured (e.g. it had no real activity in the measured
                # window) -- the same known-good default this feature is meant to improve on,
                # rather than leaving that specific track unshifted.
                audio_shift_ms_by_track = {
                    track: measured.get(track, configured_shift_ms) for track in fix_sync_tracks
                }
                set_status(f"Trimming to {os.path.basename(output_path)}...")

            success = trim_clip(
                source_path, start_seconds, end_seconds, output_path, ffmpeg_path=ffmpeg_path,
                precise=precise, delete_original=delete_original, icon=icon,
                notifications_config=notifications_config, scale_height=scale_height,
                target_size_mb=target_size_mb, audio_routing=audio_routing,
                muted_destinations=muted_destinations, audio_shift_ms_by_track=audio_shift_ms_by_track,
                gains_db=gains_db, progress_callback=on_trim_progress,
            )

            def finish():
                if not root.winfo_exists():
                    # Editor was closed while this trim was still running -- trim_clip() already
                    # logged the result and fired a toast notification above, so the user still
                    # finds out; there's just no window left to update here. Nothing else to do.
                    return
                progress.stop()
                progress.pack_forget()
                trim_button.config(state="normal")
                close_button.config(text="✕ Close")
                if success:
                    status_label.config(fg=START_MARKER_COLOR, text=f"Saved to {output_path}")
                else:
                    status_label.config(fg=END_MARKER_COLOR, text="Trim failed -- see the log for details.")

                # Reopens the same source file player.stop() released above, restoring the
                # position it had before this trim started -- but always left paused (see the
                # comment where player.stop() was called above), regardless of whether it was
                # playing before. Skipped if the user switched to a different file while this was
                # running (state["path"] no longer matches), or if this trim deleted the original
                # (delete_original_after_trim).
                if state["path"] == source_path and not delete_original and os.path.isfile(source_path):
                    media = instance.media_new(source_path)
                    for option in CLIP_EDITOR_PREVIEW_QUALITY_MEDIA_OPTIONS.get(preview_quality_var.get(), []):
                        media.add_option(option)
                    player.set_media(media)
                    player.audio_set_volume(volume_var.get())
                    player.play()
                    reclaim_focus_after_play()

                    def restore_after_trim(attempts=0):
                        if not root.winfo_exists() or state["path"] != source_path:
                            return
                        if player.get_length() > 0:
                            player.set_time(int(max(0.0, reopen_seconds) * 1000))
                            draw_timeline()
                            pause_once_ready()
                        elif attempts < 50:
                            root.after(100, lambda: restore_after_trim(attempts + 1))

                    def pause_once_ready(attempts=0):
                        if not root.winfo_exists() or state["path"] != source_path:
                            return
                        if player.get_state() == vlc_module.State.Playing:
                            player.pause()
                            update_play_pause_icon()
                        elif attempts < 50:
                            root.after(100, lambda: pause_once_ready(attempts + 1))

                    root.after(150, restore_after_trim)

            try:
                root.after(0, finish)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def poll():
        if not root.winfo_exists():
            return
        if state["path"]:
            length = player.get_length()
            if length > 0:
                state["duration"] = length / 1000
            if not state["tracks_loaded"] and player.audio_get_track_count() > 0:
                refresh_audio_tracks()
            if not state["markers_loaded"]:
                refresh_markers()
            ensure_playhead_visible()
            draw_timeline()
            update_play_pause_icon()
            time_label.config(
                text=f"{format_timestamp(player.get_time() / 1000)} / {format_timestamp(state['duration'])}"
            )
        root.after(200, poll)

    # Draws the empty track immediately so the timeline is visible from the moment the editor
    # opens, rather than waiting for a <Configure> event or the first file load -- relying on
    # <Configure> alone left the canvas blank at startup in testing since it can fire before the
    # window's real geometry has settled.
    root.update_idletasks()
    draw_timeline()
    root.after(200, poll)
    start_space_bar_listener()


def main():
    config = load_config()

    handlers = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if config.get("log_file"):
        log_path = config["log_file"]
        if not os.path.isabs(log_path):
            log_path = os.path.join(SCRIPT_DIR, log_path)
        handlers.append(LineCappedFileHandler(log_path))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    cleanup_orphaned_pyinstaller_temp_dirs()

    status = {"text": "Starting...", "recording": False}
    stop_event = threading.Event()

    monitors = get_monitor_rects()
    overlay_config = config.get("overlay", {})
    overlay_state = {
        "monitor_index": resolve_default_overlay_monitor_index(monitors, overlay_config),
    }
    start_with_overlay = overlay_config.get("start_with_overlay", False)
    if start_with_overlay and overlay_state["monitor_index"] is None:
        # Mirrors toggle_audio_levels()'s own auto-pick below -- turning this on at startup with
        # no monitor chosen would otherwise enable the levels overlay with nowhere to show it.
        overlay_state["monitor_index"] = default_overlay_monitor_index(monitors)
    audio_state = {"enabled": start_with_overlay, "levels": {}}
    recording_state = {}
    reset_recording_state(recording_state)
    runtime_state = {"obs_client": None}
    editor_state = {"open": False}
    clip_editor_state = {"open": False}

    def on_quit(icon, menu_item):
        logging.info("Quit requested from tray icon.")
        stop_event.set()

    def do_restart():
        logging.info("Restarting app to apply updated settings.")
        try:
            # A frozen build's PyInstaller runtime hook points TCL_LIBRARY/TK_LIBRARY at *this*
            # process's onefile extraction folder (sys._MEIPASS). subprocess.Popen inherits the
            # environment by default, so without stripping these, the relaunched child starts
            # out pointed at a temp folder that gets deleted the moment this process exits --
            # its own copy of that same runtime hook only overwrites them if its own extraction
            # folder already exists at that point, so the stale inherited value can win the
            # race and crash Tk() with a "can't find init.tcl" error. Dropping them here forces
            # the child to always compute its own, regardless of that timing.
            env = os.environ.copy()
            env.pop("TCL_LIBRARY", None)
            env.pop("TK_LIBRARY", None)
            if getattr(sys, "frozen", False):
                subprocess.Popen([sys.executable], cwd=SCRIPT_DIR, env=env)
            else:
                subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=SCRIPT_DIR, env=env)
        except Exception:
            logging.exception("Failed to relaunch after config save.")
        stop_event.set()

    def on_edit_settings(icon, menu_item):
        open_config_editor_window(editor_state, do_restart, overlay_state, icon)

    def on_edit_clips(icon, menu_item):
        open_clip_editor_window(clip_editor_state, config, recording_state, overlay_state, icon)

    def on_open_recordings_folder(icon, menu_item):
        client = runtime_state.get("obs_client")
        folder = None
        if client:
            try:
                folder = client.get_record_directory().record_directory
            except Exception as exc:
                logging.warning("Could not get OBS recording directory: %s", exc)
        if not folder:
            logging.warning("Recordings folder unknown (OBS not connected yet).")
            return
        try:
            os.startfile(folder)
        except OSError as exc:
            logging.error("Could not open recordings folder %s: %s", folder, exc)

    def on_open_log_file(icon, menu_item):
        log_path = config.get("log_file")
        if not log_path:
            logging.warning("No log file configured.")
            return
        if not os.path.isabs(log_path):
            log_path = os.path.join(SCRIPT_DIR, log_path)
        if not os.path.isfile(log_path):
            logging.warning("Log file does not exist yet: %s", log_path)
            return
        try:
            os.startfile(log_path)
        except OSError as exc:
            logging.error("Could not open log file %s: %s", log_path, exc)

    def on_save_replay(icon, menu_item):
        client = runtime_state.get("obs_client")
        if not client:
            logging.warning("Save Replay Buffer requested but OBS is not connected.")
            return
        threading.Thread(target=save_replay_buffer, args=(client,), daemon=True).start()

    def on_buffered_split(icon, menu_item):
        client = runtime_state.get("obs_client")
        if not client:
            logging.warning("Split Recording requested but OBS is not connected.")
            return
        buffer_seconds = config.get("obs", {}).get("manual_split", {}).get("buffer_seconds", 0)
        threading.Thread(target=trigger_buffered_split, args=(client, buffer_seconds), daemon=True).start()

    def select_monitor(index):
        def action(icon, menu_item):
            overlay_state["monitor_index"] = index
            logging.info("Overlay monitor set to: %s", "Off" if index is None else monitors[index]["label"])
        return action

    def is_selected(index):
        def checked(menu_item):
            return overlay_state["monitor_index"] == index
        return checked

    def toggle_audio_levels(icon, menu_item):
        audio_state["enabled"] = not audio_state["enabled"]
        logging.info("Audio mixer levels overlay %s", "enabled" if audio_state["enabled"] else "disabled")
        # The levels only ever show up inside the floating overlay -- turning this on while the
        # overlay itself is still "Off" would otherwise have no visible effect at all, so pick a
        # monitor for it automatically instead of leaving the user to find "Overlay Monitor"
        # separately. Never overrides a monitor already chosen, and never turns the overlay back
        # off when levels are disabled (the user may still want the plain status overlay).
        if audio_state["enabled"] and overlay_state["monitor_index"] is None:
            index = default_overlay_monitor_index(monitors)
            if index is not None:
                overlay_state["monitor_index"] = index
                logging.info("Overlay monitor set to: %s", monitors[index]["label"])

    def audio_levels_checked(menu_item):
        return audio_state["enabled"]

    def obs_is_currently_running():
        return is_obs_running(config["obs"]["process_name"], get_running_processes())

    def obs_toggle_text(menu_item):
        return "Kill OBS" if obs_is_currently_running() else "Start OBS"

    def on_obs_toggle(icon, menu_item):
        if obs_is_currently_running():
            logging.info("Kill OBS requested from tray icon.")
            # Give the overlay instant feedback instead of waiting up to one poll_interval for
            # the watcher thread to notice the connection died -- it'll naturally stay cleared
            # since nothing is running to feed it, until OBS (or the watcher's own idle
            # reconnect) is back.
            audio_state["levels"] = {}
            threading.Thread(
                target=kill_process_by_name, args=(config["obs"]["process_name"],), daemon=True
            ).start()
        else:
            logging.info("Start OBS requested from tray icon.")
            threading.Thread(target=launch_obs, args=(config["obs"],), daemon=True).start()

    def on_restart_app(icon, menu_item):
        logging.info("Restart requested from tray icon.")
        do_restart()

    overlay_items = [pystray.MenuItem("Off", select_monitor(None), radio=True, checked=is_selected(None))]
    for i, mon in enumerate(monitors):
        overlay_items.append(
            pystray.MenuItem(mon["label"], select_monitor(i), radio=True, checked=is_selected(i))
        )

    menu_items = [
        pystray.MenuItem(lambda item: status["text"], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Overlay Monitor", pystray.Menu(*overlay_items)),
        pystray.MenuItem("Show Audio Mixer Levels", toggle_audio_levels, checked=audio_levels_checked),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Recordings Folder", on_open_recordings_folder),
        pystray.MenuItem("Open Log File", on_open_log_file),
        pystray.MenuItem(
            "Edit Clips...", on_edit_clips, enabled=lambda item: not clip_editor_state["open"]
        ),
        pystray.MenuItem(
            "Edit Settings...", on_edit_settings, enabled=lambda item: not editor_state["open"]
        ),
    ]
    if config.get("obs", {}).get("replay_buffer", {}).get("enabled"):
        menu_items.append(pystray.MenuItem("Save Replay Buffer", on_save_replay))
    if config.get("obs", {}).get("manual_split", {}).get("enabled"):
        menu_items.append(pystray.MenuItem("Split Recording File", on_buffered_split))
    menu_items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(obs_toggle_text, on_obs_toggle),
        pystray.MenuItem("Restart App", on_restart_app),
        pystray.MenuItem("Quit", on_quit),
    ]
    menu = pystray.Menu(*menu_items)

    icon = pystray.Icon(
        "OBSAutoRecorder",
        icon=build_tray_image(IDLE_COLOR),
        title="OBS Auto Recorder - Starting...",
        menu=menu,
    )

    watcher_thread = threading.Thread(
        target=watcher_loop,
        args=(icon, status, audio_state, recording_state, runtime_state, stop_event),
        daemon=True,
    )
    overlay_thread = threading.Thread(
        target=run_overlay, args=(monitors, overlay_state, audio_state, status, stop_event),
        kwargs={"mic_boost_config": config.get("obs", {}).get("mic_boost", {})}, daemon=True,
    )
    custom_keybinds = config.get("obs", {}).get("custom_keybinds", [])
    manual_split_buffer_seconds = config.get("obs", {}).get("manual_split", {}).get("buffer_seconds", 0)
    keybind_thread = None
    if any(kb.get("enabled", True) for kb in custom_keybinds):
        keybind_thread = threading.Thread(
            target=run_custom_keybind_listener,
            args=(
                custom_keybinds, lambda: runtime_state.get("obs_client"), lambda: manual_split_buffer_seconds,
                icon, config.get("notifications", {}), status,
            ),
            kwargs={"stop_event": stop_event},
            daemon=True,
        )

    def setup(icon):
        icon.visible = True
        watcher_thread.start()
        overlay_thread.start()
        if keybind_thread:
            keybind_thread.start()

    icon.run(setup=setup)

    # icon.run() returns as soon as icon.stop() fires (from watcher_loop's finally, once
    # stop_event is set), which can happen before the overlay thread's Tk mainloop -- polling
    # stop_event only every 120ms -- has actually destroyed its root and released Tcl/Tk. Onefile
    # PyInstaller builds extract their DLLs (including Tcl/Tk) to a temp dir and try to remove it
    # right after the interpreter shuts down; if that thread (and its Tcl interpreter) is still
    # winding down when the process exits, that removal can fail. Give both threads a moment to
    # actually finish instead of leaving them to be hard-killed mid-shutdown.
    stop_event.set()
    watcher_thread.join(timeout=5)
    overlay_thread.join(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Watcher stopped.")
