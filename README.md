# OBS Auto Recorder

A lightweight Windows background watcher that automatically starts and stops OBS recording when you launch or close a game. Runs from the system tray, launches OBS if it isn't already open, and renames finished recordings with the game's name.

## Features

- Watches for a configurable list of game processes (e.g. `cs2.exe`, `valorant.exe`, `league of legends.exe`)
- Also auto-detects **any game installed via Steam**, by scanning your Steam library folders — no need to list every Steam game by hand
- Also auto-detects **any game installed via the Epic Games Launcher**, by reading its install manifests — same idea, no need to list every Epic game by hand
- Launches OBS automatically if it isn't running, and connects to it over OBS's WebSocket API
- Starts recording when a watched game launches, stops when it exits
- Renames the finished recording to `"<Game Name> - <original filename>.mp4"`
- Clears OBS's "unclean shutdown" crash-recovery sentinel before launching, so a prior forced close (e.g. Task Manager, crash, power loss) doesn't pop OBS's crash dialog and stall automation
- System tray icon showing live status (gray = watching, red = recording, gold = recording file just split, orange = internal error — check the log)
- Optional floating always-on-top overlay, pinned to any monitor of your choice via the tray icon's right-click menu, showing the same status color
- Packaged as a single standalone `OBSAutoRecorder.exe` so it's easy to identify and kill in Task Manager (not a generic `python.exe`/`pythonw.exe` process)

## Requirements

- Windows 10/11
- [OBS Studio](https://obsproject.com/) with the built-in WebSocket server (OBS 28+)
- Python 3.10+ (only needed if you want to run from source or rebuild the `.exe`)

## Setup

### 1. Enable OBS WebSocket

In OBS: **Tools → WebSocket Server Settings**
- Enable WebSocket server
- Note the **port** (default `4455`) and set/copy the **password**

### 2. Configure

Copy the example config and fill in your values:

```
copy config.example.json config.json
```

Edit `config.json`:

| Field | Description |
|---|---|
| `watched_games` | Process names (case-insensitive) to trigger recording, e.g. `"cs2.exe"` |
| `watched_windows` | For games that share a generic process name (e.g. Java Edition Minecraft runs as `javaw.exe`, same as any Java app) — matches by process name **and** a substring of its window title. See below. |
| `poll_interval_seconds` | How often to check running processes |
| `steam.enabled` | Auto-detect any game running from a Steam library folder |
| `steam.allowed_drives` | Only scan Steam libraries on these drive letters |
| `steam.exclude_keywords` | Substrings in an exe's path that disqualify it from being treated as a game (anti-cheat installers, redistributables, background apps like Wallpaper Engine, etc.) |
| `epic.enabled` | Auto-detect any game installed via the Epic Games Launcher |
| `epic.exclude_keywords` | Substrings in an installed title's display name that disqualify it from being treated as a game (e.g. Unreal Engine editor installs) |
| `obs.path` | Full path to `obs64.exe` |
| `obs.launch_args` | Extra command-line args OBS is launched with |
| `obs.startup_wait_seconds` | How long to wait after launching OBS before trying to connect |
| `obs.websocket.host` / `port` / `password` | Must match OBS's WebSocket Server Settings |
| `obs.auto_split.enabled` | Set to `true` if you've enabled OBS's **Automatically split file** option (see below), so its splits aren't mislabeled as manual |
| `obs.auto_split.by` | `"time"` or `"size"` — must match what you set in OBS's automatic split setting |
| `obs.auto_split.minutes` / `tolerance_seconds` | When `by` is `"time"`: the split interval you set in OBS, and how many seconds of slack to allow when matching a split against it |
| `obs.auto_split.megabytes` / `tolerance_megabytes` | When `by` is `"size"`: the split size you set in OBS, and how much overshoot to allow when matching a split against it |
| `log_file` | Log file name (relative to the exe's folder, or an absolute path) |

`config.json` is gitignored since it contains your WebSocket password, never commit it in any forks of this repo.

**`watched_windows` example** (Java Edition Minecraft, launched via any launcher):

```json
"watched_windows": [
    {
        "process_name": "javaw.exe",
        "title_contains": "minecraft",
        "display_name": "Minecraft"
    }
]
```

`display_name` is optional and controls the game name used in the recording filename and log messages; without it, the process name is used instead. This costs a little extra overhead per poll (a window-title scan), so it's only used as a fallback after `watched_games`, Steam detection, and Epic detection all miss.

### 3. Get the app running

**Option A — use the prebuilt exe** (if `OBSAutoRecorder.exe` is already present): just run it.

**Option B — build it yourself**:

```
pip install -r requirements.txt
pyinstaller --onefile --noconsole --name OBSAutoRecorder --distpath . --workpath build --specpath build autostart_script.py
```

This produces `OBSAutoRecorder.exe` in the project folder, alongside `config.json` (it reads config from its own directory). Rebuild any time you change `autostart_script.py`.

You can also run it directly without building, for testing:

```
python autostart_script.py
```

### 4. Run automatically at login

Create a shortcut to `OBSAutoRecorder.exe` in your Startup folder:

1. Press `Win+R`, enter `shell:startup`, hit Enter
2. Create a shortcut there pointing to `OBSAutoRecorder.exe`

It will now launch silently (no console window) every time you log in.

## Using the tray icon

Right-click the tray icon (it may be tucked under the "show hidden icons" `^` chevron — drag it out if you want it always visible) for:

- Current status (watching / recording which game)
- **Overlay Monitor** — pick a monitor to pin a small floating color-status square to, or "Off" to disable it
- **Quit** — stops any active recording cleanly, then exits

Icon colors:
- **Gray** — watching, idle
- **Red** — recording
- **Gold** (~5s) — a recording file just auto-split
- **Orange** — the watcher crashed; check `autostart_script.log`

## Optional: split recording files

You can optionally enable OBS's file-splitting so a long session isn't stuck in one giant file. Once set up, your existing **Split Recording File** hotkey can be used while in-game to split off a new file at any time — the tray icon (and overlay, if enabled) will flash gold for a few seconds each time a split happens.

- **Manual**: OBS Settings → Hotkeys → set a **Split Recording File** hotkey. Files from a manual split are renamed with a `Split N` tag (e.g. `Game - Split 1 - filename.mp4`), and if any manual split happened during the session, the final segment gets a `Split N` tag too.
- **Automatic**: OBS Settings → Output (Advanced mode) → Recording → enable **Automatically split file**, with a time or size limit.

OBS's WebSocket API doesn't report *why* a file split happened, so the script can't natively tell a manual split from an automatic one. If you use OBS's automatic splitting, set `obs.auto_split` in `config.json` to match it (interval/size and, optionally, its trigger-matching tolerance) so the script can recognize those splits and skip labeling them — any split that doesn't match your configured automatic settings is assumed to be manual. Leave `obs.auto_split.enabled` at `false` (the default) if you only use the manual hotkey; every split will then be treated as manual.

## Known limitations

- Windows has one system tray total — it isn't per-monitor. Use the floating overlay if you need status visible on a specific monitor.
- Steam-game auto-detection matches by folder location, not a games database, so a handful of non-game Steam apps may need adding to `steam.exclude_keywords` if they cause false positives (Wallpaper Engine is excluded by default).
- Epic-game auto-detection reads the Epic Games Launcher's local install manifests (`%PROGRAMDATA%\Epic\EpicGamesLauncher\Data\Manifests`), so a game only shows up once it's been installed at least once through the launcher.
