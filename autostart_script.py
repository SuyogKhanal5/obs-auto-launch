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
                processes.append((name, proc.info.get("exe") or ""))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return processes


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


def find_target_process(watched_games, steam_common_dirs, exclude_keywords, processes):
    for name, exe in processes:
        if name.lower() in watched_games:
            return name, exe
    for name, exe in processes:
        if is_steam_game_exe(exe, steam_common_dirs, exclude_keywords):
            return name, exe
    return None, None


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


def is_process_running(name, exe, processes):
    exe_lower = exe.lower() if exe else None
    name_lower = name.lower()
    for proc_name, proc_exe in processes:
        if exe_lower:
            if proc_exe.lower() == exe_lower:
                return True
        elif proc_name.lower() == name_lower:
            return True
    return False


def is_obs_running(process_name, processes):
    process_name_lower = process_name.lower()
    return any(name.lower() == process_name_lower for name, _ in processes)


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


def connect_obs_events(ws_config, icon, status):
    try:
        event_client = obsws.EventClient(
            host=ws_config["host"],
            port=ws_config["port"],
            password=ws_config["password"],
            timeout=3,
        )
    except Exception as exc:
        logging.warning("Could not connect OBS event listener; split-flash disabled: %s", exc)
        return None

    def on_record_file_changed(data):
        logging.info("Recording file split detected: %s", getattr(data, "new_output_path", ""))
        status["flash_until"] = time.time() + SPLIT_FLASH_SECONDS
        icon.icon = build_tray_image(current_color(status))

        def revert():
            icon.icon = build_tray_image(current_color(status))

        timer = threading.Timer(SPLIT_FLASH_SECONDS, revert)
        timer.daemon = True
        timer.start()

    event_client.callback.register(on_record_file_changed)
    return event_client


def ensure_obs_ready(obs_config, processes, icon, status):
    if not is_obs_running(obs_config["process_name"], processes):
        if not launch_obs(obs_config):
            return None, None
    client = connect_obs(obs_config["websocket"])
    if not client:
        return None, None
    event_client = connect_obs_events(obs_config["websocket"], icon, status)
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


def stop_recording(client, game_display_name=None):
    try:
        resp = client.stop_record()
        logging.info("Recording stopped.")
    except Exception as exc:
        logging.error("Failed to stop recording: %s", exc)
        return

    output_path = getattr(resp, "output_path", None)
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


def set_status(icon, status, text):
    status["text"] = text
    icon.title = f"OBS Auto Recorder - {text}"
    icon.icon = build_tray_image(current_color(status))


def watcher_loop(icon, status, stop_event):
    try:
        _watcher_loop_impl(icon, status, stop_event)
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


def _watcher_loop_impl(icon, status, stop_event):
    config = load_config()
    watched_games = {g.lower() for g in config["watched_games"]}
    poll_interval = config.get("poll_interval_seconds", 1.5)
    obs_config = config["obs"]
    steam_config = config.get("steam", {})
    exclude_keywords = [k.lower() for k in steam_config.get("exclude_keywords", [])]
    steam_common_dirs = get_steam_common_dirs(steam_config)

    logging.info("Watching for processes: %s", ", ".join(sorted(watched_games)))
    set_status(icon, status, "Watching")

    obs_client = None
    active_name = None
    active_exe = None
    active_display_name = None

    while not stop_event.is_set():
        processes = get_running_processes()

        if active_name is None:
            name, exe = find_target_process(watched_games, steam_common_dirs, exclude_keywords, processes)
            if name:
                logging.info("Detected watched game: %s", exe or name)
                # obs_event_client is unused directly; kept referenced so its listener thread isn't GC'd.
                obs_client, obs_event_client = ensure_obs_ready(obs_config, processes, icon, status)
                if obs_client and start_recording(obs_client):
                    active_name, active_exe = name, exe
                    active_display_name = get_game_display_name(name, exe, steam_common_dirs)
                    status["recording"] = True
                    set_status(icon, status, f"Recording {active_display_name}")
        else:
            if not is_process_running(active_name, active_exe, processes):
                logging.info("%s has exited.", active_exe or active_name)
                if obs_client:
                    stop_recording(obs_client, active_display_name)
                active_name, active_exe, active_display_name = None, None, None
                status["recording"] = False
                set_status(icon, status, "Watching")

        stop_event.wait(poll_interval)

    if active_name is not None and obs_client:
        logging.info("Quit requested while recording; stopping recording.")
        stop_recording(obs_client, active_display_name)


OVERLAY_SIZE = 26
OVERLAY_MARGIN = 16


def run_overlay(monitors, overlay_state, status, stop_event):
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-toolwindow", True)
    except tk.TclError:
        pass
    root.withdraw()

    shown_index = [None]

    def poll():
        if stop_event.is_set():
            root.destroy()
            return

        index = overlay_state["monitor_index"]
        if index != shown_index[0]:
            shown_index[0] = index
            if index is None or index >= len(monitors):
                root.withdraw()
            else:
                mon = monitors[index]
                x = mon["right"] - OVERLAY_SIZE - OVERLAY_MARGIN
                y = mon["top"] + OVERLAY_MARGIN
                root.geometry(f"{OVERLAY_SIZE}x{OVERLAY_SIZE}+{x}+{y}")
                root.deiconify()

        if shown_index[0] is not None:
            root.configure(bg=color_to_hex(current_color(status)))

        root.after(250, poll)

    root.after(250, poll)
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

    overlay_items = [pystray.MenuItem("Off", select_monitor(None), radio=True, checked=is_selected(None))]
    for i, mon in enumerate(monitors):
        overlay_items.append(
            pystray.MenuItem(mon["label"], select_monitor(i), radio=True, checked=is_selected(i))
        )

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: status["text"], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Overlay Monitor", pystray.Menu(*overlay_items)),
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
        threading.Thread(target=watcher_loop, args=(icon, status, stop_event), daemon=True).start()
        threading.Thread(target=run_overlay, args=(monitors, overlay_state, status, stop_event), daemon=True).start()

    icon.run(setup=setup)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Watcher stopped.")
