"""OBS Auto Recorder installer.

A small, plain-language setup wizard: picks an install folder, locates OBS,
asks a few simple yes/no preferences, then copies the bundled app exe into
place, writes a starter config.json, and optionally creates a desktop icon
and a "start with Windows" shortcut. Built as its own standalone exe
(OBSAutoRecorderInstaller.exe) via PyInstaller, with OBSAutoRecorder.exe and
config.example.json embedded as data files.

Kept intentionally separate from autostart_script.py: it only needs a
handful of stdlib modules plus psutil, not the full app's dependency set.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import winreg
from tkinter import filedialog, messagebox

import psutil

APP_NAME = "OBS Auto Recorder"
EXE_NAME = "OBSAutoRecorder.exe"
DEFAULT_INSTALL_DIR = r"C:\Program Files\OBSAutoRecorder"
DEFAULT_OBS_PATHS = [
    r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
]


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


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
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "OBSAutoRecorder.lnk")


def find_obs_exe():
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


def is_obs_running():
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in ("obs64.exe", "obs32.exe"):
            return True
    return False


def is_app_running():
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name == EXE_NAME.lower():
            return True
    return False


def get_obs_websocket_config_path():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "obs-studio", "plugin_config", "obs-websocket", "config.json")


def try_configure_obs_websocket(password):
    """Best-effort: pre-fill OBS's own WebSocket plugin settings so the user doesn't
    have to open OBS and copy a password by hand. Returns (True, None) on success,
    (False, reason) if skipped/failed -- never raises, this is a bonus convenience,
    not something the rest of setup depends on."""
    if is_obs_running():
        return False, "OBS is currently running"

    path = get_obs_websocket_config_path()
    if not path:
        return False, "could not locate OBS's settings folder"

    obs_studio_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
    if not os.path.isdir(obs_studio_dir):
        return False, "OBS has not been run yet"

    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                ws_config = json.load(f)
        else:
            ws_config = {}
        ws_config["server_enabled"] = True
        ws_config["auth_required"] = True
        ws_config["server_password"] = password
        ws_config["server_port"] = 4455
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ws_config, f, indent=4)
        return True, None
    except (OSError, json.JSONDecodeError) as exc:
        return False, str(exc)


def build_config(template, obs_path, password, options):
    config = json.loads(json.dumps(template))
    config["watched_games"] = []
    config["watched_windows"] = []
    config["obs"]["path"] = obs_path or template["obs"]["path"]
    config["obs"]["websocket"]["password"] = password
    config["obs"]["auto_split"]["enabled"] = options["split_long_recordings"]
    config["cleanup"]["delete_short_clips"]["enabled"] = options["delete_short_clips"]
    config["notifications"]["enabled"] = options["notifications"]
    config["organize_into_game_subfolders"] = options["per_game_folders"]
    return config


def is_existing_install(install_dir):
    return os.path.isfile(os.path.join(install_dir, EXE_NAME))


def do_install(install_dir, obs_path, options, on_progress, write_config=True):
    on_progress(f"Creating {install_dir} ...")
    os.makedirs(install_dir, exist_ok=True)

    on_progress("Copying application files...")
    shutil.copy2(resource_path(EXE_NAME), os.path.join(install_dir, EXE_NAME))

    password = None
    obs_ws_configured = None
    obs_ws_reason = None
    if write_config:
        on_progress("Writing your settings...")
        with open(resource_path("config.example.json"), "r", encoding="utf-8") as f:
            template = json.load(f)
        password = secrets.token_urlsafe(12)
        config = build_config(template, obs_path, password, options)
        config_path = os.path.join(install_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)

        on_progress("Configuring OBS's WebSocket server...")
        obs_ws_configured, obs_ws_reason = try_configure_obs_websocket(password)

    exe_path = os.path.join(install_dir, EXE_NAME)

    if options.get("desktop_shortcut"):
        on_progress("Creating desktop shortcut...")
        create_shortcut(get_desktop_shortcut_path(), exe_path, install_dir)

    if options.get("start_at_login"):
        on_progress("Setting up automatic startup...")
        startup_path = get_startup_shortcut_path()
        if startup_path:
            create_shortcut(startup_path, exe_path, install_dir)

    on_progress("Done.")
    return {
        "exe_path": exe_path,
        "password": password,
        "obs_ws_configured": obs_ws_configured,
        "obs_ws_reason": obs_ws_reason,
    }


def do_uninstall(install_dir, keep_config, on_progress):
    on_progress("Removing shortcuts...")
    for path in [get_desktop_shortcut_path(), get_startup_shortcut_path()]:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    on_progress("Removing application files...")
    for name in (EXE_NAME, "autostart_script.log"):
        try:
            path = os.path.join(install_dir, name)
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    if not keep_config:
        try:
            config_path = os.path.join(install_dir, "config.json")
            if os.path.isfile(config_path):
                os.remove(config_path)
        except OSError:
            pass

    try:
        if not os.listdir(install_dir):
            os.rmdir(install_dir)
    except OSError:
        pass
    on_progress("Done.")


PAGE_BG = "#f4f4f4"
HEADER_BG = "#1f2937"
HEADER_FG = "#ffffff"


def main():
    root = tk.Tk()
    root.title(f"{APP_NAME} Setup")
    root.geometry("580x480")
    root.resizable(False, False)

    header = tk.Frame(root, bg=HEADER_BG, height=64)
    header.pack(fill="x")
    header.pack_propagate(False)
    tk.Label(
        header, text=f"{APP_NAME} Setup", bg=HEADER_BG, fg=HEADER_FG, font=("Segoe UI", 14, "bold")
    ).pack(side="left", padx=20)

    container = tk.Frame(root, bg=PAGE_BG)
    container.pack(fill="both", expand=True)
    container.columnconfigure(0, weight=1)
    container.rowconfigure(0, weight=1)

    nav = tk.Frame(root)
    nav.pack(fill="x", padx=16, pady=12)

    pages = {}

    def register(name, frame):
        frame.grid(row=0, column=0, sticky="nsew")
        pages[name] = frame

    def show(name):
        pages[name].tkraise()

    def page_frame():
        return tk.Frame(container, bg=PAGE_BG, padx=24, pady=20)

    def heading(parent, text):
        tk.Label(parent, text=text, bg=PAGE_BG, font=("Segoe UI", 12, "bold"), anchor="w").pack(
            fill="x", pady=(0, 12)
        )

    def body_text(parent, text):
        tk.Label(
            parent, text=text, bg=PAGE_BG, font=("Segoe UI", 10), anchor="w", justify="left", wraplength=510
        ).pack(fill="x", pady=(0, 8))

    # ---------- Welcome ----------
    welcome = page_frame()
    register("welcome", welcome)
    heading(welcome, f"Welcome to {APP_NAME}")
    body_text(
        welcome,
        f"{APP_NAME} is a small background app that automatically starts and stops "
        "OBS recording whenever you launch or close a game — so you never have to "
        "remember to hit record.",
    )
    for line in [
        "\u2713  Detects games automatically (Steam, Epic, GOG, Xbox) — nothing to set up",
        "\u2713  Runs quietly in the system tray, out of your way",
        "\u2713  Can start automatically when your PC turns on",
    ]:
        tk.Label(welcome, text=line, bg=PAGE_BG, font=("Segoe UI", 10), anchor="w").pack(fill="x", pady=2)

    # ---------- Install location ----------
    location = page_frame()
    register("location", location)
    heading(location, "Choose where to install")
    body_text(location, "Most people can just leave this as-is and click Next.")

    dir_row = tk.Frame(location, bg=PAGE_BG)
    dir_row.pack(fill="x", pady=(4, 4))
    dir_var = tk.StringVar(value=DEFAULT_INSTALL_DIR)
    tk.Entry(dir_row, textvariable=dir_var, width=48).pack(side="left", fill="x", expand=True)

    def browse_install_dir():
        chosen = filedialog.askdirectory(initialdir=dir_var.get() or "C:\\")
        if chosen:
            dir_var.set(os.path.normpath(os.path.join(chosen, "OBSAutoRecorder")))

    tk.Button(dir_row, text="Browse...", command=browse_install_dir).pack(side="left", padx=(8, 0))

    location_notice = tk.Label(
        location, text="", bg=PAGE_BG, fg="#b45309", font=("Segoe UI", 9, "italic"),
        anchor="w", wraplength=510, justify="left",
    )
    location_notice.pack(fill="x", pady=(2, 12))

    desktop_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        location, text="Create a shortcut on my Desktop", variable=desktop_var, bg=PAGE_BG
    ).pack(anchor="w", pady=4)

    startup_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        location, text="Start automatically when Windows starts", variable=startup_var, bg=PAGE_BG
    ).pack(anchor="w", pady=4)

    # ---------- Find OBS ----------
    obs_page = page_frame()
    register("obs", obs_page)
    heading(obs_page, "Locate OBS Studio")
    body_text(obs_page, f"{APP_NAME} controls OBS for you, so it needs to know where OBS is installed.")

    obs_status = tk.Label(
        obs_page, text="", bg=PAGE_BG, font=("Segoe UI", 10), anchor="w", wraplength=510, justify="left"
    )
    obs_status.pack(fill="x", pady=(8, 8))

    obs_var = tk.StringVar(value=find_obs_exe() or "")

    def refresh_obs_status():
        if obs_var.get() and os.path.isfile(obs_var.get()):
            obs_status.config(text=f"\u2713  Found: {obs_var.get()}", fg="#15803d")
        else:
            obs_status.config(
                text=(
                    "Couldn't find OBS automatically. If it's already installed, click Browse "
                    "below and find obs64.exe. Don't have OBS yet? That's fine — install it later "
                    "from obsproject.com, then point to it from this app's Settings."
                ),
                fg="#b45309",
            )

    def browse_obs():
        chosen = filedialog.askopenfilename(
            title="Locate obs64.exe", filetypes=[("OBS Studio", "obs64.exe"), ("All files", "*.*")]
        )
        if chosen:
            obs_var.set(chosen)
            refresh_obs_status()

    tk.Button(obs_page, text="Browse for obs64.exe...", command=browse_obs).pack(anchor="w", pady=(0, 8))
    refresh_obs_status()

    # ---------- Quick options ----------
    options_page = page_frame()
    register("options", options_page)
    heading(options_page, "A few quick preferences")
    body_text(options_page, "You can change any of these later from the app's Settings.")

    split_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        options_page, text="Split long recordings into smaller files automatically",
        variable=split_var, bg=PAGE_BG,
    ).pack(anchor="w", pady=4)

    short_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        options_page, text="Automatically delete very short recordings (under 20 seconds)",
        variable=short_var, bg=PAGE_BG,
    ).pack(anchor="w", pady=4)

    notify_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        options_page, text="Show a popup notification when recording starts or stops",
        variable=notify_var, bg=PAGE_BG,
    ).pack(anchor="w", pady=4)

    folders_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        options_page, text="Put each game's recordings in its own folder",
        variable=folders_var, bg=PAGE_BG,
    ).pack(anchor="w", pady=4)

    # ---------- Ready / progress ----------
    ready_page = page_frame()
    register("ready", ready_page)
    heading(ready_page, "Ready to install")
    summary_label = tk.Label(
        ready_page, text="", bg=PAGE_BG, font=("Segoe UI", 10), anchor="w", justify="left", wraplength=510
    )
    summary_label.pack(fill="x", pady=(0, 12))
    progress_label = tk.Label(ready_page, text="", bg=PAGE_BG, font=("Segoe UI", 9), fg="#555555", anchor="w")
    progress_label.pack(fill="x")

    # ---------- Already installed ----------
    existing_page = page_frame()
    register("existing", existing_page)
    heading(existing_page, f"{APP_NAME} is already installed here")
    body_text(existing_page, "What would you like to do?")

    update_btn = tk.Button(existing_page, text="Update (keep my current settings)")
    update_btn.pack(anchor="w", pady=(8, 4), ipadx=8, ipady=2)
    tk.Label(
        existing_page, text="Replaces the app files only — your config.json is left untouched.",
        bg=PAGE_BG, font=("Segoe UI", 9), fg="#555555", anchor="w",
    ).pack(anchor="w", pady=(0, 16))

    keep_config_var = tk.BooleanVar(value=True)
    uninstall_btn = tk.Button(existing_page, text="Uninstall")
    uninstall_btn.pack(anchor="w", pady=(0, 4), ipadx=8, ipady=2)
    tk.Checkbutton(
        existing_page, text="Keep my settings (config.json) in case I reinstall later",
        variable=keep_config_var, bg=PAGE_BG,
    ).pack(anchor="w")

    different_folder_btn = tk.Button(existing_page, text="Choose a different folder instead")
    different_folder_btn.pack(anchor="w", pady=(16, 0))

    existing_error = tk.Label(
        existing_page, text="", bg=PAGE_BG, fg="#b91c1c", font=("Segoe UI", 9), anchor="w", wraplength=510
    )
    existing_error.pack(fill="x", pady=(12, 0))

    # ---------- Finish ----------
    finish_page = page_frame()
    register("finish", finish_page)
    finish_heading = tk.Label(finish_page, text="", bg=PAGE_BG, font=("Segoe UI", 12, "bold"), anchor="w")
    finish_heading.pack(fill="x", pady=(0, 12))
    finish_body = tk.Label(
        finish_page, text="", bg=PAGE_BG, font=("Segoe UI", 10), anchor="w", justify="left", wraplength=510
    )
    finish_body.pack(fill="x", pady=(0, 12))
    launch_var = tk.BooleanVar(value=True)
    launch_check = tk.Checkbutton(finish_page, text=f"Launch {APP_NAME} now", variable=launch_var, bg=PAGE_BG)
    launch_check.pack(anchor="w", pady=(8, 0))

    # ---------- Navigation ----------
    order = ["welcome", "location", "obs", "options", "ready", "finish"]
    current = {"index": 0, "install_dir": DEFAULT_INSTALL_DIR, "on_existing": False}

    back_btn = tk.Button(nav, text="< Back")
    next_btn = tk.Button(nav, text="Next >")
    cancel_btn = tk.Button(nav, text="Cancel", command=root.destroy)

    def show_nav(back=True, next_=True, cancel=True):
        for widget in (back_btn, next_btn, cancel_btn):
            widget.pack_forget()
        if cancel:
            cancel_btn.pack(side="right")
        if next_:
            next_btn.pack(side="right", padx=(0, 8))
        if back:
            back_btn.pack(side="right", padx=(0, 8))

    def run_async(worker, on_done):
        def wrapper():
            try:
                result = worker()
            except Exception as exc:
                error = exc
                root.after(0, lambda: on_done(None, error))
                return
            root.after(0, lambda: on_done(result, None))

        threading.Thread(target=wrapper, daemon=True).start()

    def collect_options():
        return {
            "desktop_shortcut": desktop_var.get(),
            "start_at_login": startup_var.get(),
            "split_long_recordings": split_var.get(),
            "delete_short_clips": short_var.get(),
            "notifications": notify_var.get(),
            "per_game_folders": folders_var.get(),
        }

    def go_to_index(index):
        current["index"] = index
        current["on_existing"] = False
        name = order[index]
        show(name)
        if name == "ready":
            show_nav(back=True, next_=True, cancel=True)
            back_btn.config(state="normal")
            existing = os.path.join(dir_var.get(), "config.json")
            preserved_note = " (your existing settings will be kept)" if os.path.isfile(existing) else ""
            summary_label.config(
                text=(
                    f"Install folder: {dir_var.get()}{preserved_note}\n"
                    f"OBS location: {obs_var.get() or 'not set yet'}\n"
                    f"Desktop shortcut: {'Yes' if desktop_var.get() else 'No'}\n"
                    f"Start with Windows: {'Yes' if startup_var.get() else 'No'}"
                )
            )
            progress_label.config(text="")
            next_btn.config(text="Install", state="normal", command=start_install)
        else:
            show_nav(back=True, next_=True, cancel=True)
            back_btn.config(state=("disabled" if index == 0 else "normal"))
            next_btn.config(text="Next >", state="normal", command=go_next)

    def go_next():
        if order[current["index"]] == "location":
            target_dir = dir_var.get().strip()
            if not target_dir:
                location_notice.config(text="Please choose an install folder.")
                return
            location_notice.config(text="")
            if is_existing_install(target_dir):
                existing_error.config(text="")
                current["on_existing"] = True
                show("existing")
                show_nav(back=True, next_=False, cancel=True)
                return
            go_to_index(current["index"] + 1)
            return
        if order[current["index"]] == "obs":
            refresh_obs_status()
        go_to_index(min(current["index"] + 1, len(order) - 1))

    def go_back():
        if current.get("on_existing"):
            current["on_existing"] = False
            go_to_index(order.index("location"))
            return
        go_to_index(max(current["index"] - 1, 0))

    def start_install():
        next_btn.config(state="disabled")
        back_btn.config(state="disabled")

        def worker():
            return do_install(dir_var.get(), obs_var.get() or None, collect_options(), report_progress)

        def report_progress(message):
            root.after(0, lambda: progress_label.config(text=message))

        def done(result, error):
            if error is not None:
                next_btn.config(state="normal")
                back_btn.config(state="normal")
                messagebox.showerror(
                    "Setup failed",
                    f"Something went wrong during install:\n\n{error}\n\n"
                    "Try choosing a different install folder, or make sure no antivirus is blocking it.",
                    parent=root,
                )
                return
            show_install_finish(result)

        run_async(worker, done)

    def show_install_finish(result):
        finish_heading.config(text="Setup complete!")
        lines = [f"{APP_NAME} is installed at:\n{result['exe_path']}\n"]
        if result["password"] is None:
            lines.append("Your existing settings were kept as-is.")
        elif result["obs_ws_configured"]:
            lines.append(
                "OBS's WebSocket server was configured automatically — you're all set. "
                "Just start playing a game and it'll start recording on its own!"
            )
        else:
            lines.append(
                "One manual step is still needed: open OBS \u2192 Tools \u2192 WebSocket Server "
                f"Settings, turn it on, and set the password to:\n\n{result['password']}\n\n"
                "(Or copy whatever password OBS already shows there into this app's Settings instead.)"
            )
        finish_body.config(text="\n".join(lines))
        launch_check.pack(anchor="w", pady=(8, 0))
        current["install_dir"] = dir_var.get()
        show("finish")
        show_nav(back=False, next_=True, cancel=False)
        next_btn.config(text="Finish", state="normal", command=finish)

    def show_uninstall_finish():
        finish_heading.config(text="Uninstalled")
        finish_body.config(text=f"{APP_NAME} has been removed from {dir_var.get()}.")
        launch_var.set(False)
        launch_check.pack_forget()
        current["install_dir"] = None
        show("finish")
        show_nav(back=False, next_=True, cancel=False)
        next_btn.config(text="Close", state="normal", command=root.destroy)

    def on_update_clicked():
        if is_app_running():
            existing_error.config(
                text=f"Please quit {APP_NAME} from the system tray first, then try again."
            )
            return
        update_btn.config(state="disabled")
        uninstall_btn.config(state="disabled")

        def worker():
            return do_install(dir_var.get(), None, collect_options(), lambda _msg: None, write_config=False)

        def done(result, error):
            update_btn.config(state="normal")
            uninstall_btn.config(state="normal")
            if error is not None:
                messagebox.showerror("Update failed", str(error), parent=root)
                return
            show_install_finish(result)

        run_async(worker, done)

    def on_uninstall_clicked():
        if is_app_running():
            existing_error.config(
                text=f"Please quit {APP_NAME} from the system tray first, then try again."
            )
            return
        update_btn.config(state="disabled")
        uninstall_btn.config(state="disabled")

        def worker():
            do_uninstall(dir_var.get(), keep_config_var.get(), lambda _msg: None)

        def done(_result, error):
            update_btn.config(state="normal")
            uninstall_btn.config(state="normal")
            if error is not None:
                messagebox.showerror("Uninstall failed", str(error), parent=root)
                return
            show_uninstall_finish()

        run_async(worker, done)

    def on_different_folder_clicked():
        current["on_existing"] = False
        go_to_index(order.index("location"))

    update_btn.config(command=on_update_clicked)
    uninstall_btn.config(command=on_uninstall_clicked)
    different_folder_btn.config(command=on_different_folder_clicked)
    back_btn.config(command=go_back)
    next_btn.config(command=go_next)

    def finish():
        if launch_var.get() and current["install_dir"]:
            try:
                subprocess.Popen([os.path.join(current["install_dir"], EXE_NAME)], cwd=current["install_dir"])
            except OSError:
                pass
        root.destroy()

    go_to_index(0)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        try:
            messagebox.showerror("Setup error", str(exc))
        except Exception:
            pass
        raise
