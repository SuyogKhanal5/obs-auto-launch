import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes

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


IDLE_COLOR = (90, 90, 90, 255)
RECORDING_COLOR = (220, 30, 30, 255)
SPLIT_COLOR = (255, 210, 0, 255)
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


def is_steam_game_exe(exe_path, steam_common_dirs, exclude_keywords):
    if not exe_path:
        return False
    exe_lower = exe_path.lower()
    for common_dir in steam_common_dirs:
        prefix = common_dir if common_dir.endswith(os.sep) else common_dir + os.sep
        if exe_lower.startswith(prefix):
            return not any(kw in exe_lower for kw in exclude_keywords)
    return False


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


def get_epic_game_for_exe(exe_path, epic_games):
    if not exe_path:
        return None
    exe_lower = os.path.normpath(exe_path).lower()
    for game in epic_games:
        prefix = game["install_dir"] if game["install_dir"].endswith(os.sep) else game["install_dir"] + os.sep
        if exe_lower.startswith(prefix):
            return game["display_name"]
    return None


def find_target_process(
    watched_games, steam_common_dirs, exclude_keywords, epic_games, watched_windows, processes
):
    for name, exe, pid in processes:
        if name.lower() in watched_games:
            return name, exe, pid, None
    for name, exe, pid in processes:
        if is_steam_game_exe(exe, steam_common_dirs, exclude_keywords):
            return name, exe, pid, None
    for name, exe, pid in processes:
        epic_display_name = get_epic_game_for_exe(exe, epic_games)
        if epic_display_name:
            return name, exe, pid, epic_display_name
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


def get_game_display_name(name, exe, steam_common_dirs):
    if exe:
        exe_lower = exe.lower()
        for common_dir in steam_common_dirs:
            prefix = common_dir if common_dir.endswith(os.sep) else common_dir + os.sep
            if exe_lower.startswith(prefix):
                remainder = exe[len(prefix):]
                folder = remainder.split(os.sep)[0]
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


def connect_obs_events(ws_config, icon, status, audio_state, recording_state):
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

    def on_record_file_changed(data):
        new_path = getattr(data, "new_output_path", "")
        if new_path == recording_state.get("current_path"):
            # OBS emits RecordFileChanged twice for a single split; ignore the duplicate.
            return
        logging.info("Recording file split detected: %s", new_path)
        status["flash_until"] = time.time() + SPLIT_FLASH_SECONDS
        icon.icon = build_tray_image(current_color(status))

        old_path = recording_state.get("current_path")
        if old_path:
            rename_with_game_prefix(old_path, recording_state.get("display_name"))
        recording_state["current_path"] = new_path

        def revert():
            icon.icon = build_tray_image(current_color(status))

        timer = threading.Timer(SPLIT_FLASH_SECONDS, revert)
        timer.daemon = True
        timer.start()

    def on_input_volume_meters(data):
        levels = {}
        for entry in data.inputs:
            name = entry.get("inputName")
            if not name:
                continue
            peak = 0.0
            for channel in entry.get("inputLevelsMul") or []:
                if len(channel) >= 2:
                    peak = max(peak, channel[1])
            levels[name] = peak
        audio_state["levels"] = levels

    event_client.callback.register(on_record_state_changed)
    event_client.callback.register(on_record_file_changed)
    event_client.callback.register(on_input_volume_meters)
    return event_client


def ensure_obs_ready(obs_config, processes, icon, status, audio_state, recording_state):
    if not is_obs_running(obs_config["process_name"], processes):
        if not launch_obs(obs_config):
            return None, None
    client = connect_obs(obs_config["websocket"])
    if not client:
        return None, None
    event_client = connect_obs_events(obs_config["websocket"], icon, status, audio_state, recording_state)
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


def rename_with_game_prefix(output_path, game_display_name):
    if not output_path or not game_display_name:
        return

    output_path = os.path.normpath(output_path)
    directory = os.path.dirname(output_path)
    filename = os.path.basename(output_path)
    prefix = sanitize_filename_part(game_display_name)
    if not prefix:
        return

    new_path = os.path.join(directory, f"{prefix} - {filename}")
    retries, delay = 5, 1
    for attempt in range(1, retries + 1):
        try:
            os.rename(output_path, new_path)
            logging.info("Renamed recording to %s", os.path.basename(new_path))
            return
        except OSError as exc:
            if attempt < retries:
                time.sleep(delay)
                continue
            logging.error("Failed to rename recording file: %s", exc)


def stop_recording(client, game_display_name=None, recording_state=None):
    try:
        resp = client.stop_record()
        logging.info("Recording stopped.")
    except Exception as exc:
        logging.error("Failed to stop recording: %s", exc)
        return

    # OBS's StopRecord response reports the pre-split filename after a split has
    # occurred, so prefer the path we've tracked live from split/start events.
    tracked_path = recording_state.get("current_path") if recording_state else None
    output_path = tracked_path or getattr(resp, "output_path", None)
    rename_with_game_prefix(output_path, game_display_name)


def set_status(icon, status, text):
    status["text"] = text
    icon.title = f"OBS Auto Recorder - {text}"
    icon.icon = build_tray_image(current_color(status))


def watcher_loop(icon, status, audio_state, recording_state, stop_event):
    try:
        _watcher_loop_impl(icon, status, audio_state, recording_state, stop_event)
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


def _watcher_loop_impl(icon, status, audio_state, recording_state, stop_event):
    config = load_config()
    watched_games = {g.lower() for g in config["watched_games"]}
    watched_windows = config.get("watched_windows", [])
    poll_interval = config.get("poll_interval_seconds", 1.5)
    obs_config = config["obs"]
    steam_config = config.get("steam", {})
    exclude_keywords = [k.lower() for k in steam_config.get("exclude_keywords", [])]
    steam_common_dirs = get_steam_common_dirs(steam_config)
    epic_games = get_epic_installed_games(config.get("epic", {}))

    logging.info("Watching for processes: %s", ", ".join(sorted(watched_games)))
    if watched_windows:
        logging.info(
            "Watching for window titles: %s",
            ", ".join(f"{w['process_name']} containing '{w['title_contains']}'" for w in watched_windows),
        )
    set_status(icon, status, "Watching")

    obs_client = None
    active_name = None
    active_pid = None
    active_display_name = None

    while not stop_event.is_set():
        processes = get_running_processes()

        if active_name is None:
            name, exe, pid, display_override = find_target_process(
                watched_games, steam_common_dirs, exclude_keywords, epic_games, watched_windows, processes
            )
            if name:
                logging.info("Detected watched game: %s", exe or name)
                # obs_event_client is unused directly; kept referenced so its listener thread isn't GC'd.
                obs_client, obs_event_client = ensure_obs_ready(
                    obs_config, processes, icon, status, audio_state, recording_state
                )
                if obs_client and start_recording(obs_client):
                    active_name, active_pid = name, pid
                    active_display_name = display_override or get_game_display_name(name, exe, steam_common_dirs)
                    recording_state["display_name"] = active_display_name
                    status["recording"] = True
                    set_status(icon, status, f"Recording {active_display_name}")
        else:
            if not is_process_running(active_pid):
                logging.info("%s has exited.", active_display_name or active_name)
                if obs_client:
                    stop_recording(obs_client, active_display_name, recording_state)
                active_name, active_pid, active_display_name = None, None, None
                recording_state["display_name"] = None
                recording_state["current_path"] = None
                status["recording"] = False
                set_status(icon, status, "Watching")

        stop_event.wait(poll_interval)

    if active_name is not None and obs_client:
        logging.info("Quit requested while recording; stopping recording.")
        stop_recording(obs_client, active_display_name, recording_state)


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


def main():
    config = load_config()

    handlers = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if config.get("log_file"):
        log_path = config["log_file"]
        if not os.path.isabs(log_path):
            log_path = os.path.join(SCRIPT_DIR, log_path)
        handlers.append(logging.FileHandler(log_path))

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
    recording_state = {"display_name": None, "current_path": None}

    def on_quit(icon, menu_item):
        logging.info("Quit requested from tray icon.")
        stop_event.set()

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

    overlay_items = [pystray.MenuItem("Off", select_monitor(None), radio=True, checked=is_selected(None))]
    for i, mon in enumerate(monitors):
        overlay_items.append(
            pystray.MenuItem(mon["label"], select_monitor(i), radio=True, checked=is_selected(i))
        )

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: status["text"], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Overlay Monitor", pystray.Menu(*overlay_items)),
        pystray.MenuItem("Show Audio Mixer Levels While Recording", toggle_audio_levels, checked=audio_levels_checked),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(
        "OBSAutoRecorder",
        icon=build_tray_image(IDLE_COLOR),
        title="OBS Auto Recorder - Starting...",
        menu=menu,
    )

    def setup(icon):
        icon.visible = True
        threading.Thread(
            target=watcher_loop, args=(icon, status, audio_state, recording_state, stop_event), daemon=True
        ).start()
        threading.Thread(
            target=run_overlay, args=(monitors, overlay_state, audio_state, status, stop_event), daemon=True
        ).start()

    icon.run(setup=setup)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Watcher stopped.")
