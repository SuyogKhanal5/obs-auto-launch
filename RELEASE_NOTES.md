**VERIFIED STABLE** -- every change below was confirmed against a real running OBS instance and/or a real recording during development, not just unit tests.

### Clip editor
- New "Fix audio track sync" checkbox measures each isolated audio track's real lag directly from the clip you're trimming (via cross-correlation against Desktop Audio) and corrects each one independently, instead of always applying one fixed guess -- real tracks (e.g. Discord vs. a locally-rendered game) were confirmed to need genuinely different corrections within the very same recording.
- New "Track Routing" dialog, mirroring OBS's own isolation routing settings: consolidate multiple source tracks into fewer output tracks, drop a track entirely, or mute one -- all from checkboxes, without leaving the editor. Mute settings have moved into this dialog.
- The status bar now shows real encode progress (a live percentage, driven by ffmpeg's own progress output) instead of a static "Trimming..." message for the whole export.
- The preview now stays paused after a trim finishes instead of resuming playback.

### Audio sync
- Root-caused and fixed the clip editor's sync-fix measurement silently coming back empty inside the packaged app while working correctly in every standalone test: a numpy internal module PyInstaller wasn't bundling was crashing the measurement on a background thread with nowhere for the error to go, disguised as "no confident measurement" rather than a visible failure.
- Fixed a double-correction bug where a clip already corrected at record time could get the same offset applied a second time by the editor.
- New opt-in `audio_sync_shift` config option to apply a fixed manual shift to specific tracks on every finished recording automatically, for anyone who'd rather set a static correction than use the clip editor's per-clip measurement.

### Reliability
- A crash inside a background thread (e.g. the audio-sync measurement) could previously vanish with no trace at all in this app's console-less build -- now always logged with a full traceback.
- The clip editor fully releases its hold on the source file before handing it to ffmpeg for measuring or trimming, rather than just pausing playback.

### Known issue
- The clip editor's space bar play/pause shortcut is disabled in this release. Use the on-screen play/pause button instead.
