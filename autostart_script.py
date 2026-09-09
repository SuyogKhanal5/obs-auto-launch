import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk

import obsws_python as obsws
import psutil
import pystray
from PIL import Image, ImageDraw

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
    monitors = []

    MonitorEnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p
    )

    def callback(hmonitor, hdc, rect_ptr, data):
        r = rect_ptr.contents
        monitors.append({"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom})
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(callback), 0)

    monitors.sort(key=lambda m: (m["left"], m["top"]))
    for m in monitors:
        m["width"] = m["right"] - m["left"]
        m["height"] = m["bottom"] - m["top"]
        m["label"] = f"{m['width']}x{m['height']} monitor at ({m['left']}, {m['top']})"
    return monitors


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


def get_steam_install_path():
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


def get_steam_common_dirs(steam_config):
    if not steam_config.get("enabled", True):
        return []

    install_path = get_steam_install_path()
    if not install_path:
        logging.warning("Could not locate Steam install path via registry.")
        return []

    library_paths = [install_path]
    vdf_path = os.path.join(install_path, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf_path):
        with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        for match in re.finditer(r'"path"\s+"([^"]+)"', content):
            library_paths.append(os.path.normpath(match.group(1).replace("\\\\", "\\")))

    allowed_drives = steam_config.get("allowed_drives")
    if allowed_drives:
        allowed = {d.upper().rstrip("\\/:") for d in allowed_drives}
        library_paths = [p for p in library_paths if os.path.splitdrive(p)[0].upper().rstrip(":") in allowed]

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
    if not xbox_config.get("enabled", True):
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
    if not gog_config.get("enabled", True):
        return []

    exclude_keywords = [k.lower() for k in gog_config.get("exclude_keywords", [])]
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
        logging.info("No GOG Galaxy installs found in registry.")
        return []

    logging.info(
        "Watching GOG Galaxy installs: %s", ", ".join(g["display_name"] for g in games) if games else "none found"
    )
    return games


def get_epic_manifest_dir():
    root = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return os.path.join(root, "Epic", "EpicGamesLauncher", "Data", "Manifests")


def get_epic_installed_games(epic_config):
    if not epic_config.get("enabled", True):
        return []

    manifest_dir = get_epic_manifest_dir()
    if not os.path.isdir(manifest_dir):
        logging.info("No Epic Games manifests found at %s", manifest_dir)
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
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    sentinel_dir = os.path.join(appdata, "obs-studio", ".sentinel")
    if not os.path.isdir(sentinel_dir):
        return
    for entry in os.listdir(sentinel_dir):
        try:
            os.remove(os.path.join(sentinel_dir, entry))
        except OSError:
            pass


STARTUP_SHORTCUT_NAME = "OBSAutoRecorder.lnk"


def get_startup_shortcut_path():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", STARTUP_SHORTCUT_NAME)


def is_startup_shortcut_enabled():
    path = get_startup_shortcut_path()
    return bool(path and os.path.isfile(path))


def _ps_single_quote(value):
    return "'" + value.replace("'", "''") + "'"


def set_startup_shortcut_enabled(enabled):
    """Adds or removes a Startup-folder shortcut so the app launches at login.
    Only works from the built exe (nothing standalone to point a shortcut at when
    running from source)."""
    path = get_startup_shortcut_path()
    if not path:
        logging.warning("Could not resolve the Startup folder; cannot manage the startup shortcut.")
        return False

    if not enabled:
        if os.path.isfile(path):
            try:
                os.remove(path)
                logging.info("Removed startup shortcut.")
            except OSError as exc:
                logging.error("Failed to remove startup shortcut: %s", exc)
                return False
        return True

    if not getattr(sys, "frozen", False):
        logging.warning("Cannot create a startup shortcut while running from source; use the built .exe.")
        return False

    target = sys.executable
    working_dir = os.path.dirname(target)
    ps_script = (
        "$WshShell = New-Object -ComObject WScript.Shell; "
        f"$Shortcut = $WshShell.CreateShortcut({_ps_single_quote(path)}); "
        f"$Shortcut.TargetPath = {_ps_single_quote(target)}; "
        f"$Shortcut.WorkingDirectory = {_ps_single_quote(working_dir)}; "
        "$Shortcut.Save()"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logging.error("Failed to create startup shortcut: %s", exc)
        return False
    if result.returncode != 0:
        logging.error("Failed to create startup shortcut: %s", result.stderr.strip())
        return False
    logging.info("Created startup shortcut at %s", path)
    return True


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


def maybe_transcode(path, transcode_config):
    if transcode_config.get("enabled") and path:
        threading.Thread(target=transcode_recording, args=(path, transcode_config), daemon=True).start()


def connect_obs_events(config, icon, status, audio_state, recording_state):
    ws_config = config["obs"]["websocket"]
    auto_split_config = config["obs"].get("auto_split", {})
    silent_config = config.get("cleanup", {}).get("flag_silent_recordings", {})
    transcode_config = config.get("post_record_transcode", {})
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

        recording_state["current_path"] = new_path
        recording_state["segment_start_time"] = time.time()
        reset_segment_audio_tracking(recording_state)

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
            peak = 0.0
            for channel in entry.get("inputLevelsMul") or []:
                if len(channel) >= 2:
                    peak = max(peak, channel[1])
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
    sync_multi_track_audio(client, obs_config.get("multi_track_audio", {}))
    apply_output_folder(client, obs_config.get("output_folder"))
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


def set_game_audio_capture_target(client, input_name, process_name):
    try:
        client.set_input_settings(
            input_name,
            {"window": f"::{process_name}", "priority": WINDOW_MATCH_PRIORITY_EXE_FALLBACK},
            True,
        )
        logging.info("Pointed '%s' audio capture at %s", input_name, process_name)
    except Exception as exc:
        logging.warning("Could not point '%s' audio capture at %s: %s", input_name, process_name, exc)


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


def sync_multi_track_audio(client, multi_track_config):
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


def start_replay_buffer(client):
    try:
        client.start_replay_buffer()
        logging.info("Replay buffer started.")
    except Exception as exc:
        logging.warning("Could not start replay buffer: %s", exc)


def stop_replay_buffer(client):
    try:
        client.stop_replay_buffer()
        logging.info("Replay buffer stopped.")
    except Exception as exc:
        logging.warning("Could not stop replay buffer: %s", exc)


def save_replay_buffer(client):
    try:
        client.save_replay_buffer()
        logging.info("Replay buffer saved.")
    except Exception as exc:
        logging.error("Could not save replay buffer: %s", exc)


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


def transcode_recording(input_path, transcode_config):
    ffmpeg_path = transcode_config.get("ffmpeg_path", "ffmpeg")
    args = transcode_config.get("args", ["-c:v", "libx264", "-crf", "23", "-c:a", "aac"])
    suffix = transcode_config.get("suffix", "_compressed")
    delete_original = transcode_config.get("delete_original", False)

    directory = os.path.dirname(input_path)
    base, ext = os.path.splitext(os.path.basename(input_path))
    output_path = os.path.join(directory, f"{base}{suffix}{ext}")

    cmd = [ffmpeg_path, "-y", "-i", input_path] + list(args) + [output_path]
    logging.info("Transcoding %s with ffmpeg...", os.path.basename(input_path))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        logging.error("ffmpeg not found at '%s'; skipping post-record transcode.", ffmpeg_path)
        return
    if result.returncode != 0:
        logging.error("ffmpeg transcode failed for %s: %s", input_path, result.stderr[-2000:])
        return
    logging.info("Transcoded to %s", os.path.basename(output_path))
    if delete_original:
        try:
            os.remove(input_path)
        except OSError as exc:
            logging.warning("Could not delete original after transcode: %s", exc)


def rename_with_game_prefix(output_path, game_display_name, split_part=None, silent=False, use_subfolder=False):
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

    maybe_transcode(new_path, config.get("post_record_transcode", {}))
    return True


def set_status(icon, status, text):
    status["text"] = text
    icon.title = f"OBS Auto Recorder - {text}"
    icon.icon = build_tray_image(current_color(status))


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
    obs_recovery_state = {"last_attempt": 0, "last_start_failure": 0}

    while not stop_event.is_set():
        processes = get_running_processes()

        if active_name is None:
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
                if obs_client and start_recording(obs_client):
                    active_name, active_pid = name, pid
                    active_display_name = display_name
                    recording_state["display_name"] = active_display_name
                    status["recording"] = True
                    set_status(icon, status, f"Recording {active_display_name}")
                    notify(icon, notifications_config, "Recording started", active_display_name)
                    if game_audio_config.get("enabled"):
                        set_game_audio_capture_target(obs_client, game_audio_config["input_name"], name)
                    if replay_buffer_config.get("enabled"):
                        start_replay_buffer(obs_client)
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
                    if replay_buffer_config.get("enabled"):
                        stop_replay_buffer(obs_client)
                    kept = stop_recording(obs_client, icon, active_display_name, recording_state, config)
                    if kept:
                        notify(icon, notifications_config, "Recording stopped", active_display_name or active_name)
                active_name, active_pid, active_display_name = None, None, None
                reset_recording_state(recording_state)
                status["recording"] = False
                set_status(icon, status, "Watching")

        stop_event.wait(poll_interval)

    if active_name is not None and obs_client:
        logging.info("Quit requested while recording; stopping recording.")
        if replay_buffer_config.get("enabled"):
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


def run_overlay(monitors, overlay_state, audio_state, status, stop_event):
    root = tk.Tk()
    root.withdraw()

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
        show = audio_state["enabled"] and status["recording"]
        index = overlay_state["monitor_index"] if show else None
        levels = audio_state["levels"]
        row_count = max(len(levels), 1)

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
            if not levels:
                meter_canvas.create_text(
                    8, METER_ROW_HEIGHT // 2, anchor="w", fill="#888888", text="No active audio sources"
                )
            else:
                for i, (name, peak) in enumerate(levels.items()):
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
        poll_dot()
        poll_meters()
        root.after(120, poll)

    root.after(120, poll)
    root.mainloop()


def parse_csv_field(text):
    return [item.strip() for item in text.split(",") if item.strip()]


def format_csv_field(items):
    return ", ".join(items or [])


def make_scrollable_tab(notebook, title):
    outer = tk.Frame(notebook)
    notebook.add(outer, text=title)
    canvas = tk.Canvas(outer, highlightthickness=0)
    scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas)

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
    tk.Label(parent, text=label_text, anchor="w").grid(row=row, column=0, sticky="w", padx=(10, 6), pady=4)
    entry = tk.Entry(parent, textvariable=var, width=width)
    entry.grid(row=row, column=1, sticky="we", padx=(0, 10), pady=4)
    return entry


def add_checkbox(parent, row, label_text, var, columnspan=2):
    tk.Checkbutton(parent, text=label_text, variable=var).grid(
        row=row, column=0, columnspan=columnspan, sticky="w", padx=10, pady=4
    )


def add_browse_button(parent, row, var, mode="file", filetypes=(("Executable", "*.exe"), ("All files", "*.*"))):
    def browse():
        path = filedialog.askopenfilename(filetypes=filetypes) if mode == "file" else filedialog.askdirectory()
        if path:
            var.set(os.path.normpath(path))

    tk.Button(parent, text="Browse...", command=browse).grid(row=row, column=2, padx=(0, 10), pady=4)


def add_section_label(parent, row, text):
    tk.Label(parent, text=text, font=("Segoe UI", 9, "bold")).grid(
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
COMMON_GAMES = [
    {"name": "League of Legends", "process_name": "league of legends.exe"},
    {"name": "Wizard101", "process_name": "WizardGraphicalClient.exe"},
    {"name": "Valorant", "process_name": "VALORANT-Win64-Shipping.exe"},
    {"name": "Warframe", "process_name": "Warframe.x64.exe"},
    {"name": "Minecraft: Java Edition", "process_name": "javaw.exe", "title_contains": "minecraft"},
    {"name": "Minecraft: Bedrock Edition", "process_name": "Minecraft.Windows.exe"},
    {"name": "Fortnite", "process_name": "FortniteClient-Win64-Shipping.exe"},
    {"name": "Apex Legends", "process_name": "r5apex.exe"},
    {"name": "Overwatch 2", "process_name": "Overwatch.exe"},
    {"name": "Counter-Strike 2", "process_name": "cs2.exe"},
    {"name": "Rocket League", "process_name": "RocketLeague.exe"},
    {"name": "Roblox", "process_name": "RobloxPlayerBeta.exe"},
    {"name": "Genshin Impact", "process_name": "GenshinImpact.exe"},
]


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
    tk.Label(parent, text="Watched game processes (exact exe name, e.g. cs2.exe)", anchor="w").pack(
        anchor="w", padx=10, pady=(10, 2)
    )
    frame = tk.Frame(parent)
    frame.pack(fill="x", padx=10, pady=(0, 10))
    listbox = tk.Listbox(frame, height=8, width=32, exportselection=False)
    listbox.pack(side="left", fill="both", expand=True)
    for g in initial_games:
        listbox.insert("end", g)

    controls = tk.Frame(frame)
    controls.pack(side="left", fill="y", padx=(10, 0))
    entry_var = tk.StringVar()
    tk.Entry(controls, textvariable=entry_var, width=22).pack(pady=(0, 4))

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

    tk.Button(controls, text="Add", command=add_game).pack(fill="x")
    tk.Button(controls, text="Remove Selected", command=remove_selected).pack(fill="x", pady=(4, 0))
    tk.Button(controls, text="Pick Running...", command=pick_from_running).pack(fill="x", pady=(4, 0))
    if on_pick_common:
        tk.Button(
            controls, text="Common Games...", command=lambda: on_pick_common(insert_unique, current_watched_lower)
        ).pack(fill="x", pady=(4, 0))
    return listbox


def build_watched_windows_editor(parent, initial_rows):
    tk.Label(
        parent, text="Window-title rules (for games sharing a generic process name, e.g. javaw.exe)", anchor="w"
    ).pack(anchor="w", padx=10, pady=(10, 2))

    header = tk.Frame(parent)
    header.pack(fill="x", padx=10)
    for text, w in (("Process name", 16), ("Title contains", 16), ("Display name", 16)):
        tk.Label(header, text=text, width=w, anchor="w").pack(side="left", padx=2)

    container = tk.Frame(parent)
    container.pack(fill="x", padx=10)
    rows = []

    def add_row(process="", title="", display=""):
        row_frame = tk.Frame(container)
        row_frame.pack(fill="x", pady=2)
        process_var = tk.StringVar(value=process)
        title_var = tk.StringVar(value=title)
        display_var = tk.StringVar(value=display)
        tk.Entry(row_frame, textvariable=process_var, width=16).pack(side="left", padx=2)
        tk.Entry(row_frame, textvariable=title_var, width=16).pack(side="left", padx=2)
        tk.Entry(row_frame, textvariable=display_var, width=16).pack(side="left", padx=2)
        entry = {"process": process_var, "title": title_var, "display": display_var}

        def pick():
            open_process_picker(
                parent, on_add=lambda names: process_var.set(names[0]) if names else None, multiselect=False
            )

        tk.Button(row_frame, text="Pick...", command=pick).pack(side="left", padx=2)

        def remove():
            row_frame.destroy()
            rows.remove(entry)

        tk.Button(row_frame, text="Remove", command=remove).pack(side="left", padx=4)
        rows.append(entry)

    for w in initial_rows:
        add_row(w.get("process_name", ""), w.get("title_contains", ""), w.get("display_name", ""))

    tk.Button(parent, text="+ Add Window Rule", command=lambda: add_row()).pack(anchor="w", padx=10, pady=(4, 10))
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


def build_multi_track_audio_editor(parent, initial_rows, get_ws_config):
    tk.Label(
        parent, text="Which input feeds which recording track, in the dedicated profile below", anchor="w"
    ).pack(anchor="w", padx=10, pady=(10, 2))

    header = tk.Frame(parent)
    header.pack(fill="x", padx=10)
    tk.Label(header, text="Input name (exact, as in OBS)", width=30, anchor="w").pack(side="left", padx=2)
    tk.Label(header, text="Track", width=6, anchor="w").pack(side="left", padx=2)

    container = tk.Frame(parent)
    container.pack(fill="x", padx=10)
    rows = []

    def add_row(input_name="", track=1):
        row_frame = tk.Frame(container)
        row_frame.pack(fill="x", pady=2)
        name_var = tk.StringVar(value=input_name)
        track_var = tk.StringVar(value=str(track))
        tk.Entry(row_frame, textvariable=name_var, width=30).pack(side="left", padx=2)
        ttk.Combobox(
            row_frame, textvariable=track_var, values=[str(i) for i in range(1, 7)],
            state="readonly", width=4,
        ).pack(side="left", padx=2)
        entry = {"input_name": name_var, "track": track_var}

        def pick():
            open_obs_input_picker(parent, get_ws_config, on_pick=lambda name: name_var.set(name))

        tk.Button(row_frame, text="Pick...", command=pick).pack(side="left", padx=2)

        def remove():
            row_frame.destroy()
            rows.remove(entry)

        tk.Button(row_frame, text="Remove", command=remove).pack(side="left", padx=4)
        rows.append(entry)

    for t in initial_rows:
        add_row(t.get("input_name", ""), t.get("track", 1))

    tk.Button(parent, text="+ Add Track Mapping", command=lambda: add_row()).pack(
        anchor="w", padx=10, pady=(4, 10)
    )
    return rows


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


def open_config_editor_window(editor_state, restart_callback):
    if editor_state.get("open"):
        logging.info("Settings editor is already open.")
        return
    editor_state["open"] = True

    def run():
        previous_default_root = tk._default_root
        try:
            _run_config_editor(restart_callback)
        except Exception:
            logging.exception("Settings editor crashed.")
        finally:
            tk._default_root = previous_default_root
            editor_state["open"] = False

    threading.Thread(target=run, daemon=True).start()


def _run_config_editor(restart_callback):
    config = load_config()

    root = tk.Tk()

    # The overlay thread already created its own persistent Tk() root at app startup,
    # which tkinter keeps as the process-wide "default root". Every StringVar/BooleanVar
    # created below without an explicit master binds to whatever _default_root is right
    # now, not to this window's own interpreter -- so without forcing it here, every
    # field would silently read/write the overlay's interpreter instead of this one's,
    # and every widget would show its default (blank/unchecked) state regardless of
    # config.json's actual values. open_config_editor_window() restores the previous
    # default root once this window closes (even if construction raises).
    tk._default_root = root

    root.title("OBS Auto Recorder - Settings")
    root.geometry("620x560")
    root.minsize(520, 420)

    notebook = ttk.Notebook(root)
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
    startup_label = "Launch automatically when Windows starts"
    if not is_frozen:
        startup_label += " (only available from the built .exe)"
    startup_checkbox = tk.Checkbutton(general_tab, text=startup_label, variable=startup_var)
    startup_checkbox.grid(row=3, column=0, columnspan=2, sticky="w", padx=10, pady=4)
    if not is_frozen:
        startup_checkbox.config(state="disabled")

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
    add_labeled_entry(launchers_tab, row, "Allowed drives (comma-separated, e.g. C, D)", steam_drives_var)
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
    tk.Label(obs_tab, text="Split by", anchor="w").grid(row=row, column=0, sticky="w", padx=(10, 6), pady=4)
    ttk.Combobox(
        obs_tab, textvariable=auto_split_by_var, values=["time", "size"], state="readonly", width=10
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

    multi_track_config = obs_config.get("multi_track_audio", {})
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
        anchor="w", justify="left", wraplength=520,
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

    multi_track_list_frame = tk.Frame(obs_tab)
    multi_track_list_frame.grid(row=row, column=0, columnspan=3, sticky="we")
    multi_track_rows = build_multi_track_audio_editor(
        multi_track_list_frame, multi_track_config.get("tracks", []), get_current_ws_config
    )
    row += 1

    replay_buffer_config = obs_config.get("replay_buffer", {})
    add_section_label(obs_tab, row, "Replay Buffer")
    row += 1
    replay_buffer_enabled_var = tk.BooleanVar(value=replay_buffer_config.get("enabled", False))
    add_checkbox(obs_tab, row, "Start/stop OBS's replay buffer with recording", replay_buffer_enabled_var)
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

    # --- Post-processing & Notifications ---
    post_tab = make_scrollable_tab(notebook, "Post-Processing")
    post_tab.columnconfigure(1, weight=1)
    transcode_config = config.get("post_record_transcode", {})
    row = 0
    add_section_label(post_tab, row, "Post-Record Transcode (ffmpeg)")
    row += 1
    transcode_enabled_var = tk.BooleanVar(value=transcode_config.get("enabled", False))
    add_checkbox(post_tab, row, "Run finished recordings through ffmpeg", transcode_enabled_var)
    row += 1
    transcode_ffmpeg_path_var = tk.StringVar(value=transcode_config.get("ffmpeg_path", "ffmpeg"))
    add_labeled_entry(post_tab, row, "ffmpeg path", transcode_ffmpeg_path_var)
    add_browse_button(post_tab, row, transcode_ffmpeg_path_var)
    row += 1
    transcode_args_var = tk.StringVar(
        value=" ".join(transcode_config.get("args", ["-c:v", "libx264", "-crf", "23", "-c:a", "aac"]))
    )
    add_labeled_entry(post_tab, row, "ffmpeg args (space-separated)", transcode_args_var, width=44)
    row += 1
    transcode_suffix_var = tk.StringVar(value=transcode_config.get("suffix", "_compressed"))
    add_labeled_entry(post_tab, row, "Output filename suffix", transcode_suffix_var)
    row += 1
    transcode_delete_original_var = tk.BooleanVar(value=transcode_config.get("delete_original", False))
    add_checkbox(post_tab, row, "Delete original after a successful transcode", transcode_delete_original_var)
    row += 1

    add_section_label(post_tab, row, "Notifications")
    row += 1
    notifications_enabled_var = tk.BooleanVar(value=config.get("notifications", {}).get("enabled", False))
    add_checkbox(post_tab, row, "Show Windows toast notifications for key events", notifications_enabled_var)
    row += 1

    # --- Save / Cancel ---
    status_label = tk.Label(root, text="", fg="#b00020", anchor="w")
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

        replay_buffer = obs.setdefault("replay_buffer", {})
        replay_buffer["enabled"] = replay_buffer_enabled_var.get()

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

        transcode = new_config.setdefault("post_record_transcode", {})
        transcode["enabled"] = transcode_enabled_var.get()
        transcode["ffmpeg_path"] = transcode_ffmpeg_path_var.get().strip() or "ffmpeg"
        transcode["args"] = transcode_args_var.get().split()
        transcode["suffix"] = transcode_suffix_var.get()
        transcode["delete_original"] = transcode_delete_original_var.get()

        notifications = new_config.setdefault("notifications", {})
        notifications["enabled"] = notifications_enabled_var.get()

        return new_config, errors

    def do_save(and_restart):
        new_config, errors = collect_config()
        if errors:
            status_label.config(text="; ".join(errors))
            return
        if not new_config.get("obs", {}).get("path"):
            status_label.config(text="'OBS executable path' is required")
            return
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

    button_bar = tk.Frame(root)
    button_bar.pack(fill="x", padx=10, pady=10)
    tk.Button(button_bar, text="Cancel", command=root.destroy).pack(side="right")
    tk.Button(button_bar, text="Save", command=lambda: do_save(False)).pack(side="right", padx=8)
    tk.Button(button_bar, text="Save and Restart", command=lambda: do_save(True)).pack(side="right")

    root.mainloop()


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

    status = {"text": "Starting...", "recording": False}
    stop_event = threading.Event()

    monitors = get_monitor_rects()
    overlay_state = {"monitor_index": None}
    audio_state = {"enabled": False, "levels": {}}
    recording_state = {}
    reset_recording_state(recording_state)
    runtime_state = {"obs_client": None}
    editor_state = {"open": False}

    def on_quit(icon, menu_item):
        logging.info("Quit requested from tray icon.")
        stop_event.set()

    def do_restart():
        logging.info("Restarting app to apply updated settings.")
        try:
            if getattr(sys, "frozen", False):
                subprocess.Popen([sys.executable], cwd=SCRIPT_DIR)
            else:
                subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=SCRIPT_DIR)
        except Exception:
            logging.exception("Failed to relaunch after config save.")
        stop_event.set()

    def on_edit_settings(icon, menu_item):
        open_config_editor_window(editor_state, do_restart)

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

    def audio_levels_checked(menu_item):
        return audio_state["enabled"]

    def on_kill_obs(icon, menu_item):
        logging.info("Kill OBS requested from tray icon.")
        threading.Thread(
            target=kill_process_by_name, args=(config["obs"]["process_name"],), daemon=True
        ).start()

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
        pystray.MenuItem("Show Audio Mixer Levels While Recording", toggle_audio_levels, checked=audio_levels_checked),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Recordings Folder", on_open_recordings_folder),
        pystray.MenuItem("Open Log File", on_open_log_file),
        pystray.MenuItem(
            "Edit Settings...", on_edit_settings, enabled=lambda item: not editor_state["open"]
        ),
    ]
    if config.get("obs", {}).get("replay_buffer", {}).get("enabled"):
        menu_items.append(pystray.MenuItem("Save Replay Buffer", on_save_replay))
    menu_items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Kill OBS", on_kill_obs),
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
        target=run_overlay, args=(monitors, overlay_state, audio_state, status, stop_event), daemon=True
    )

    def setup(icon):
        icon.visible = True
        watcher_thread.start()
        overlay_thread.start()

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
