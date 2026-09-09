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
- Optional post-record transcode: runs a finished recording through `ffmpeg` in the background (e.g. to compress it) once it's done (see [Optional: post-record transcode](#optional-post-record-transcode))
- Optional Windows toast notifications for key events (recording started/stopped, OBS restarted, low disk space, no audio detected) via the tray icon (see [Optional: notifications](#optional-notifications))
- Clears OBS's "unclean shutdown" crash-recovery sentinel before launching, so a prior forced close (e.g. Task Manager, crash, power loss) doesn't pop OBS's crash dialog and stall automation
- Detects a hung OBS (process running but its WebSocket stops responding) or a memory-bloated idle OBS, and automatically kills and relaunches it instead of silently failing to record
- Optionally repoints an OBS Application Audio Capture source at each detected game, to isolate its audio from Discord/Spotify/etc. (see [Optional: isolate game audio](#optional-isolate-game-audio))
- Optional multi-track audio: routes mic/desktop/game audio (or any inputs you pick) to separate recording tracks for editing later, entirely inside its own dedicated OBS profile so your existing profile is never touched (see [Optional: multi-track audio](#optional-multi-track-audio))
- System tray icon showing live status (gray = watching, red = recording, gold = recording file just split, orange = internal error or OBS recovery in progress — check the log), plus shortcuts to open the recordings folder and log file
- Built-in GUI settings editor (tray icon → **Edit Settings...**) for every `config.json` option — no manual JSON editing required (see [Optional: settings editor](#optional-settings-editor))
- Optional floating always-on-top overlay, pinned to any monitor of your choice via the tray icon's right-click menu, showing the same status color
- Packaged as a single standalone `OBSAutoRecorder.exe` so it's easy to identify and kill in Task Manager (not a generic `python.exe`/`pythonw.exe` process)
- A plain-language installer (`OBSAutoRecorderInstaller.exe`) for non-technical users: picks an install folder, finds (or lets you browse to) OBS, offers a few simple preferences, optionally creates a desktop icon and/or sets up auto-start, and even tries to configure OBS's WebSocket server for you automatically — see [Quick install](#quick-install-recommended)

## Requirements

- Windows 10/11
- [OBS Studio](https://obsproject.com/) with the built-in WebSocket server (OBS 28+) — doesn't need to be installed before you run the installer, just before you actually want to record
- Python 3.10+ (only needed if you want to run from source or build the exes yourself — not needed to use the [prebuilt releases](../../releases/latest))

## Quick install (recommended)

For most people, this is the whole setup:

1. Download **`OBSAutoRecorderInstaller.exe`** from the [Releases page](../../releases/latest) and run it.
2. Click through the wizard — pick where to install (defaults to Program Files), it'll try to find OBS Studio automatically, and offers a few plain-language yes/no preferences (all changeable later).
3. Click Install, then Finish.

That's it — no editing files, no copying passwords by hand. The installer even tries to configure OBS's WebSocket server for you automatically (if OBS has been run at least once already); if it can't, it'll tell you the one manual step left and the password to use.

Running the installer again later lets you update or uninstall — it detects an existing install and asks which you want, and an update never touches your existing settings.

Everything below is for people who want more control: building from source, installing by hand, or understanding every `config.json` option (the same options are also available from the app's own **Edit Settings...** tray menu once it's running).

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
| `post_record_transcode.ffmpeg_path` | Path to `ffmpeg`, or just `"ffmpeg"` if it's on your `PATH` |
| `post_record_transcode.args` | Extra `ffmpeg` arguments between the input and output file, e.g. codec/quality settings |
| `post_record_transcode.suffix` | Appended to the filename (before the extension) for the transcoded output, so it doesn't collide with the original |
| `post_record_transcode.delete_original` | Delete the original recording once the transcode succeeds |
| `obs.path` | Full path to `obs64.exe` |
| `obs.launch_args` | Extra command-line args OBS is launched with |
| `obs.startup_wait_seconds` | How long to wait after launching OBS before trying to connect |
| `obs.output_folder` | Recording output folder to enforce in OBS (created automatically if missing). Leave unset/blank to leave OBS's own recording folder setting alone |
| `obs.websocket.host` / `port` / `password` | Must match OBS's WebSocket Server Settings |
| `obs.auto_split.enabled` | Set to `true` if you've enabled OBS's **Automatically split file** option (see below), so its splits aren't mislabeled as manual |
| `obs.auto_split.by` | `"time"` or `"size"` — must match what you set in OBS's automatic split setting |
| `obs.auto_split.minutes` / `tolerance_seconds` | When `by` is `"time"`: the split interval you set in OBS, and how many seconds of slack to allow when matching a split against it |
| `obs.auto_split.megabytes` / `tolerance_megabytes` | When `by` is `"size"`: the split size you set in OBS, and how much overshoot to allow when matching a split against it |
| `obs.recovery.memory_limit_gb` | Restart OBS if its memory usage exceeds this while idle (see [OBS health recovery](#obs-health-recovery)). Defaults to `5` if omitted |
| `obs.recovery.cooldown_seconds` | Minimum time between automatic OBS restarts, whether triggered by a hang or by memory. Defaults to `30` if omitted |
| `obs.game_audio_capture.enabled` | Repoint an existing **Application Audio Capture** source at the detected game each time recording starts, to isolate its audio (see [Optional: isolate game audio](#optional-isolate-game-audio)) |
| `obs.game_audio_capture.input_name` | Name of that source in your OBS scene, exactly as it appears in OBS |
| `obs.multi_track_audio.enabled` | Route configured inputs to separate recording tracks, inside a dedicated OBS profile (see [Optional: multi-track audio](#optional-multi-track-audio)) |
| `obs.multi_track_audio.profile_name` | Name of the dedicated OBS profile to create/use for this — created automatically the first time, cloned from whatever profile is active then. Defaults to `"OBS Auto Recorder"` if omitted |
| `obs.multi_track_audio.tracks` | List of `{"input_name": "...", "track": N}` entries (`N` is `1`-`6`) — which OBS input feeds which recording track. Names must match an existing OBS input exactly |
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

**Option A — use the prebuilt exe directly** (no installer, e.g. for a portable/USB setup): download `OBSAutoRecorder.exe` from the [Releases page](../../releases/latest), put it in its own folder alongside a `config.json` (see step 2), and run it. No Python needed.

**Option B — build it yourself**:

```
pip install -r requirements.txt
pyinstaller --onefile --noconsole --name OBSAutoRecorder --distpath . --workpath build --specpath build autostart_script.py
```

This produces `OBSAutoRecorder.exe` in the project folder, alongside `config.json` (it reads config from its own directory). Rebuild any time you change `autostart_script.py`.

To also build the installer (`OBSAutoRecorderInstaller.exe`), build the app exe first as above, then:

```
pyinstaller --onefile --noconsole --uac-admin --name OBSAutoRecorderInstaller --distpath . --workpath build --specpath build --add-data "<full path to OBSAutoRecorder.exe>;." --add-data "<full path to config.example.json>;." installer.py
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
- **Show Audio Mixer Levels While Recording** — toggle live mixer levels on the overlay
- **Open Recordings Folder** — opens OBS's current recording output folder (queried live from OBS; only available once OBS has connected)
- **Open Log File** — opens `log_file` in your default text editor
- **Edit Settings...** — opens a GUI settings window covering every `config.json` option (watched games/windows, launchers, OBS connection, cleanup/guards, post-processing, notifications) organized into tabs, with **Save** and **Save and Restart** buttons. No manual JSON editing needed. See [Optional: settings editor](#optional-settings-editor)
- **Save Replay Buffer** — only shown if `obs.replay_buffer.enabled` is `true`; saves the last few minutes of the replay buffer immediately
- **Kill OBS** — force-kills any running `obs.process_name` process (e.g. if it's hung); the watcher will relaunch it the next time a watched game starts
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

Instead of hand-editing `config.json`, right-click the tray icon → **Edit Settings...** for a tabbed GUI covering every option in this README's config table: General (including "launch at Windows startup" — see [Run automatically at login](#4-run-automatically-at-login)), Watched Games (including the window-title rules), Launchers, OBS (path, output folder, WebSocket, auto-split, health recovery, game audio, multi-track audio, replay buffer), Cleanup & Guards, and Post-Processing/Notifications.

- **Save** writes `config.json` and closes the window, with a reminder that a restart may be necessary for some settings to take effect (config is only read at startup, so changes don't apply to the already-running watcher).
- **Save and Restart** writes `config.json` and immediately relaunches the whole app (stops any active recording first, same as a normal Quit) so every setting takes effect right away.
- Only one editor window can be open at a time; the menu item disables itself while one is open.
- List-like fields (exclude keywords, install folders, launch args) are entered comma-separated; `ffmpeg` arguments are space-separated.
- On the Watched Games tab, **Pick Running...** opens a filterable list of currently running process names to add from, instead of typing an exact exe name from memory — processes already in your watched list are shown greyed out with an "(already watching)" marker. The same picker is available per-row on the window-title rules.
- Also on the Watched Games tab, **Common Games...** opens a filterable list of popular games (League of Legends, Valorant, Wizard101, Warframe, Fortnite, Minecraft, etc.) to add with one click, without needing to have the game running first — useful for games this app's launcher auto-detection can't see (e.g. Riot's client) or that need a window-title rule (Minecraft: Java Edition). Not exhaustive; anything not listed can still be added via **Pick Running...** or by typing the exe name in by hand.
- On the OBS tab's Multi-Track Audio section, **Pick...** (per row) connects to OBS live with whatever WebSocket settings are currently in the form and lists its actual current inputs to choose from, instead of typing a name from memory.

The editor always reloads `config.json` fresh when opened and only overwrites the fields shown in the form, so any advanced/unlisted key you've hand-added is left untouched.

## Optional: isolate game audio

If your recordings pick up Discord, Spotify, or other background app audio alongside the game, OBS's **Application Audio Capture** source can isolate just the game's audio — it captures a chosen process's audio output directly, regardless of what else is playing. This script can optionally point that source at whichever game it just detected, so you don't have to re-target it by hand every time you switch games:

1. In OBS, add an **Application Audio Capture** source to your scene (any window it's currently pointed at doesn't matter — it'll be overwritten automatically), and give it a name, e.g. `Game Audio`.
2. In `config.json`, set `obs.game_audio_capture.enabled` to `true` and `obs.game_audio_capture.input_name` to that exact name.

From then on, whenever a watched game starts recording, the script repoints that source at the game's process by executable name (not by window title), so it keeps working even if the game's window title changes mid-session or it has no visible window at all.

Mute or remove your desktop/system audio source in OBS if you don't want it recorded alongside the isolated game audio.

## Optional: multi-track audio

Records selected OBS inputs (mic, desktop audio, isolated game audio, Discord, etc.) each to their own separate audio track inside the recording, so you can mute/adjust/remove any one of them afterward in your editor instead of being stuck with a single pre-mixed track.

**It never touches your existing OBS profile.** The first time it runs, the watcher creates a separate, dedicated OBS profile (named `obs.multi_track_audio.profile_name`, default `"OBS Auto Recorder"`) cloned from whatever profile was active at that moment — same recording folder, same quality, same file format — and only ever makes further changes inside that dedicated profile. Every time a watched game starts recording, OBS is switched into it first; your own profile(s) are left exactly as you set them up, and switching profiles doesn't touch your scenes/sources (those live in your scene collection, which is separate from profiles in OBS).

Setup:

1. In `config.json` (or the settings editor's OBS tab → **Multi-Track Audio**), set `obs.multi_track_audio.enabled` to `true`.
2. List which input feeds which track in `obs.multi_track_audio.tracks`, e.g.:

```json
"multi_track_audio": {
    "enabled": true,
    "profile_name": "OBS Auto Recorder",
    "tracks": [
        {"input_name": "Mic/Aux", "track": 1},
        {"input_name": "Desktop Audio", "track": 2},
        {"input_name": "Game Audio", "track": 3}
    ]
}
```

Input names must match an existing OBS input exactly (case-sensitive) — use the settings editor's **Pick...** button per row to choose from OBS's actual current inputs instead of typing one from memory. An input that doesn't exist yet (e.g. you haven't set up [game audio isolation](#optional-isolate-game-audio)) is skipped with a warning in the log rather than blocking recording — add it whenever you're ready and it'll pick it up on the next recording start.

What this changes, all scoped to the dedicated profile only:

- **Output Mode** is switched to **Advanced** if it wasn't already — OBS only supports recording multiple audio tracks into one file in Advanced mode. If your original profile used Simple mode, the dedicated profile falls back to OBS's default Advanced-mode encoder settings; fine-tune quality/bitrate in OBS's Settings → Output while `"OBS Auto Recorder"` is the active profile if needed.
- The recording's enabled tracks are set to match whichever track numbers you've used.
- Each listed input's own track routing is set so it feeds *only* the track(s) you assigned it — nothing is left multiplexed onto every track by default.

**Recording format**: your existing format/container choice is carried over as-is and never forced — this feature works with whichever one you use. That said, only some formats have reliably embedded every enabled track in every OBS version; **MKV** is the one that always has. If you use a different format (MP4, MOV, etc.) and only see one audio track in the finished file, switch this profile's Recording Format to MKV in OBS's Settings → Output. The log also warns about this the first time it detects a format outside the reliable set.

## Optional: split recording files

You can optionally enable OBS's file-splitting so a long session isn't stuck in one giant file. Once set up, your existing **Split Recording File** hotkey can be used while in-game to split off a new file at any time — the tray icon (and overlay, if enabled) will flash gold for a few seconds each time a split happens.

- **Manual**: OBS Settings → Hotkeys → set a **Split Recording File** hotkey. Files from a manual split are renamed with a `Split N` tag (e.g. `Game - Split 1 - filename.mp4`), and if any manual split happened during the session, the final segment gets a `Split N` tag too.
- **Automatic**: OBS Settings → Output (Advanced mode) → Recording → enable **Automatically split file**, with a time or size limit.

OBS's WebSocket API doesn't report *why* a file split happened, so the script can't natively tell a manual split from an automatic one. If you use OBS's automatic splitting, set `obs.auto_split` in `config.json` to match it (interval/size and, optionally, its trigger-matching tolerance) so the script can recognize those splits and skip labeling them — any split that doesn't match your configured automatic settings is assumed to be manual. Leave `obs.auto_split.enabled` at `false` (the default) if you only use the manual hotkey; every split will then be treated as manual.

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

Setting `post_record_transcode.enabled` to `true` runs each finished recording through `ffmpeg` in the background right after it's renamed (requires `ffmpeg` installed and either on your `PATH` or pointed to via `post_record_transcode.ffmpeg_path`). `post_record_transcode.args` are passed to `ffmpeg` between the input and output file — customize these for whatever codec/quality/size tradeoff you want (the default re-encodes to H.264/AAC at a moderate quality, mainly to shrink OBS's typically larger native output). The output filename gets `post_record_transcode.suffix` appended; set `post_record_transcode.delete_original` to `true` to remove the original once the transcode succeeds.

This runs in a background thread per file and doesn't block the watcher, but a long transcode queue (e.g. many short segments from frequent splits) will pile up if `ffmpeg` can't keep up with how fast files finish.

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
