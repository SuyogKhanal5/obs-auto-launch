**VERIFIED STABLE** -- every change below was confirmed against a real running OBS instance and/or a real recording during development, not just unit tests.

### Clip editor
- Space bar play/pause now works, including while the embedded video preview itself has keyboard focus -- previously disabled after repeated attempts at reclaiming Tk-level focus couldn't reliably work around that. Implemented as a foreground-window-gated global hotkey instead, verified live against a real OS-level hook and synthesized key events, not just mocked tests.

### Reliability
- Fixed a real bug (introduced, then caught, during this same round of work) where `ttk.Combobox` -- itself a subclass of `ttk.Entry` -- was still treated as a text field even after being explicitly excluded from a focus check, since the exclusion only removed it from an `isinstance` tuple that `ttk.Entry` alone already matched via inheritance.
