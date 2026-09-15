### Installer
- The WebSocket password on the finish page is now in a selectable/copyable field with a Copy button, instead of being buried in plain paragraph text you'd have to retype by hand.
- Added a progress bar to the install, update, and uninstall flows -- previously the update/uninstall flows gave no visual feedback at all beyond the button disabling, and progress messages during them were silently discarded.
- The Ready-to-install page now warns upfront if OBS is currently running (WebSocket auto-config can't happen while it's open), instead of only surfacing that after install finishes.

These installer changes are covered by scripted Tk smoke tests (real button clicks driving the actual wizard code, not just mocked unit tests of isolated logic) but have not yet been run through a real end-user install.
