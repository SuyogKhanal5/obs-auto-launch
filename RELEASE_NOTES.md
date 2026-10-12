### Cross-platform support
- OBS Auto Recorder now runs on macOS and Linux, not just Windows -- the tray icon, global hotkeys, the clip editor, autostart, and OBS/ffmpeg/VLC discovery all go through a shared platform abstraction layer instead of Windows-only code paths. Windows keeps its existing zip + installer exe; macOS ships as a native-arch .app/.dmg (Apple Silicon and Intel); Linux ships as a tar.gz, an AppImage, and an installer binary.

### Mic Boost
- Replaced the old noise gate with real voice isolation (RNNoise or Speex, your choice) -- an adaptive suppressor instead of a simple hard on/off threshold.
- Added an optional 3-band EQ (low/mid/high) to shape the boosted mic's tone.
- Added "Auto-Detect..." for the boost amount, and a live post-boost level preview in the Audio Mixer Levels overlay.
- Fixed several lifecycle bugs: mic boost now reliably (re)applies whenever OBS becomes reachable rather than only at recording start, and Auto-Detect no longer fails right after OBS first launches.

### Custom Keybinds
- Added Raw Input as the new default hotkey delivery mechanism -- catches your keybinds over fullscreen-exclusive games with none of the input-lag risk a global low-level hook carries. RegisterHotKey and the low-level hook are both still available as alternatives in Settings.

### Clip Editor
- Added a waveform view, then substantially reworked it: it renders once per file/track and all zoom/pan is an instant client-side crop, with a real higher-resolution re-render only firing once you settle on a deep zoom. The crop itself now preserves genuine peak-to-peak envelope data instead of a plain image resize, which was quietly capable of misrepresenting real peaks once zoomed out on a longer recording.
- The rendered waveform is now cached in memory per file/track, so reopening a clip you were just editing is instant instead of re-decoding the whole file again.
- Fixed the waveform silently panning back out during playback if you'd deliberately zoomed into a specific region.
- Added Next/Previous buttons next to Recent recordings to step through that list one file at a time.
- The "Open a recording" dialog now defaults to your actual OBS recordings folder instead of wherever the file picker last happened to land.
- Size-limited exports now default their target to 20MB -- the checkbox itself still starts unchecked, since limiting size silently drops every audio track but the first.
- Added per-destination gain in Track Routing (boost or attenuate one source into one output track), with configurable Settings defaults.
- Cancel now actually stops an in-progress export instead of leaving it running in the background.

### Fixes
- Fixed a Settings crash, a silent config-write failure on Program Files installs, a custom-keybind restart race, and mic gain being silently canceled out by ffmpeg's own auto-normalize during multi-track mixing.

Covered by an automated test suite (752 tests) plus targeted live smoke tests against real recordings for the clip editor changes above; not yet run through a full manual end-to-end session on every platform.
