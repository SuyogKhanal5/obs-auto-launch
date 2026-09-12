### Markers
- New "Add Marker" custom keybind action: drops an OBS chapter marker into the current recording.
- Markers show up on the clip editor's timeline as blue flags. Clicking one sets Start to 30s before it and 5s after, still freely adjustable afterward.
- Markers only work on Hybrid MP4 recordings (an OBS limitation). Picking the Add Marker keybind now automatically switches the recording format to Hybrid MP4 and restarts OBS once so it takes effect; if a save still can't use markers, the app now shows a toast explaining why.

### Overlay
- New "Default monitor" and "Start with audio mixer levels shown" options in Settings → General, so the overlay doesn't need to be turned on by hand from the tray every session.
- Hardened the overlay's redraw loop against a bad tick permanently freezing the display with no log trace -- it now logs and keeps retrying instead.

### Reliability
- Root-caused and fixed a real bug where a recording could be silently orphaned (stuck open, never renamed) if the app was ever killed, crashed, or force-restarted mid-recording. The app now recognizes a recording it started that's still running from an unclean shutdown and automatically stops and finalizes it on the next launch, without ever touching a recording started manually in OBS.

### CI
- Removed the automatic build-and-release trigger on every push to main/tags -- builds are now triggered manually (workflow_dispatch) instead.
