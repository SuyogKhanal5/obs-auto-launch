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
import webbrowser
import zipfile
from tkinter import filedialog, messagebox, ttk

import psutil

import platform_common
import platform_windows

APP_NAME = "OBS Auto Recorder"
APP_FOLDER_NAME = "OBSAutoRecorder"  # onedir build folder embedded in this installer's data
APP_INTERNAL_DIR_NAME = "_internal"  # PyInstaller onedir's support-files subfolder
# PyInstaller's own --name OBSAutoRecorder produces a different real binary name/layout per OS:
# OBSAutoRecorder.exe (Windows), a bare OBSAutoRecorder binary (Linux), or an OBSAutoRecorder.app
# bundle whose real executable lives at Contents/MacOS/OBSAutoRecorder (macOS) -- see
# CROSS_PLATFORM_PLAN.md Phase 7 / .github/workflows/build-release.yml for how each is built.
EXE_NAME = platform_common.example_executable_name("OBSAutoRecorder")
APP_BUNDLE_NAME = f"{APP_FOLDER_NAME}.app"  # macOS only
# macOS embeds the .app bundle as a zip (see build-release.yml) rather than a plain data
# directory -- PyInstaller's own macOS codesigning pass tries to re-sign a Mach-O binary it finds
# nested inside embedded *data*, which fails; a zip has no binary for it to find at all. Not used
# on Windows/Linux, which embed the onedir folder directly.
APP_BUNDLE_ZIP_NAME = "OBSAutoRecorder-app.zip"


def app_relative_path():
    """Path to the real launchable executable, relative to install_dir. Just EXE_NAME everywhere
    except macOS, where the app ships as a .app bundle (a directory), not a bare executable, so
    the actual Mach-O binary lives nested inside it."""
    if sys.platform == "darwin":
        return os.path.join(APP_BUNDLE_NAME, "Contents", "MacOS", EXE_NAME)
    return EXE_NAME


# Per-user install locations (CROSS_PLATFORM_PLAN.md §2.8) -- none of the 3 need elevation for
# the app install itself (a separate concern from installing system packages like ffmpeg/OBS via
# platform_common.install_optional_dependency/run_linux_install_command, which does need root on
# Linux and admin on Windows for THAT, regardless of where this app itself lives).
if sys.platform == "win32":
    DEFAULT_INSTALL_DIR = r"C:\Program Files\OBSAutoRecorder"
elif sys.platform == "darwin":
    DEFAULT_INSTALL_DIR = os.path.expanduser("~/Applications/OBSAutoRecorder")
else:
    DEFAULT_INSTALL_DIR = os.path.expanduser("~/.local/share/OBSAutoRecorder")
OBS_DOWNLOAD_URL = "https://obsproject.com/download"
FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"

# Duplicated from autostart_script.py's COMMON_GAMES rather than imported (see the module
# docstring on why this installer stays independent of the main app's code/dependencies). Only
# games with at least one launch path this app can't auto-discover belong here -- Steam, Epic,
# GOG, Xbox, and Battle.net installs are all found automatically once those launchers are enabled
# in Settings, so a Steam-only title would just be redundant clutter on this page.
COMMON_GAMES = [
    {"name": "League of Legends", "process_name": "league of legends.exe"},
    {"name": "Wizard101", "process_name": "WizardGraphicalClient.exe"},
    {"name": "Valorant", "process_name": "VALORANT-Win64-Shipping.exe"},
    {"name": "Warframe", "process_name": "Warframe.x64.exe"},
    {"name": "Minecraft: Java Edition", "process_name": "javaw.exe", "title_contains": "minecraft"},
    {"name": "Apex Legends", "process_name": "r5apex.exe"},
    {"name": "Roblox", "process_name": "RobloxPlayerBeta.exe"},
    {"name": "Genshin Impact", "process_name": "GenshinImpact.exe"},
]


def is_ffmpeg_installed():
    """Presence check backed by platform_common.find_ffmpeg_executable() -- the same per-OS
    search autostart_script.py itself uses, no longer duplicated here (previously this had its
    own separate copy of the Windows candidate list; see CROSS_PLATFORM_PLAN.md Phase 1)."""
    return platform_common.find_ffmpeg_executable() is not None


def ensure_ffmpeg(on_progress):
    """Best-effort, non-blocking: ffmpeg powers the app's optional MKV-to-MP4 post-record
    conversion, but most people don't have it and don't know where to get it. Windows/macOS
    install it automatically via platform_common.install_optional_dependency (winget/Homebrew)
    when available; Linux never does a silent background install at all (see
    CROSS_PLATFORM_PLAN.md §2.7) -- the finish page shows the exact install command instead (see
    build_linux_ffmpeg_command below), left for the user to run explicitly. Returns one of
    "already_present", "installed", "no_package_manager", "install_failed", "manual_only" --
    callers decide what (if anything) to tell the user based on that; this never raises or fails
    setup itself."""
    if is_ffmpeg_installed():
        return "already_present"
    if sys.platform not in ("win32", "darwin"):
        return "manual_only"
    if not platform_common.has_package_manager():
        return "no_package_manager"
    on_progress("Installing ffmpeg (for optional MKV to MP4 conversion)...")
    success, _reason = platform_common.install_optional_dependency("ffmpeg")
    return "installed" if success else "install_failed"


def build_linux_ffmpeg_command():
    """The exact command to install ffmpeg on this Linux system, for the finish page to show
    verbatim when ensure_ffmpeg above returned "manual_only" -- or None if no supported package
    manager was detected, in which case the finish page falls back to the plain download link."""
    return platform_common.build_linux_install_command("ffmpeg")


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def get_desktop_shortcut_path():
    """The "Create a shortcut on my Desktop" installer option -- still Windows-only (no
    Linux/macOS equivalent implemented yet, unlike autostart below, which CROSS_PLATFORM_PLAN.md
    Phase 5 did port); reaches directly into platform_windows.py rather than through
    platform_common since this isn't part of the cross-platform interface. create_shortcut/
    get_startup_shortcut_path moved there too -- see platform_windows.py's own "Shortcuts /
    autostart" section."""
    return platform_windows.get_desktop_shortcut_path()


def find_obs_exe():
    """Backed by platform_common.find_obs_executable() -- the per-OS search (registry lookup on
    Windows, .app bundle / Flatpak on macOS / Linux) now lives there, see
    CROSS_PLATFORM_PLAN.md Phase 1. Kept under this name since it's called from a couple of
    places in this file already."""
    return platform_common.find_obs_executable()


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
    watched_games = []
    watched_windows = []
    for game in COMMON_GAMES:
        if game["name"] not in options.get("selected_games", []):
            continue
        if "title_contains" in game:
            watched_windows.append({
                "process_name": game["process_name"],
                "title_contains": game["title_contains"],
                "display_name": game["name"],
            })
        else:
            watched_games.append(game["process_name"])
    config["watched_games"] = watched_games
    config["watched_windows"] = watched_windows
    config["obs"]["path"] = obs_path or template["obs"]["path"]
    config["obs"]["websocket"]["password"] = password
    config["obs"]["auto_split"]["enabled"] = options["split_long_recordings"]
    config["cleanup"]["delete_short_clips"]["enabled"] = options["delete_short_clips"]
    config["notifications"]["enabled"] = options["notifications"]
    config["organize_into_game_subfolders"] = options["per_game_folders"]
    config["obs"]["multi_track_audio"]["enabled"] = options["multi_track_audio"]
    if options["output_folder"]:
        config["obs"]["output_folder"] = options["output_folder"]
    config["obs"]["game_audio_capture"]["enabled"] = options["game_audio_isolation"]
    config["obs"]["replay_buffer"]["mode"] = "with_recording" if options["replay_buffer"] else "off"
    config["disk_space_guard"]["enabled"] = options["disk_space_guard"]
    if options["disk_space_guard_min_gb"]:
        try:
            config["disk_space_guard"]["minimum_free_gb"] = float(options["disk_space_guard_min_gb"])
        except ValueError:
            pass
    return config


def is_existing_install(install_dir):
    return os.path.isfile(os.path.join(install_dir, app_relative_path()))


def do_install(install_dir, obs_path, options, on_progress, write_config=True):
    on_progress(f"Creating {install_dir} ...")
    os.makedirs(install_dir, exist_ok=True)

    on_progress("Copying application files...")
    if sys.platform == "darwin":
        # The .app bundle is embedded as a zip, not a plain data directory -- see
        # APP_BUNDLE_ZIP_NAME's own docstring for why. Extracting overwrites an existing install
        # in place, same as copytree's dirs_exist_ok=True does for the other two OSes below.
        with zipfile.ZipFile(resource_path(APP_BUNDLE_ZIP_NAME)) as zf:
            zf.extractall(install_dir)
    else:
        # The app ships as a PyInstaller onedir build (an exe plus an _internal/ folder of
        # support files) rather than onefile -- deliberately, since onefile re-extracts itself to
        # a fresh %TEMP% folder on every single launch, which antivirus real-time scanning can
        # intermittently fail to clean up (a widely-reported PyInstaller/Defender interaction).
        # Onedir runs directly from where it's installed, so that whole failure mode doesn't
        # exist. Embedded here as a whole folder (APP_FOLDER_NAME) rather than a single file;
        # dirs_exist_ok=True lets Update overwrite an existing install in place.
        shutil.copytree(resource_path(APP_FOLDER_NAME), install_dir, dirs_exist_ok=True)

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

    on_progress("Checking for ffmpeg...")
    ffmpeg_status = ensure_ffmpeg(on_progress)

    exe_path = os.path.join(install_dir, app_relative_path())

    if options.get("desktop_shortcut"):
        on_progress("Creating desktop shortcut...")
        platform_windows.create_shortcut(get_desktop_shortcut_path(), exe_path, install_dir)

    if options.get("start_at_login"):
        on_progress("Setting up automatic startup...")
        # exe_path/install_dir here are the freshly-installed app's own location -- NOT
        # sys.executable (this installer's own path), which is why they're passed explicitly
        # rather than relying on enable_autostart's sys.executable default (see that function's
        # own docstring).
        platform_common.enable_autostart(exe_path, install_dir)

    on_progress("Done.")
    return {
        "exe_path": exe_path,
        "password": password,
        "obs_ws_configured": obs_ws_configured,
        "obs_ws_reason": obs_ws_reason,
        "ffmpeg_status": ffmpeg_status,
    }


def do_uninstall(install_dir, keep_config, on_progress):
    on_progress("Removing shortcuts...")
    desktop_path = get_desktop_shortcut_path()
    try:
        if desktop_path and os.path.isfile(desktop_path):
            os.remove(desktop_path)
    except OSError:
        pass
    platform_common.disable_autostart()

    on_progress("Removing application files...")
    internal_dir = os.path.join(install_dir, APP_INTERNAL_DIR_NAME)
    if os.path.isdir(internal_dir):
        shutil.rmtree(internal_dir, ignore_errors=True)
    if sys.platform == "darwin":
        # The .app bundle is a directory, not a file -- os.remove would just raise on it (caught
        # below, but silently leaving the whole bundle behind instead of actually uninstalling).
        bundle_path = os.path.join(install_dir, APP_BUNDLE_NAME)
        if os.path.isdir(bundle_path):
            shutil.rmtree(bundle_path, ignore_errors=True)
    for name in ((EXE_NAME,) if sys.platform != "darwin" else ()) + ("autostart_script.log",):
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
    root.geometry("580x600")
    root.resizable(False, False)

    header = tk.Frame(root, bg=HEADER_BG, height=64)
    header.pack(fill="x")
    header.pack_propagate(False)
    tk.Label(
        header, text=f"{APP_NAME} Setup", bg=HEADER_BG, fg=HEADER_FG, font=("Segoe UI", 14, "bold")
    ).pack(side="left", padx=20)

    # nav is packed (side="bottom") before container so it always claims its space against the
    # bottom edge first -- if it were packed after an expand=True container instead, a page tall
    # enough to make container's own requested height exceed what's left over (the Advanced
    # options page does, at ~470px) would squeeze nav down to near-zero height, hiding the
    # Back/Next/Cancel buttons entirely with no error or indication anything was wrong.
    nav = tk.Frame(root)
    nav.pack(side="bottom", fill="x", padx=16, pady=12)

    container = tk.Frame(root, bg=PAGE_BG)
    container.pack(fill="both", expand=True)
    container.columnconfigure(0, weight=1)
    container.rowconfigure(0, weight=1)

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
        chosen = filedialog.askdirectory(initialdir=dir_var.get() or os.path.expanduser("~"))
        if chosen:
            dir_var.set(os.path.normpath(os.path.join(chosen, "OBSAutoRecorder")))

    tk.Button(dir_row, text="Browse...", command=browse_install_dir).pack(side="left", padx=(8, 0))

    location_notice = tk.Label(
        location, text="", bg=PAGE_BG, fg="#b45309", font=("Segoe UI", 9, "italic"),
        anchor="w", wraplength=510, justify="left",
    )
    location_notice.pack(fill="x", pady=(2, 12))

    # The desktop-icon feature itself is still Windows-only (see platform_windows.py's own
    # "Shortcuts / autostart" section) -- unlike autostart below, no Linux/macOS equivalent is
    # implemented yet, so this option doesn't exist to show on those OSes at all.
    desktop_var = tk.BooleanVar(value=sys.platform == "win32")
    if sys.platform == "win32":
        tk.Checkbutton(
            location, text="Create a shortcut on my Desktop", variable=desktop_var, bg=PAGE_BG
        ).pack(anchor="w", pady=4)

    startup_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        location, text="Start automatically at login", variable=startup_var, bg=PAGE_BG
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
            download_link.pack_forget()
        else:
            obs_status.config(
                text=(
                    "Couldn't find OBS automatically. If it's already installed, click Browse "
                    "to find obs64.exe. Don't have OBS yet? That's fine — install it now with the "
                    "link below, or later from obsproject.com, then point to it from this app's Settings."
                ),
                fg="#b45309",
            )
            download_link.pack(side="left", padx=(16, 0))

    def browse_obs():
        example = platform_common.example_executable_name("obs64")
        chosen = filedialog.askopenfilename(
            title=f"Locate {example}", filetypes=platform_common.executable_filetypes()
        )
        if chosen:
            obs_var.set(chosen)
            refresh_obs_status()

    def on_easy_install_obs():
        # Windows/macOS: a silent, fire-and-forget background install (winget/Homebrew) --
        # nothing to confirm first, same philosophy as today. Linux never does this silently
        # (CROSS_PLATFORM_PLAN.md §2.7); this button only exists there once
        # linux_install_command has already been shown as read-only text below, and running it
        # goes through run_linux_install_command (pkexec) instead of install_optional_dependency.
        easy_install_btn.config(state="disabled")
        install_verb = "Installing" if sys.platform in ("win32", "darwin") else "Running the install command for"
        obs_status.config(text=f"{install_verb} OBS Studio... this can take a few minutes.", fg="#555555")
        download_link.pack_forget()

        def worker():
            if sys.platform in ("win32", "darwin"):
                return platform_common.install_optional_dependency("obs", timeout=900)
            return platform_common.run_linux_install_command(linux_install_command, timeout=900)

        def done(result, error):
            easy_install_btn.config(state="normal")
            success, reason = result if error is None else (False, str(error))
            if success:
                found = find_obs_exe()
                if found:
                    obs_var.set(found)
                refresh_obs_status()
            else:
                obs_status.config(
                    text=(
                        f"Automatic install didn't finish ({reason}). Try Browse if it's already "
                        "installed, or use the download link below."
                    ),
                    fg="#b91c1c",
                )
                download_link.pack(side="left", padx=(16, 0))

        run_async(worker, done)

    obs_buttons_row = tk.Frame(obs_page, bg=PAGE_BG)
    obs_buttons_row.pack(anchor="w", pady=(0, 8))
    tk.Button(
        obs_buttons_row, text=f"Browse for {platform_common.example_executable_name('obs64')}...", command=browse_obs,
    ).pack(side="left")
    linux_install_command = platform_common.build_linux_install_command("obs") if sys.platform not in ("win32", "darwin") else None
    if sys.platform in ("win32", "darwin"):
        # "Easy Install" only makes sense (and only appears) when a silent-install mechanism is
        # actually available to run it -- otherwise the manual download link below is the only
        # path, same as before.
        if platform_common.has_package_manager():
            easy_install_btn = tk.Button(obs_buttons_row, text="Easy Install", command=on_easy_install_obs)
            easy_install_btn.pack(side="left", padx=(8, 0))
    elif linux_install_command:
        easy_install_btn = tk.Button(obs_buttons_row, text="Run this command", command=on_easy_install_obs)
        easy_install_btn.pack(side="left", padx=(8, 0))
    download_link = tk.Label(
        obs_buttons_row, text="Don't have OBS? Download it here ↗", bg=PAGE_BG, fg="#2563eb",
        font=("Segoe UI", 10, "underline"), cursor="hand2",
    )
    download_link.bind("<Button-1>", lambda _event: webbrowser.open(OBS_DOWNLOAD_URL))

    # Linux: the exact command shown as read-only text before the "Run this command" button above
    # can ever be clicked -- transparent, nothing hidden, per CROSS_PLATFORM_PLAN.md §2.7. Falls
    # back to the plain download link (already handled above) if no package manager/Flatpak was
    # detected at all.
    if sys.platform not in ("win32", "darwin") and linux_install_command:
        command_row = tk.Frame(obs_page, bg=PAGE_BG)
        command_row.pack(anchor="w", fill="x", pady=(0, 8))
        tk.Label(command_row, text="This will run:", bg=PAGE_BG, font=("Segoe UI", 9)).pack(anchor="w")
        command_entry = tk.Entry(command_row, font=("Consolas", 9))
        command_entry.insert(0, linux_install_command)
        command_entry.config(state="readonly")
        command_entry.pack(fill="x", pady=(2, 0))

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

    # ---------- Common Games ----------
    games_page = page_frame()
    register("games", games_page)
    heading(games_page, "Games to watch")
    body_text(
        games_page,
        "Check any of these you play. You can add more, or edit these, later from the app's Settings.",
    )

    game_vars = {}
    for game in COMMON_GAMES:
        var = tk.BooleanVar(value=False)
        tk.Checkbutton(games_page, text=game["name"], variable=var, bg=PAGE_BG).pack(anchor="w", pady=2)
        game_vars[game["name"]] = var

    tk.Label(
        games_page,
        text=(
            "    Steam, Epic, GOG, Xbox, and Battle.net games are all detected automatically once "
            "those launchers are enabled in Settings → Launchers -- these are just for games "
            "with their own separate launcher, so nothing here is required."
        ),
        bg=PAGE_BG, font=("Segoe UI", 9), fg="#555555", anchor="w", justify="left", wraplength=510,
    ).pack(anchor="w", pady=(12, 0))

    # ---------- Advanced (optional, skippable) ----------
    advanced_page = page_frame()
    register("advanced", advanced_page)
    heading(advanced_page, "Advanced options (optional)")
    body_text(
        advanced_page,
        "Everything here is off by default and fine to leave alone — skip straight to Install if "
        "you don't need any of it. All of it can be turned on later from the app's Settings too.",
    )
    skip_advanced_link = tk.Label(
        advanced_page, text="Skip this page \u2192", bg=PAGE_BG, fg="#2563eb",
        font=("Segoe UI", 10, "underline"), cursor="hand2",
    )
    skip_advanced_link.pack(anchor="w", pady=(0, 10))

    multi_track_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        advanced_page, text="Record mic, desktop, and game audio to separate tracks",
        variable=multi_track_var, bg=PAGE_BG,
    ).pack(anchor="w", pady=4)
    tk.Label(
        advanced_page,
        text="    Uses its own dedicated OBS profile, so this never changes your existing OBS setup.",
        bg=PAGE_BG, font=("Segoe UI", 9), fg="#555555", anchor="w",
    ).pack(anchor="w")

    game_audio_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        advanced_page, text="Isolate game audio from Discord/Spotify/etc.",
        variable=game_audio_var, bg=PAGE_BG,
    ).pack(anchor="w", pady=(10, 4))
    tk.Label(
        advanced_page,
        text="    Requires an Application Audio Capture source named \"Game Audio\" in OBS — add "
        "one now or later; this just tells the app to use it once it's there.",
        bg=PAGE_BG, font=("Segoe UI", 9), fg="#555555", anchor="w", justify="left", wraplength=490,
    ).pack(anchor="w")

    replay_buffer_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        advanced_page, text="Start OBS's replay buffer alongside recording",
        variable=replay_buffer_var, bg=PAGE_BG,
    ).pack(anchor="w", pady=(10, 4))
    tk.Label(
        advanced_page,
        text=(
            "    Defaults to a 30-second buffer -- change the length, or switch to replay-buffer-"
            "only mode (skips full recordings entirely), later from this app's Settings \u2192 OBS. "
            "OBS needs one restart after this is turned on before the buffer actually works."
        ),
        bg=PAGE_BG, font=("Segoe UI", 9), fg="#555555", anchor="w", justify="left", wraplength=490,
    ).pack(anchor="w")

    disk_guard_var = tk.BooleanVar(value=False)
    disk_guard_row = tk.Frame(advanced_page, bg=PAGE_BG)
    disk_guard_row.pack(fill="x", pady=(10, 0))
    tk.Checkbutton(
        disk_guard_row, text="Don't start recording if free disk space is below", variable=disk_guard_var, bg=PAGE_BG,
    ).pack(side="left")
    disk_guard_min_gb_var = tk.StringVar(value="")
    tk.Entry(disk_guard_row, textvariable=disk_guard_min_gb_var, width=5).pack(side="left", padx=(4, 4))
    tk.Label(disk_guard_row, text="GB (blank = 10)", bg=PAGE_BG).pack(side="left")

    tk.Label(
        advanced_page, text="Recording output folder", bg=PAGE_BG, font=("Segoe UI", 10), anchor="w",
    ).pack(anchor="w", pady=(14, 2))
    output_folder_row = tk.Frame(advanced_page, bg=PAGE_BG)
    output_folder_row.pack(fill="x")
    output_folder_var = tk.StringVar(value="")
    tk.Entry(output_folder_row, textvariable=output_folder_var, width=44).pack(side="left", fill="x", expand=True)

    def browse_output_folder():
        chosen = filedialog.askdirectory(initialdir=output_folder_var.get() or os.path.expanduser("~"))
        if chosen:
            output_folder_var.set(os.path.normpath(chosen))

    tk.Button(output_folder_row, text="Browse...", command=browse_output_folder).pack(side="left", padx=(8, 0))
    tk.Label(
        advanced_page, text="    Leave blank to use OBS's own recording folder setting.",
        bg=PAGE_BG, font=("Segoe UI", 9), fg="#555555", anchor="w",
    ).pack(anchor="w")

    # ---------- Ready / progress ----------
    ready_page = page_frame()
    register("ready", ready_page)
    heading(ready_page, "Ready to install")
    summary_label = tk.Label(
        ready_page, text="", bg=PAGE_BG, font=("Segoe UI", 10), anchor="w", justify="left", wraplength=510
    )
    summary_label.pack(fill="x", pady=(0, 12))
    obs_running_notice = tk.Label(
        ready_page, text="", bg=PAGE_BG, fg="#b45309", font=("Segoe UI", 9, "italic"),
        anchor="w", wraplength=510, justify="left",
    )
    obs_running_notice.pack(fill="x", pady=(0, 12))
    progress_label = tk.Label(ready_page, text="", bg=PAGE_BG, font=("Segoe UI", 9), fg="#555555", anchor="w")
    progress_label.pack(fill="x")
    progress_bar = ttk.Progressbar(ready_page, mode="indeterminate")

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
    existing_progress_label = tk.Label(
        existing_page, text="", bg=PAGE_BG, font=("Segoe UI", 9), fg="#555555", anchor="w",
    )
    existing_progress_label.pack(fill="x", pady=(4, 0))
    existing_progress_bar = ttk.Progressbar(existing_page, mode="indeterminate")

    # ---------- Finish ----------
    finish_page = page_frame()
    register("finish", finish_page)
    finish_heading = tk.Label(finish_page, text="", bg=PAGE_BG, font=("Segoe UI", 12, "bold"), anchor="w")
    finish_heading.pack(fill="x", pady=(0, 12))
    finish_body = tk.Label(
        finish_page, text="", bg=PAGE_BG, font=("Segoe UI", 10), anchor="w", justify="left", wraplength=510
    )
    finish_body.pack(fill="x", pady=(0, 12))
    # Shown only when manual WebSocket setup is needed (see show_install_finish) -- a real Entry,
    # not just more Label text, specifically so the password can actually be selected and copied
    # rather than retyped by hand into OBS.
    password_row = tk.Frame(finish_page, bg=PAGE_BG)
    password_var = tk.StringVar(value="")
    password_entry = tk.Entry(password_row, textvariable=password_var, width=28, state="readonly")
    password_entry.pack(side="left")
    password_copied_label = tk.Label(password_row, text="", bg=PAGE_BG, fg="#15803d", font=("Segoe UI", 9))

    def copy_password():
        root.clipboard_clear()
        root.clipboard_append(password_var.get())
        password_copied_label.config(text="Copied!")
        root.after(2000, lambda: password_copied_label.config(text=""))

    tk.Button(password_row, text="Copy", command=copy_password).pack(side="left", padx=(8, 8))
    password_copied_label.pack(side="left")
    launch_var = tk.BooleanVar(value=True)
    launch_check = tk.Checkbutton(finish_page, text=f"Launch {APP_NAME} now", variable=launch_var, bg=PAGE_BG)
    launch_check.pack(anchor="w", pady=(8, 0))

    # ---------- Navigation ----------
    order = ["welcome", "location", "obs", "options", "games", "advanced", "ready", "finish"]
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
            "multi_track_audio": multi_track_var.get(),
            "output_folder": output_folder_var.get().strip(),
            "game_audio_isolation": game_audio_var.get(),
            "replay_buffer": replay_buffer_var.get(),
            "disk_space_guard": disk_guard_var.get(),
            "disk_space_guard_min_gb": disk_guard_min_gb_var.get().strip(),
            "selected_games": [name for name, var in game_vars.items() if var.get()],
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
            selected_games = [name for name, var in game_vars.items() if var.get()]
            games_note = ", ".join(selected_games) if selected_games else "none selected"
            summary_label.config(
                text=(
                    f"Install folder: {dir_var.get()}{preserved_note}\n"
                    f"OBS location: {obs_var.get() or 'not set yet'}\n"
                    f"Games to watch: {games_note}\n"
                    f"Desktop shortcut: {'Yes' if desktop_var.get() else 'No'}\n"
                    f"Start with Windows: {'Yes' if startup_var.get() else 'No'}"
                )
            )
            # Checked here (before install starts) rather than only reported after the fact on
            # the finish page -- try_configure_obs_websocket silently skips whenever OBS is
            # running, so without this notice the first the user hears about needing a manual
            # WebSocket setup step is after install has already finished.
            if is_obs_running():
                obs_running_notice.config(
                    text=(
                        "⚠  OBS is currently running -- its WebSocket server can't be configured "
                        "automatically while it's open. Close OBS first if you'd like that done for "
                        "you, or continue and set the password manually afterward (shown on the "
                        "finish page)."
                    )
                )
            else:
                obs_running_notice.config(text="")
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
        progress_bar.pack(fill="x", pady=(6, 0))
        progress_bar.start(12)

        def worker():
            return do_install(dir_var.get(), obs_var.get() or None, collect_options(), report_progress)

        def report_progress(message):
            root.after(0, lambda: progress_label.config(text=message))

        def done(result, error):
            progress_bar.stop()
            progress_bar.pack_forget()
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
                "Settings, turn it on, and set the password to the one below.\n"
                "(Or copy whatever password OBS already shows there into this app's Settings instead.)"
            )
        ffmpeg_status = result.get("ffmpeg_status")
        if ffmpeg_status == "installed":
            lines.append(
                "\nffmpeg was also installed automatically, so you're ready to convert MKV "
                "recordings to MP4 later from Settings → Post-Processing, if you ever want to."
            )
        elif ffmpeg_status == "manual_only":
            command = build_linux_ffmpeg_command()
            if command:
                lines.append(
                    "\nffmpeg isn't installed -- it's only needed if you want to convert MKV "
                    f"recordings to MP4 later. Run this command to install it:\n\n    {command}\n\n"
                    "and this app will find it on its own, or point Settings → Post-Processing at "
                    "it directly."
                )
            else:
                lines.append(
                    "\nffmpeg wasn't found and no supported package manager was detected to "
                    "install it automatically -- it's only needed if you want to convert MKV "
                    "recordings to MP4 later. Install it via your distro's package manager, then "
                    "this app will find it on its own, or point Settings → Post-Processing at it "
                    "directly."
                )
        elif ffmpeg_status in ("no_package_manager", "install_failed"):
            lines.append(
                "\nffmpeg wasn't found and couldn't be installed automatically -- it's only needed "
                f"if you want to convert MKV recordings to MP4 later. Install it from {FFMPEG_DOWNLOAD_URL} "
                "and this app will find it on its own, or point Settings → Post-Processing at it directly."
            )
        finish_body.config(text="\n".join(lines))
        if result["password"] is not None and not result["obs_ws_configured"]:
            password_var.set(result["password"])
            password_copied_label.config(text="")
            password_row.pack(anchor="w", pady=(0, 12))
        else:
            password_row.pack_forget()
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
        password_row.pack_forget()
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
        existing_error.config(text="")
        existing_progress_bar.pack(fill="x", pady=(4, 0))
        existing_progress_bar.start(12)

        def report_progress(message):
            root.after(0, lambda: existing_progress_label.config(text=message))

        def worker():
            return do_install(dir_var.get(), None, collect_options(), report_progress, write_config=False)

        def done(result, error):
            existing_progress_bar.stop()
            existing_progress_bar.pack_forget()
            existing_progress_label.config(text="")
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
        existing_error.config(text="")
        existing_progress_bar.pack(fill="x", pady=(4, 0))
        existing_progress_bar.start(12)

        def report_progress(message):
            root.after(0, lambda: existing_progress_label.config(text=message))

        def worker():
            do_uninstall(dir_var.get(), keep_config_var.get(), report_progress)

        def done(_result, error):
            existing_progress_bar.stop()
            existing_progress_bar.pack_forget()
            existing_progress_label.config(text="")
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
    skip_advanced_link.bind("<Button-1>", lambda _event: go_to_index(order.index("ready")))

    def finish():
        if launch_var.get() and current["install_dir"]:
            try:
                subprocess.Popen([os.path.join(current["install_dir"], app_relative_path())], cwd=current["install_dir"])
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
