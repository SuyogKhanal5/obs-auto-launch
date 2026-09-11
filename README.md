# OBS Auto Recorder

A lightweight Windows background watcher that automatically starts and stops OBS recording when you launch or close a game. Runs from the system tray, launches OBS if it isn't already open, and renames finished recordings with the game's name.

## Features

- Watches for a configurable list of game processes (e.g. `cs2.exe`, `valorant.exe`, `league of legends.exe`)
- Auto-detects **any game installed via Steam**, by scanning your Steam library folders — no need to list every Steam game by hand
- Auto-detects **any game installed via the Epic Games Launcher**, by reading its install manifests — same idea, no need to list every Epic game by hand
- Auto-detects **any game installed via GOG Galaxy** (registry-based), **the Xbox app/PC Game Pass** (folder-based, defaults to `C:\XboxGames`), and optionally **Battle.net** (folder-based, needs its install folders listed) — see [Optional: more launchers](#optional-more-launchers)
- Launches OBS automatically if it isn't running, and connects to it over OBS's WebSocket API
- Starts recording when a watched game launches, stops when it exits
- Renames the finished recording to `"<Game Name> - <original filename>.mp4"`, or optionally files it into a per-game subfolder instead (see `organize_into_game_subfolders`)
- Optional disk-space guard: skips starting a recording (and alerts) if free disk space is below a configured threshold, instead of starting a recording that can't finish (see [Optional: disk space guard](#optional-disk-space-guard))
- Optional cleanup: deletes recordings shorter than a configured length (e.g. an accidental game launch), and flags/tags recordings where no audio was ever detected — checked live during recording, not just after the fact (see [Optional: recording cleanup](#optional-recording-cleanup))
- Optional replay buffer support: starts/stops OBS's replay buffer alongside recording, with a "Save Replay Buffer" tray action (see [Optional: replay buffer](#optional-replay-buffer))
- Optional post-record transcode: runs a finished recording through `ffmpeg` in the background (e.g. to compress it, or remux MKV to MP4 while preserving every audio track) once it's done (see [Optional: post-record transcode](#optional-post-record-transcode))
- Optional manual split with a delay: adds a "Split Recording File" tray item that splits the current recording after a configurable buffer, independent of any OBS hotkey (see [Optional: split recording files](#optional-split-recording-files))
- Optional custom keybinds: set your own system-wide key combos (e.g. `Ctrl+Alt+S`) that call OBS actions like Split Recording File, Save Replay Buffer, or Pause Recording directly over the WebSocket, independent of OBS's own Hotkeys settings (see [Optional: custom keybinds](#optional-custom-keybinds))
- Optional Windows toast notifications for key events (recording started/stopped, OBS restarted, low disk space, no audio detected) via the tray icon (see [Optional: notifications](#optional-notifications))
- Clears OBS's "unclean shutdown" crash-recovery sentinel before launching, so a prior forced close (e.g. Task Manager, crash, power loss) doesn't pop OBS's crash dialog and stall automation
- Detects a hung OBS (process running but its WebSocket stops responding) or a memory-bloated idle OBS, and automatically kills and relaunches it instead of silently failing to record
- Optionally isolates each detected game's audio from Discord/Spotify/etc. by repointing an OBS Application Audio Capture source at it — creating that source automatically if it doesn't exist yet (see [Optional: isolate game audio](#optional-isolate-game-audio))
- Optional multi-track audio: routes mic/desktop/game audio (or any inputs you pick) to separate recording tracks for editing later, entirely inside its own dedicated OBS profile so your existing profile is never touched — use **MKV** as the recording format if you turn this on (see [Optional: multi-track audio](#optional-multi-track-audio))
- System tray icon showing live status (gray = watching, red = recording, green = recording file just split, orange = internal error or OBS recovery in progress — check the log), plus shortcuts to open the recordings folder and log file
- Built-in GUI settings editor (tray icon → **Edit Settings...**) for every `config.json` option — no manual JSON editing required (see [Optional: settings editor](#optional-settings-editor))
- Optional floating always-on-top overlay, pinned to any monitor of your choice via the tray icon's right-click menu, showing the same status color
- Packaged as a single standalone `OBSAutoRecorder.exe` so it's easy to identify and kill in Task Manager (not a generic `python.exe`/`pythonw.exe` process)
- A plain-language installer (`OBSAutoRecorderInstaller.exe`) for non-technical users: picks an install folder, finds (or lets you browse to) OBS, offers a few simple preferences, optionally creates a desktop icon and/or sets up auto-start, and even tries to configure OBS's WebSocket server for you automatically — see [Quick install](#quick-install-recommended)

## Requirements

- Windows 10/11
- [OBS Studio](https://obsproject.com/) with the built-in WebSocket server (OBS 28+) — doesn't need to be installed before you run the installer, just before you actually want to record
- Python 3.10+ (only needed if you want to run from source or build the exes yourself — not needed to use the [prebuilt releases](../../releases/latest))

## Quick install (recommended)

This is the whole setup for most people — five minutes, no file editing, nothing to type by hand:

1. **Install OBS Studio first**, if you haven't already — [obsproject.com](https://obsproject.com/), or let the installer do it for you in step 3 below. You don't need to configure anything inside it yet.
2. Download **`OBSAutoRecorderInstaller.exe`** from the [Releases page](../../releases/latest) and run it.
3. Click through the wizard:
   - **Choose where to install** — the default (Program Files) is fine for almost everyone.
   - **Locate OBS Studio** — it finds this automatically in the common install locations; if it can't and you have `winget` (most Windows 10/11 PCs do), an **Easy Install** button installs OBS Studio for you right there — otherwise, click **Browse...** if it's already installed elsewhere, or use the download link on that page.
   - **A few quick preferences** — plain yes/no toggles (split long recordings, delete accidental short clips, notifications, per-game folders). All changeable later.
   - **Advanced options** — multi-track audio, game audio isolation, the replay buffer, the disk space guard, a custom recording folder. Everything here is off by default and safe to skip entirely with the **Skip this page** link — come back to it later from **Edit Settings...** once you know you want one of these.
4. Click **Install**, then **Finish** (leave "Launch OBS Auto Recorder now" checked).
5. **Check it worked**: launch any game installed via Steam, Epic, GOG, or the Xbox app — these are auto-detected, nothing to configure first. The tray icon should turn red within a couple of seconds, and OBS should start recording. Close the game and confirm a new recording file appears, renamed with the game's name, in OBS's recording folder. (A game from somewhere else, e.g. Riot's client or a standalone installer? Add it via **Edit Settings... → Watched Games → Common Games...** or **Pick Running...** first — see [Optional: settings editor](#optional-settings-editor).)

The installer also tries to configure OBS's WebSocket server for you automatically (needs OBS to have been run at least once already, and to not be running during install). If it can't, it tells you the one manual step left — see the [Steps to take inside OBS](#steps-to-take-inside-obs) table below for exactly what to click.

It also installs `ffmpeg` for you automatically via `winget` if it isn't already on your PC — this only matters if you later turn on [post-record transcode](#optional-post-record-transcode) (e.g. to convert MKV recordings to MP4), so most people can ignore it entirely. No `winget`? The Finish page just tells you where to grab it manually instead; nothing about the rest of setup depends on it.

Running the installer again later lets you update or uninstall — it detects an existing install and asks which you want, and an update never touches your existing settings.

Everything below is for people who want more control: what (if anything) needs setting up inside OBS itself, building from source, installing by hand, or understanding every `config.json` option (the same options are also available from the app's own **Edit Settings...** tray menu once it's running).

## Steps to take inside OBS

Nothing here is required for basic recording — the app auto-configures OBS's WebSocket connection for you and works out of the box. These are the *only* things you'd ever do by hand directly inside OBS itself, and only if you want the specific feature next to them:

| If you want to... | Do this inside OBS |
|---|---|
| Just record games automatically (the basics) | Nothing — skip this whole table |
| Fix the WebSocket connection, if the installer's auto-setup couldn't | **Tools → WebSocket Server Settings** → enable it, note the port/password. See [Enable OBS WebSocket](#1-enable-obs-websocket) |
| [Isolate game audio](#optional-isolate-game-audio) from Discord/Spotify/etc. | Nothing — the app creates the Application Audio Capture source in OBS for you |
| Use [multi-track audio](#optional-multi-track-audio) (separate audio per source) | Nothing to add manually — the **Quick Setup** wizard (in **Edit Settings...**) creates any OBS sources it needs for you. **Set your recording format to `mkv`** though (easiest: the app's own **Settings → OBS → Recording format** field, not OBS's UI — see below) |
| Use [automatic file splitting](#optional-split-recording-files) | **Settings → Output** (Advanced mode) → **Recording** → enable **Automatically split file** |
| Set a manual **split** hotkey instead | OBS's own **Settings → Hotkeys** → set **Split Recording File** — or skip OBS's hotkey entirely and use this app's own **Split Recording File** tray item or a [custom keybind](#optional-custom-keybinds) instead (see [Optional: split recording files](#optional-split-recording-files)) |
| Use the [replay buffer](#optional-replay-buffer) | **Settings → Output** → **Replay Buffer** → set its length and save location |
| Trigger an OBS action (split, replay buffer, pause, mute, etc.) from your own key combo | Nothing to set up in OBS — **Edit Settings... → Custom Keybinds** tab (see [Optional: custom keybinds](#optional-custom-keybinds)) |

**About that MKV recommendation:** if you turn on multi-track audio, only some recording formats reliably embed *every* separate audio track in every OBS version — MKV always has. Rather than digging into OBS's own Advanced Output settings, just set this app's own `obs.recording_format` to `mkv` (**Edit Settings... → OBS tab → Recording format**, or type `mkv` directly in `config.json`) — the app applies it to OBS for you, the same way it applies the recording folder. If you skip this and only ever see one audio track in your finished recordings, this is almost certainly why.

## Manual setup

### 1. Enable OBS WebSocket

In OBS: **Tools → WebSocket Server Settings**
- Enable WebSocket server
- Note the **port** (default `4455`) and set/copy the **password**

(The installer above tries to do this step for you automatically — only needed if you're setting up by hand, or the automatic step couldn't find OBS's config.)

### 2. Configure

Get `config.example.json` — it's attached to each [Release](../../releases/latest) alongside the exe, or you can grab it from this repo. Put it next to `OBSAutoRecorder.exe` and rename your copy to `config.json`:

```
copy config.example.json config.json
```

Edit `config.json`:

| Field | Description |
|---|---|
| `watched_games` | Process names (case-insensitive) to trigger recording, e.g. `"cs2.exe"` |
| `watched_windows` | For games that share a generic process name (e.g. Java Edition Minecraft runs as `javaw.exe`, same as any Java app) — matches by process name **and** a substring of its window title. See below. |
| `poll_interval_seconds` | How often to check running processes |
| `organize_into_game_subfolders` | If `true`, finished recordings are filed into a `<Game Name>\` subfolder instead of being prefixed with the game name in-place (see [Optional: game subfolders](#optional-game-subfolders)) |
| `steam.enabled` | Auto-detect any game running from a Steam library folder |
| `steam.allowed_drives` | Only scan Steam libraries on these drive letters |
| `steam.exclude_keywords` | Substrings in an exe's path that disqualify it from being treated as a game (anti-cheat installers, redistributables, background apps like Wallpaper Engine, etc.) |
| `epic.enabled` | Auto-detect any game installed via the Epic Games Launcher |
| `epic.exclude_keywords` | Substrings in an installed title's display name that disqualify it from being treated as a game (e.g. Unreal Engine editor installs) |
| `gog.enabled` | Auto-detect any game installed via GOG Galaxy (read from the registry, no setup needed) |
| `gog.exclude_keywords` | Substrings in an installed title's display name that disqualify it from being treated as a game |
| `xbox.enabled` | Auto-detect any game installed via the Xbox app / PC Game Pass |
| `xbox.install_dirs` | Folders to scan, one subfolder per game (defaults to `["C:\\XboxGames"]`, the default Xbox app install location) |
| `xbox.exclude_keywords` | Substrings in an exe's path that disqualify it from being treated as a game |
| `battlenet.enabled` | Auto-detect any game under the configured Battle.net folders |
| `battlenet.install_dirs` | Folders to scan, one subfolder per game. Battle.net has no shared install root or manifest to read automatically, so this only detects anything once you list where your Battle.net games live |
| `battlenet.exclude_keywords` | Substrings in an exe's path that disqualify it from being treated as a game |
| `disk_space_guard.enabled` | Skip starting a recording (and show an error) if free disk space is below `minimum_free_gb` (see [Optional: disk space guard](#optional-disk-space-guard)) |
| `disk_space_guard.minimum_free_gb` | Minimum free space required to start recording. Defaults to `10` if omitted |
| `disk_space_guard.path` | Drive/folder to check free space on. Defaults to the drive OBS is installed on if omitted — set this to your actual recordings drive if it's different |
| `cleanup.delete_short_clips.enabled` | Delete a recording instead of keeping it if the whole session was shorter than `minimum_seconds` (see [Optional: recording cleanup](#optional-recording-cleanup)) |
| `cleanup.delete_short_clips.minimum_seconds` | Minimum session length to keep a recording. Defaults to `20` if omitted |
| `cleanup.flag_silent_recordings.enabled` | Watch OBS's live audio meters while recording; if no input ever crosses `peak_threshold`, warn after `warn_after_seconds` and tag the finished file with a `[NO AUDIO]` prefix |
| `cleanup.flag_silent_recordings.peak_threshold` | Minimum audio peak (0.0-1.0) that counts as "audio detected". Defaults to `0.02` if omitted |
| `cleanup.flag_silent_recordings.warn_after_seconds` | How long with no audio before logging/notifying. Defaults to `30` if omitted |
| `notifications.enabled` | Show Windows toast notifications (via the tray icon) for recording start/stop, OBS restarts, low disk space, and no-audio warnings (see [Optional: notifications](#optional-notifications)) |
| `post_record_transcode.enabled` | Run each finished recording through `ffmpeg` in the background once it's done (see [Optional: post-record transcode](#optional-post-record-transcode)) |
| `post_record_transcode.ffmpeg_path` | Path to `ffmpeg`, or just `"ffmpeg"` if it's on your `PATH` (the default). If it can't be resolved, the app automatically falls back to searching common install locations (winget/Chocolatey/Scoop) — see **Auto-detect ffmpeg** in [Optional: post-record transcode](#optional-post-record-transcode) |
| `post_record_transcode.args` | Extra `ffmpeg` arguments between the input and output file, e.g. codec/quality settings. Include `-map 0` (as the default args do) if you want every track carried over — without it, `ffmpeg` only keeps one "best" audio track and silently drops the rest, which matters if you're using [multi-track audio](#optional-multi-track-audio) |
| `post_record_transcode.output_extension` | Container extension for the transcoded output, e.g. `.mp4`. Leave unset/blank to keep the original file's extension. See [MKV → MP4 preset](#optional-post-record-transcode) below for converting a multi-track MKV recording to MP4 without re-encoding |
| `post_record_transcode.suffix` | Appended to the filename (before the extension) for the transcoded output, so it doesn't collide with the original |
| `post_record_transcode.delete_original` | Delete the original recording once the transcode succeeds |
| `obs.process_name` | Process name to watch for/kill when managing OBS, e.g. `"obs64.exe"` (the default) — only needs changing for a non-standard OBS build |
| `obs.path` | Full path to `obs64.exe` |
| `obs.launch_args` | Extra command-line args OBS is launched with |
| `obs.startup_wait_seconds` | How long to wait after launching OBS before trying to connect |
| `obs.output_folder` | Recording output folder to enforce in OBS (created automatically if missing). Leave unset/blank to leave OBS's own recording folder setting alone |
| `obs.recording_format` | Recording container format to enforce in OBS, using OBS's own internal format code (`mp4`, `mkv`, `mov`, `hybrid_mp4`, `fragmented_mp4`, `fragmented_mov`, `flv`, `ts`, `hls`, or any other code your OBS version supports). Leave unset/blank to leave OBS's own format setting alone |
| `obs.websocket.host` / `port` / `password` | Must match OBS's WebSocket Server Settings |
| `obs.auto_split.enabled` | Set to `true` if you've enabled OBS's **Automatically split file** option (see below), so its splits aren't mislabeled as manual |
| `obs.auto_split.by` | `"time"` or `"size"` — must match what you set in OBS's automatic split setting |
| `obs.auto_split.minutes` / `tolerance_seconds` | When `by` is `"time"`: the split interval you set in OBS, and how many seconds of slack to allow when matching a split against it |
| `obs.auto_split.megabytes` / `tolerance_megabytes` | When `by` is `"size"`: the split size you set in OBS, and how much overshoot to allow when matching a split against it |
| `obs.manual_split.enabled` | Add a "Split Recording File" tray menu item that splits the current recording on demand (see [Optional: split recording files](#optional-split-recording-files)) |
| `obs.manual_split.buffer_seconds` | Delay, in seconds, between clicking "Split Recording File" and the split actually happening. Defaults to `0` (immediate) if omitted |
| `obs.custom_keybinds` | List of system-wide key combos this app itself listens for, each calling an OBS action directly over the WebSocket (see [Optional: custom keybinds](#optional-custom-keybinds)). Defaults to `[]` (none) if omitted |
| `obs.recovery.memory_limit_gb` | Restart OBS if its memory usage exceeds this while idle (see [OBS health recovery](#obs-health-recovery)). Defaults to `5` if omitted |
| `obs.recovery.cooldown_seconds` | Minimum time between automatic OBS restarts, whether triggered by a hang or by memory. Defaults to `30` if omitted |
| `obs.game_audio_capture.enabled` | Repoint an existing **Application Audio Capture** source at the detected game each time recording starts, to isolate its audio (see [Optional: isolate game audio](#optional-isolate-game-audio)) |
| `obs.game_audio_capture.input_name` | Name of that source in your OBS scene, exactly as it appears in OBS |
| `obs.multi_track_audio.enabled` | Route configured inputs to separate recording tracks, inside a dedicated OBS profile (see [Optional: multi-track audio](#optional-multi-track-audio)) |
| `obs.multi_track_audio.profile_name` | Name of the dedicated OBS profile to create/use for this — created automatically the first time, cloned from whatever profile is active then. Defaults to `"OBS Auto Recorder"` if omitted |
| `obs.multi_track_audio.tracks` | List of `{"input_name": "...", "track": N}` entries (`N` is `1`-`6`) — which OBS input feeds which recording track. Names must match an existing OBS input exactly |
| `obs.multi_track_audio.app_captures` | List of `{"input_name": "...", "process_name": "..."}` entries re-pointed at that exe on every recording start, so an Application Audio Capture input (Discord, Spotify, etc.) can't silently drift onto a stale window match (see [Optional: multi-track audio](#optional-multi-track-audio)). Populated automatically by Quick Setup; defaults to `[]` if omitted |
| `obs.replay_buffer.enabled` | Start/stop OBS's replay buffer alongside recording, and add a "Save Replay Buffer" tray action (see [Optional: replay buffer](#optional-replay-buffer)) |
| `log_file` | Log file name (relative to the exe's folder, or an absolute path). Capped at the most recent 10,000 lines — older lines are dropped as new ones are added |

`config.json` is gitignored since it contains your WebSocket password, never commit it in any forks of this repo.

Editing `config.json` never requires rebuilding `OBSAutoRecorder.exe` — it's a plain file read from disk, not baked into the executable. It's only read once at startup, though, so **restart the app** (Quit from the tray icon, then relaunch) for a config change to take effect. Rebuilding is only needed after editing `autostart_script.py` itself.

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

**Option A — use the prebuilt build directly** (no installer, e.g. for a portable/USB setup): download `OBSAutoRecorder.zip` from the [Releases page](../../releases/latest), extract it (an `OBSAutoRecorder.exe` alongside an `_internal/` folder — both need to stay together, in their own folder), add a `config.json` next to the exe (see step 2), and run it. No Python needed.

**Option B — build it yourself**:

```
pip install -r requirements.txt
pyinstaller --onedir --noconsole --name OBSAutoRecorder --distpath . --workpath build --specpath build autostart_script.py
```

This produces an `OBSAutoRecorder/` folder in the project folder (an exe plus an `_internal/` support-files folder — both required, don't separate them), with `config.json` read from that same folder. Rebuild any time you change `autostart_script.py`.

Built as onedir rather than onefile deliberately: a onefile exe re-extracts itself to a fresh `%TEMP%` folder on *every single launch*, and antivirus real-time scanning can intermittently fail to release a newly-extracted file in time for that folder to be cleaned up afterward — a widely-reported PyInstaller/Windows Defender interaction, not a bug specific to this app, but one this app hits particularly often since it relaunches itself on every settings save (the Settings editor's "Save and Restart", and "Restart App" from the tray menu). Onedir runs directly from wherever it's installed, so that whole failure mode doesn't exist. The tradeoff is a folder instead of a single file — the installer hides that from end users by embedding and installing the whole folder, so downloading and running `OBSAutoRecorderInstaller.exe` is still a one-file experience.

To also build the installer (`OBSAutoRecorderInstaller.exe`), build the app as above, then:

```
pyinstaller --onefile --noconsole --uac-admin --name OBSAutoRecorderInstaller --distpath . --workpath build --specpath build --add-data "<full path to the OBSAutoRecorder folder>;OBSAutoRecorder" --add-data "<full path to config.example.json>;." installer.py
```

The `--add-data` source paths must be absolute (PyInstaller resolves relative ones against `--specpath`, not your working directory). `--uac-admin` makes the installer prompt for admin rights on launch, needed for its default install location (Program Files).

A GitHub Actions workflow ([.github/workflows/build-release.yml](.github/workflows/build-release.yml)) does all of this automatically:
- Pushing a `vX.Y.Z` tag builds both exes and publishes them, alongside `config.example.json`, as a new versioned GitHub Release.
- Every push to `main` builds both exes and republishes them to a rolling [`latest`](../../releases/tag/latest) pre-release, so the newest code is always available even between tagged versions. The [Releases page](../../releases/latest) itself still points at the newest *tagged* release, since the rolling build is marked as a pre-release.

You can also run either one directly without building, for testing:

```
python autostart_script.py
python installer.py
```

### Running the test suite

```
python -m unittest discover -s tests -v
```

No extra dependencies beyond `requirements.txt` — the tests use the standard library's `unittest` and a small fake OBS WebSocket client ([tests/fakes.py](tests/fakes.py)) instead of a real OBS connection, so they run in well under a second and don't need OBS, a GUI, or any game running. They cover the pure logic that's cheapest to get wrong silently: game/launcher detection and matching priority ([tests/test_detection.py](tests/test_detection.py)), multi-track audio routing and profile setup — including that removing an input from `obs.multi_track_audio.tracks` actually clears its routing in OBS rather than leaving it stale ([tests/test_multi_track_audio.py](tests/test_multi_track_audio.py)), the multi-track quick-setup wizard's track-building/source-creation logic ([tests/test_quick_setup.py](tests/test_quick_setup.py)), the installer's config-building logic ([tests/test_installer.py](tests/test_installer.py)), and auto-split/disk-guard/config-parsing helpers ([tests/test_config_helpers.py](tests/test_config_helpers.py)). The GitHub Actions workflow runs this suite before every build, so a regression fails CI instead of shipping.

These intentionally don't cover the tray icon, the settings editor GUI, or anything requiring a live OBS/Windows session — that's still best verified manually (run the app, watch the log, check the tray).

### 4. Run automatically at login

If you used the installer, you already chose this on the Install location page. To change it later: right-click the tray icon → **Edit Settings...** → General tab → check/uncheck **Launch automatically when Windows starts**, then **Save**. This adds/removes a shortcut in your Startup folder for you (only available when running the built `.exe`, not `python autostart_script.py`).

To do it by hand instead:

1. Press `Win+R`, enter `shell:startup`, hit Enter
2. Create a shortcut there pointing to `OBSAutoRecorder.exe`

Either way, it will launch silently (no console window) every time you log in.

## Using the tray icon

Right-click the tray icon (it may be tucked under the "show hidden icons" `^` chevron — drag it out if you want it always visible) for:

- Current status (watching / recording which game)
- **Overlay Monitor** — pick a monitor to pin a small floating color-status square to, or "Off" to disable it
- **Show Audio Mixer Levels** — toggle live mixer levels on the overlay; connects to OBS for this whenever it's running, independent of whether a game is currently being recorded (shows "No active audio sources" only if OBS itself isn't running yet). Turning this on automatically picks a monitor for **Overlay Monitor** too if it was still set to "Off" (your primary monitor, where available), so the levels actually become visible instead of updating on a hidden overlay — it never overrides a monitor you've already chosen, and doesn't turn the overlay back off when you disable levels again.
- **Open Recordings Folder** — opens OBS's current recording output folder (queried live from OBS; only available once OBS has connected)
- **Open Log File** — opens `log_file` in your default text editor
- **Edit Settings...** — opens a GUI settings window covering every `config.json` option (watched games/windows, launchers, OBS connection, cleanup/guards, post-processing, notifications) plus a **Custom Keybinds** tab, organized into tabs, with **Save** and **Save and Restart** buttons. No manual JSON editing needed. See [Optional: settings editor](#optional-settings-editor)
- **Save Replay Buffer** — only shown if `obs.replay_buffer.enabled` is `true`; saves the last few minutes of the replay buffer immediately
- **Split Recording File** — only shown if `obs.manual_split.enabled` is `true`; splits the current recording after `obs.manual_split.buffer_seconds` (see [Optional: split recording files](#optional-split-recording-files))
- **Kill OBS** / **Start OBS** — swaps between the two depending on whether OBS is currently running: force-kills it if it is (e.g. if it's hung; the watcher will relaunch it the next time a watched game starts), or launches it manually if it isn't (e.g. to use the audio mixer overlay or OBS itself without waiting for a game to be detected)
- **Restart App** — stops any active recording cleanly, then relaunches the whole app. Same as **Save and Restart** in the settings editor, without needing to open it — useful after hand-editing `config.json`, or just to recover from a stuck state
- **Quit** — stops any active recording cleanly, then exits

Icon colors:
- **Gray** — watching, idle
- **Red** — recording
- **Gold** (~5s) — a recording file just auto-split
- **Orange** — the watcher crashed, or it's restarting a hung/bloated OBS; check `autostart_script.log`

## OBS health recovery

OBS can sometimes stay running as a process while no longer working properly — frozen with its WebSocket server unresponsive, or ballooned in memory after a long session. Left alone, this would mean recordings silently never start. The watcher guards against both:

- **Hung OBS**: if a watched game launches and OBS's process is present but its WebSocket won't connect after retries, the watcher kills and relaunches OBS before trying again.
- **Bloated OBS**: while idle (no game running), if OBS's memory usage exceeds `obs.recovery.memory_limit_gb` (default `5`), the watcher preemptively kills and relaunches it.

Both cases flash the tray icon orange and log a warning. To avoid restart loops, either kind of restart is followed by an `obs.recovery.cooldown_seconds` cooldown (default `30`) before another is attempted.

## Optional: settings editor

Instead of hand-editing `config.json`, right-click the tray icon → **Edit Settings...** for a tabbed GUI covering every option in this README's config table: General (including "launch at Windows startup" — see [Run automatically at login](#4-run-automatically-at-login)), Watched Games (including the window-title rules), Launchers, OBS (path, output folder, recording format, WebSocket, auto-split, manual split, health recovery, game audio, multi-track audio, replay buffer), a **Custom Keybinds** tab (see [Optional: custom keybinds](#optional-custom-keybinds)), Cleanup & Guards, and Post-Processing/Notifications.

- **Save** writes `config.json` and closes the window, with a reminder that a restart may be necessary for some settings to take effect (config is only read at startup, so changes don't apply to the already-running watcher).
- **Save and Restart** writes `config.json` and immediately relaunches the whole app (stops any active recording first, same as a normal Quit) so every setting takes effect right away.
- Only one editor window can be open at a time; the menu item disables itself while one is open.
- List-like fields (exclude keywords, install folders, launch args) are entered comma-separated; `ffmpeg` arguments are space-separated.
- On the Watched Games tab, **Pick Running...** opens a filterable list of currently running process names to add from, instead of typing an exact exe name from memory — processes already in your watched list are shown greyed out with an "(already watching)" marker. The same picker is available per-row on the window-title rules.
- Also on the Watched Games tab, **Common Games...** opens a filterable list of popular games (League of Legends, Valorant, Wizard101, Warframe, Fortnite, Minecraft, etc.) to add with one click, without needing to have the game running first — useful for games this app's launcher auto-detection can't see (e.g. Riot's client) or that need a window-title rule (Minecraft: Java Edition). Not exhaustive; anything not listed can still be added via **Pick Running...** or by typing the exe name in by hand.
- On the OBS tab's Multi-Track Audio section, **Pick...** (per row) connects to OBS live with whatever WebSocket settings are currently in the form and lists its actual current inputs to choose from, instead of typing a name from memory — each one is shown with its kind (e.g. "Scarlet — Microphone/Aux", "Discord — Application Audio Capture") so you can tell multiple similarly-named devices apart, such as two microphones, when deciding which one to route.
- Also on the Multi-Track Audio section, **Quick Setup...** builds the whole track layout for you in one dialog — pick your desktop/mic devices from dropdowns and tick common apps (Discord, Spotify, Chrome, etc.), and it creates whatever OBS sources are missing and fills in the track list (see [Optional: multi-track audio](#optional-multi-track-audio)).

The editor always reloads `config.json` fresh when opened and only overwrites the fields shown in the form, so any advanced/unlisted key you've hand-added is left untouched.

## Optional: isolate game audio

If your recordings pick up Discord, Spotify, or other background app audio alongside the game, OBS's **Application Audio Capture** source can isolate just the game's audio — it captures a chosen process's audio output directly, regardless of what else is playing. This script can optionally point that source at whichever game it just detected, so you don't have to re-target it by hand every time you switch games. Just set `obs.game_audio_capture.enabled` to `true` in `config.json` (or **Edit Settings... → OBS tab → Game Audio Isolation**) — the app creates the `Game Audio` **Application Audio Capture** source in OBS for you automatically the first time it's needed, nothing to add by hand. Set `obs.game_audio_capture.input_name` if you'd rather it use a different name (e.g. because you already have a similarly-purposed source under a different one).

From then on, whenever a watched game starts recording, the script repoints that source at the game's process by executable name (not by window title), so it keeps working even if the game's window title changes mid-session or it has no visible window at all.

Mute or remove your desktop/system audio source in OBS if you don't want it recorded alongside the isolated game audio.

## Optional: multi-track audio

Records selected OBS inputs (mic, desktop audio, isolated game audio, Discord, etc.) each to their own separate audio track inside the recording, so you can mute/adjust/remove any one of them afterward in your editor instead of being stuck with a single pre-mixed track.

**It never touches your existing OBS profile.** The first time it runs, the watcher creates a separate, dedicated OBS profile (named `obs.multi_track_audio.profile_name`, default `"OBS Auto Recorder"`) cloned from whatever profile was active at that moment — same recording folder, same quality, same file format — and only ever makes further changes inside that dedicated profile. Every time a watched game starts recording, OBS is switched into it first; your own profile(s) are left exactly as you set them up, and switching profiles doesn't touch your scenes/sources (those live in your scene collection, which is separate from profiles in OBS).

**Quick setup (recommended):** in the settings editor's OBS tab → **Multi-Track Audio** → **Quick Setup...**, pick your desktop audio device and microphone from dropdowns (populated live from OBS), tick whichever common apps you use — Discord/Slack/Zoom, Spotify/Apple Music, Chrome/Firefox/Edge — and click **Apply**. It creates an Application Audio Capture source in OBS for any app you picked that doesn't already have one (reusing an existing one by name instead of duplicating it), fills in the track list below with the same layout below, and turns the feature on — no hand-typing input names or track numbers. Game audio is included automatically on track 3 if [game audio isolation](#optional-isolate-game-audio) is already enabled above; anything not in the common-apps list can still be added afterward with **+ Add Track Mapping**. While you're on that tab, also set **Recording format** to `mkv` — see below for why.

**Recommended track layout** (this is exactly what Quick Setup builds for you):

| Track | Contains |
|---|---|
| 1 | Desktop audio **+** microphone, combined |
| 2 | Microphone alone |
| 3 | Game audio (isolated) |
| 4 | Discord / other voice chat |
| 5 | Spotify / Apple Music |
| 6 | Browser |

The reasoning behind track 1: most media players (VLC included) default to playing a file's *first* audio track and never prompt you to pick one. Putting the full desktop+mic mix there means a quick double-click preview of a clip sounds complete and normal, exactly like a regular recording — while tracks 2-6 stay available as isolated stems for when you actually sit down to edit and want to mute/replace/rebalance one source (drop the music track, duck voice chat, re-level the mic) without re-recording anything.

**If an app's isolation randomly stops working** (e.g. Discord audio bleeds into other tracks again, or its track goes silent, after having worked before): this is an OBS quirk, not something you misconfigured. A "capture this app's audio, whatever window it has" source in OBS is stored internally as an exe-only match, but OBS can silently rewrite that into a specific window's exact title the moment its Properties dialog is opened in OBS itself (even without changing anything) — and that stored title then goes stale as soon as the app's window title changes next (Discord's includes the current server/channel name, so this can happen constantly). Every app added via **Quick Setup** is automatically re-pointed back to its exe-only match at the start of every recording, so this self-heals on its own within one recording start; you never need to fix it by hand in OBS. This self-heal only covers apps added through Quick Setup, since that's the only place the app's exe name is known — an app added manually via **+ Add Track Mapping** won't get it unless you also add a matching entry to `obs.multi_track_audio.app_captures` yourself (see below).

Manual setup:

1. In `config.json` (or the settings editor's OBS tab → **Multi-Track Audio**), set `obs.multi_track_audio.enabled` to `true`.
2. List which input feeds which track in `obs.multi_track_audio.tracks`, e.g.:

```json
"multi_track_audio": {
    "enabled": true,
    "profile_name": "OBS Auto Recorder",
    "tracks": [
        {"input_name": "Mic/Aux", "track": 1},
        {"input_name": "Desktop Audio", "track": 2},
        {"input_name": "Game Audio", "track": 3},
        {"input_name": "Discord", "track": 4}
    ],
    "app_captures": [
        {"input_name": "Discord", "process_name": "Discord.exe"}
    ]
}
```

Input names must match an existing OBS input exactly (case-sensitive) — use the settings editor's **Pick...** button per row to choose from OBS's actual current inputs instead of typing one from memory. An input that doesn't exist yet (e.g. you haven't set up [game audio isolation](#optional-isolate-game-audio)) is skipped with a warning in the log rather than blocking recording — add it whenever you're ready and it'll pick it up on the next recording start.

`app_captures` is optional and only needed for the self-heal described above on an Application Audio Capture input you set up by hand instead of through Quick Setup — `input_name` must match the OBS input exactly, `process_name` is the exe OBS should keep it pointed at (e.g. `Discord.exe`, `Spotify.exe`). Entries here that don't correspond to an existing input get created automatically, same as Quick Setup does.

The same input can appear more than once, on different tracks (e.g. a mic on both a "mic only" track and a combined "everything" track), and multiple inputs can share the same track number to get mixed together on it (e.g. two music sources both on track 5) — list as many rows per input as you need.

What this changes, all scoped to the dedicated profile only:

- **Output Mode** is switched to **Advanced** if it wasn't already — OBS only supports recording multiple audio tracks into one file in Advanced mode. If your original profile used Simple mode, the dedicated profile falls back to OBS's default Advanced-mode encoder settings; fine-tune quality/bitrate in OBS's Settings → Output while `"OBS Auto Recorder"` is the active profile if needed.
- The recording's enabled tracks are set to match whichever track numbers you've used.
- Each listed input's own track routing is set so it feeds *only* the track(s) you assigned it — nothing is left multiplexed onto every track by default. Any other Desktop Audio/microphone/Application Audio Capture input in the scene collection that *isn't* listed has its track routing cleared too, so removing a row here actually takes effect in OBS instead of leaving its old routing in place.

**Recording format — use MKV.** Your existing format/container choice is carried over as-is and never forced by this feature — it works with whichever one you use — but only some formats have reliably embedded *every* enabled track in every OBS version, and **MKV** is the one that always has. Set `obs.recording_format` to `mkv` (**Edit Settings... → OBS tab → Recording format**, or directly in `config.json`) and the app applies it for you; no need to dig through OBS's own Advanced Output settings. Skip this and only ever see one audio track in your finished recordings? This is almost certainly why — the log also warns about it the first time it detects a format outside the reliable set.

## Optional: split recording files

You can optionally enable OBS's file-splitting so a long session isn't stuck in one giant file. Once set up, a split can be triggered while in-game to split off a new file at any time — the tray icon (and overlay, if enabled) will flash green for a few seconds each time a split happens.

**About OBS's "Automatically split file" checkbox:** despite the name, this is OBS's master switch for split support generally — even a purely manual split (OBS's own hotkey, this app's tray item, or a custom keybind) fails with `OBSSDKRequestError: ... code 702` unless it's ticked in OBS's own Settings → Output (Advanced mode) → Recording. The tray item and custom keybinds below tick it for you automatically the moment they're used, so nothing to set up by hand for those. **OBS's own hotkey is the one exception**: this app has no way to intercept that path, so if you only ever use OBS's native Split Recording File hotkey (never the tray item or a custom keybind), you'll still need to tick this checkbox yourself once — the time/size fields next to it can stay at whatever OBS defaults to, they're irrelevant unless you also want [automatic splitting](#optional-split-recording-files) itself.

- **Manual, via OBS's own hotkey**: OBS Settings → Hotkeys → set a **Split Recording File** hotkey. Files from a manual split are renamed with a `Split N` tag (e.g. `Game - Split 1 - filename.mp4`), and if any manual split happened during the session, the final segment gets a `Split N` tag too.
- **Manual, via this app's tray menu**: set `obs.manual_split.enabled` to `true` (**Edit Settings... → OBS tab → Manual Split**) to add a **Split Recording File** tray item, so you don't need to bind or remember an OBS hotkey at all. `obs.manual_split.buffer_seconds` (default `0`) delays the actual split by that many seconds after clicking — the wait happens in this app, not in OBS, so it's independent of any hotkey. Files from this kind of split get the same `Split N` tag treatment as a hotkey-triggered manual split.
- **Manual, via a custom keybind**: add a **Split Recording File** entry in [Custom Keybinds](#optional-custom-keybinds) for a system-wide key combo that splits immediately (also honoring `obs.manual_split.buffer_seconds`), without opening the tray menu or touching OBS's Hotkeys settings at all.
- **Automatic**: OBS Settings → Output (Advanced mode) → Recording → enable **Automatically split file**, with a time or size limit.

OBS's WebSocket API doesn't report *why* a file split happened, so the script can't natively tell a manual split from an automatic one. If you use OBS's automatic splitting, set `obs.auto_split` in `config.json` to match it (interval/size and, optionally, its trigger-matching tolerance) so the script can recognize those splits and skip labeling them — any split that doesn't match your configured automatic settings is assumed to be manual. Leave `obs.auto_split.enabled` at `false` (the default) if you only use a manual split (hotkey, tray item, or custom keybind); every split will then be treated as manual.

## Optional: custom keybinds

**Edit Settings... → Custom Keybinds** lets you set your own system-wide key combos (e.g. `Ctrl+Alt+S`) that call an OBS action directly over the WebSocket the instant they're pressed — Split Recording File, Save/Start/Stop/Toggle Replay Buffer, Start/Stop/Toggle Recording, or Pause/Resume/Toggle Recording Pause. Each row has an on/off checkbox, an action dropdown, Ctrl/Alt/Shift/Win modifier checkboxes, and a key (0-9, A-Z, or F1-F12).

This is *not* OBS hotkey rebinding, and doesn't touch OBS's own Hotkeys settings at all: obs-websocket (the protocol this app talks to OBS with) has no API to read or change what physical key OBS itself has a hotkey bound to. Instead, this app registers its own keybinds directly with Windows and calls the matching WebSocket request when one fires — completely independent of, and in addition to, whatever's set in OBS's own **Settings → Hotkeys** dialog. That means:

- They work even if you've never opened OBS's Hotkeys page.
- They only fire while this app is running, and only actually do anything while OBS is running and connected (a keybind pressed with OBS closed just logs a warning).
- A key combo already claimed by another running app (or by OBS itself, or Windows) may fail to register — check `autostart_script.log` at startup for "Could not register custom keybind..." if one doesn't seem to work, and try a different combo.
- Changes here take effect on the next app restart (**Save and Restart**, or **Restart App** from the tray), same as other settings.
- **Split Recording File** automatically ticks OBS's own **Automatically split file** option in Settings → Output the moment it's used, if it isn't already on — see [Optional: split recording files](#optional-split-recording-files) for why OBS requires that even for a manual split.

`obs.custom_keybinds` in `config.json` is a list of `{"enabled": true, "action": "split_record_file", "modifiers": ["ctrl", "alt"], "key": "S"}` entries if you'd rather edit it by hand; valid `action` values are `split_record_file`, `save_replay_buffer`, `start_replay_buffer`, `stop_replay_buffer`, `toggle_replay_buffer`, `start_record`, `stop_record`, `toggle_record`, `pause_record`, `resume_record`, and `toggle_record_pause`.

## Optional: more launchers

Beyond the process/window list, Steam, and Epic, three more launchers can be auto-detected:

- **GOG Galaxy** — fully automatic, no setup needed. Reads installed games straight from the registry (`HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\GOG.com\Games`), the same place GOG Galaxy itself keeps track of them.
- **Xbox app / PC Game Pass** — automatic if you used the default install location. PC Game Pass installs each game as its own top-level folder (e.g. `C:\XboxGames\Halo Infinite\...`); set `xbox.install_dirs` if you chose a different install location.
- **Battle.net** — Blizzard doesn't expose a shared manifest or install root the way the others do, so this only works once you tell it where to look. Set `battlenet.install_dirs` to the parent folder(s) containing your Battle.net games (e.g. `["D:\\Games\\Battle.net"]`) and `battlenet.enabled` to `true`; each immediate subfolder is treated as a game, the same way Steam/Xbox detection works.

## Optional: game subfolders

By default, finished recordings stay in OBS's recording folder with the game name prefixed onto the filename (`Game - filename.mp4`). Set `organize_into_game_subfolders` to `true` in `config.json` to instead file each recording into a `<Game Name>\` subfolder (created automatically), keeping the original filename inside it. Split-part and no-audio tags are still applied to the filename either way.

## Optional: disk space guard

If a drive fills up mid-recording, OBS's output can end up corrupted or truncated. Setting `disk_space_guard.enabled` to `true` makes the watcher check free disk space before starting a recording, and skip starting it (flashing the tray icon orange and logging/notifying instead) if free space is under `disk_space_guard.minimum_free_gb`. By default it checks the drive OBS is installed on; set `disk_space_guard.path` explicitly if your recordings actually go to a different drive.

This is a preventative check only — it doesn't stop or clean up an already-started recording if the disk fills up during it.

## Optional: recording cleanup

Two independent, opt-in cleanup features:

- **`cleanup.delete_short_clips`** — if the whole recording session (from start to the game closing) was shorter than `minimum_seconds`, the file(s) are deleted instead of renamed/kept. Useful for filtering out accidental or immediately-closed game launches.
- **`cleanup.flag_silent_recordings`** — while recording, the watcher watches OBS's live audio meters (the same feed used by the tray's audio-mixer overlay). If no input's peak level ever crosses `peak_threshold` for `warn_after_seconds`, it logs a warning and sends a notification (if enabled) *while the recording is still running*, so you can notice and fix a muted source, wrong audio capture target, etc. before wasting the whole session. The finished file is also renamed with a `[NO AUDIO]` prefix so silent recordings are easy to spot afterward, even if you missed the live warning.

Both apply per finished segment, so a session that uses manual/automatic splits is evaluated per-file where relevant (silence tracking resets at each split; short-clip length is evaluated for the whole session).

## Optional: replay buffer

Setting `obs.replay_buffer.enabled` to `true` starts OBS's replay buffer whenever recording starts, and stops it when the watched game exits. A **Save Replay Buffer** tray menu item appears whenever this is enabled, saving the buffer on demand (equivalent to OBS's own "Save Replay Buffer" hotkey). Configure the buffer's length and save location in OBS itself (Settings → Output → Replay Buffer).

## Optional: post-record transcode

Setting `post_record_transcode.enabled` to `true` runs each finished recording through `ffmpeg` in the background right after it's renamed. `post_record_transcode.args` are passed to `ffmpeg` between the input and output file — customize these for whatever codec/quality/size tradeoff you want (the default re-encodes to H.264/AAC at a moderate quality, mainly to shrink OBS's typically larger native output, and includes `-map 0` so every audio track survives the re-encode rather than just the first one). The output filename gets `post_record_transcode.suffix` appended; set `post_record_transcode.delete_original` to `true` to remove the original once the transcode succeeds. By default the output keeps the original file's extension — set `post_record_transcode.output_extension` (e.g. `.mp4`) to convert it to a different container.

**No ffmpeg installed yet?** **Edit Settings... → Post-Processing** shows an **Install ffmpeg via winget** button and a manual download link instead of the usual options, since there's nothing useful to configure until ffmpeg actually exists somewhere on the PC. Installing it (either way) switches the panel over to the full options automatically, no need to reopen Settings. Any value already saved in `config.json` from before ffmpeg went missing (or before it's installed yet) is preserved either way, even while its controls are hidden.

**Finding ffmpeg:** this feature needs `ffmpeg` actually installed somewhere on the PC — most users don't have it by default, which is why the [installer](#quick-install-recommended) already tries to install it for you automatically via `winget`. If you skipped that or it couldn't find `winget`, leave `post_record_transcode.ffmpeg_path` as `"ffmpeg"` (the default) and the app will look for it on your `PATH` automatically; if that fails, **Edit Settings... → Post-Processing → Auto-detect ffmpeg** searches the install locations of common Windows package managers (winget, Chocolatey, Scoop) and a few common manual-install folders, and fills in the exact path if it finds one. If nothing's found anywhere, install ffmpeg first (e.g. `winget install ffmpeg`, or download from ffmpeg.org), then auto-detect again, or use **Browse...** to point the field at `ffmpeg.exe` by hand. The same auto-detect fallback also runs automatically at transcode time if the configured path stops resolving (e.g. ffmpeg got reinstalled elsewhere).

**If a recording seems to go missing:** the original recording is *never* deleted unless the transcode actually succeeds *and* `delete_original` is `true` — a missing/not-found ffmpeg, a bad argument, or any other transcode failure always leaves the original file exactly where OBS put it, just without a transcoded copy alongside it. Check `autostart_script.log` for the exact reason (it logs the full `ffmpeg` command and, on failure, ffmpeg's own error output) — a toast notification also fires on failure if `notifications.enabled` is `true`.

This runs in a background thread per file and doesn't block the watcher, but a long transcode queue (e.g. many short segments from frequent splits) will pile up if `ffmpeg` can't keep up with how fast files finish.

**MKV → MP4 preset (preserve every audio track):** if you record to MKV for [multi-track audio](#optional-multi-track-audio) but want a final MP4 for sharing/uploading, **Edit Settings... → Post-Processing → Use MKV → MP4 preset** fills in `args: ["-map", "0", "-c", "copy"]` and `output_extension: ".mp4"` for you. This is a pure remux — every audio track is carried over byte-for-byte with no re-encoding and no quality loss, just repackaged into an MP4 container. It's the most reliable way to convert between containers without silently losing tracks, since `ffmpeg` otherwise defaults to keeping only one "best" audio stream.

## Optional: notifications

Setting `notifications.enabled` to `true` shows a Windows toast notification (via the tray icon, so no extra permissions or setup needed) for: recording started/stopped, OBS restarted (hung or memory-bloated), OBS unreachable, low disk space, a short clip deleted, and no-audio detected. These mirror the corresponding log messages, so nothing shown as a notification is exclusive to it — check the log if you missed one.

## Known limitations

- Windows has one system tray total — it isn't per-monitor. Use the floating overlay if you need status visible on a specific monitor.
- Steam-game auto-detection matches by folder location, not a games database, so a handful of non-game Steam apps may need adding to `steam.exclude_keywords` if they cause false positives (Wallpaper Engine is excluded by default). The same applies to `xbox.exclude_keywords` and `battlenet.exclude_keywords`.
- Epic-game auto-detection reads the Epic Games Launcher's local install manifests (`%PROGRAMDATA%\Epic\EpicGamesLauncher\Data\Manifests`), so a game only shows up once it's been installed at least once through the launcher.
- GOG Galaxy auto-detection reads from the registry, so a game only shows up once it's been installed at least once through GOG Galaxy.
- Battle.net auto-detection requires manually configuring `battlenet.install_dirs` — there's no manifest or shared install root to read automatically.
- The silent-recording check only looks at OBS's own input audio meters; if a source is capturing audio but OBS itself reports zero level (e.g. a genuinely misconfigured capture), it'll correctly flag as silent, but it can't detect audio that's present but wrong (e.g. a completely different application's audio).
- The installer's automatic OBS WebSocket configuration only works if OBS has been run at least once already (so its settings folder exists) and isn't currently running at install time; otherwise the installer falls back to showing you the password to paste in manually. It also only writes to OBS's *default* settings profile.
- Multi-track audio's recording-folder handoff (`obs.multi_track_audio`) and `obs.output_folder` both need obs-websocket 5.3+ (bundled with OBS 29+); on older OBS versions the dedicated multi-track profile is still created but its recording folder isn't cloned from your original profile, and `obs.output_folder` silently has no effect (check the log). Track routing and Advanced-mode/track-count setup work on any OBS 28+ install.
- The installer's **Easy Install** button for OBS and its automatic `ffmpeg` install both need `winget` (present by default on current Windows 10/11, via the App Installer). Without it, both fall back to their manual alternatives (a download link, or installing `ffmpeg` yourself later) rather than failing setup.
- [Custom keybinds](#optional-custom-keybinds) are system-wide, so a combo already claimed by another running app, or reserved by OBS/Windows itself, may fail to register — try a different combo if one doesn't seem to fire.
