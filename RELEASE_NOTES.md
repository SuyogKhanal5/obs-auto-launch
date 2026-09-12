## VERIFIED STABLE

### Clip editor
- Fixed vertical spacing between the timeline controls and the Start/End row.
- Output format, resolution, Trim Clip, and Close now share one row instead of being split across separate ones.
- Zoom buttons are now uniform squares; the rewind/play-pause/forward buttons are now uniform rectangles.
- The timeline now shows its empty track immediately when the editor opens, instead of staying blank until a file is loaded.
- The file currently loaded in the editor is now locked against deletion (e.g. by storage management's cleanup, or a manual delete elsewhere) for as long as it's open; the lock releases automatically on switching files or closing the editor.

### Settings
- Clicking a Settings tab now expands its label to full text and abbreviates the others, so all eight tabs fit without truncation.
- Fixed "Edit Clips..." / "Edit Settings..." staying greyed out in the tray menu after closing either window.

### Replay buffer
- Replay buffer mode and length are now configurable directly from Settings → OBS, instead of requiring a manual change in OBS's own UI.
- New "Replay buffer only" mode: skip the full continuous recording entirely and only maintain/save the rolling buffer.
- Root-caused and fixed a real bug where the replay buffer silently failed to activate: OBS only builds that output when a profile loads, so a live settings change alone was never picked up. The app now automatically restarts OBS when a replay-buffer setting actually changes, so it takes effect immediately.
- A saved replay-buffer clip is now renamed the same way a manual split is (game name + "Replay" marker) and triggers the same green tray/overlay flash as a split.

### Common Games picker / installer
- Pruned the "Common Games" quick-add list to drop titles this app's own Steam/Epic/Xbox/Battle.net auto-detection already covers (Counter-Strike 2, Overwatch 2, Fortnite, Rocket League, Minecraft: Bedrock Edition), keeping only titles with a launch path this app can't auto-discover.
- The installer now has a "Games to watch" page, letting you pre-select from that same list during setup.
