**VERIFIED STABLE** -- every change below was confirmed against a real running OBS instance and/or a real recording during development, not just unit tests.

### Clip editor
- Trim now defaults to precise (re-encode) mode instead of fast/stream-copy. Fast mode moves into Settings for anyone who deliberately wants faster exports; precise avoids a broken leading frame that fast mode inherits from OBS's own encoding at arbitrary cut points, and both modes now flag output for reliable Discord playback.
- Fixed a real bug where trimming a multi-track recording re-encoded every audio track instead of just the one(s) needed, bloating file size.
- New in-editor preview quality option (hardware/balanced/software decode) to work around preview stutter on some systems.
- New "limit output to N MB" option (with a configurable Settings default) for fitting a clip under an upload limit -- implemented as a real two-pass encode so the target size is actually hit.
- Clicking the timeline now always seeks and starts playback there.

### Audio sync
- Root-caused and fixed isolated audio tracks (Discord, Spotify, Game Audio, browser capture) sounding a few milliseconds out of sync with Desktop Audio, heard as doubled/echoed sound in multi-track-aware players (Premiere, Discord mobile). This is a genuine Windows/OBS latency difference between per-process audio capture and device capture; the app now applies a compensating offset automatically.
- Added a "Calibrate Audio Sync" option (on-demand, and automatic the first time Game Audio Isolation is turned on) that measures the real offset on your own machine instead of relying on a fixed guess.
- Corrected the default offset value after re-measurement showed the original was overcorrecting by roughly 2x.

### Reliability
- Fixed a real race where a custom keybind could silently stop working for the rest of a session if its hotkey registration lost a timing race right after the app restarted itself.

### Known issue
- The clip editor's space bar play/pause shortcut is disabled in this release. Use the on-screen play/pause button instead.
