# OBS Auto Recorder

A lightweight Windows background watcher that automatically starts and stops OBS recording when you launch or close a game. Runs from the system tray, launches OBS if it isn't already open, and renames finished recordings with the game's name.

## Features

- Watches for a configurable list of game processes (e.g. `cs2.exe`, `valorant.exe`, `league of legends.exe`)
- Also auto-detects **any game installed via Steam**, by scanning your Steam library folders — no need to list every Steam game by hand
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
| `poll_interval_seconds` | How often to check running processes |
| `steam.enabled` | Auto-detect any game running from a Steam library folder |
| `steam.allowed_drives` | Only scan Steam libraries on these drive letters |
| `steam.exclude_keywords` | Substrings in an exe's path that disqualify it from being treated as a game (anti-cheat installers, redistributables, background apps like Wallpaper Engine, etc.) |
| `obs.path` | Full path to `obs64.exe` |
| `obs.launch_args` | Extra command-line args OBS is launched with |
| `obs.startup_wait_seconds` | How long to wait after launching OBS before trying to connect |
| `obs.websocket.host` / `port` / `password` | Must match OBS's WebSocket Server Settings |
| `log_file` | Log file name (relative to the exe's folder, or an absolute path) |

`config.json` is gitignored since it contains your WebSocket password — never commit it.

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

- **Manual**: OBS Settings → Hotkeys → set a **Split Recording File** hotkey.
- **Automatic**: OBS Settings → Output (Advanced mode) → Recording → enable **Automatically split file**, with a time or size limit.

## Known limitations

- Only the *final* segment of a split recording gets the game-name prefix; earlier split segments keep OBS's default filename.
- Windows has one system tray total — it isn't per-monitor. Use the floating overlay if you need status visible on a specific monitor.
- Steam-game auto-detection matches by folder location, not a games database, so a handful of non-game Steam apps may need adding to `steam.exclude_keywords` if they cause false positives (Wallpaper Engine is excluded by default).
