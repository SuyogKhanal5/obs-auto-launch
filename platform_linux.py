"""Linux backend for platform_common.py -- see CROSS_PLATFORM_PLAN.md for the full migration
schedule. Empty (aside from this docstring) until Phase 1 starts adding real functions.

Global hotkeys, window-title detection, and the clip editor's space-bar-while-video-focused
behavior are X11-only for v1 (see CROSS_PLATFORM_PLAN.md §2.3) -- Wayland sessions
(`os.environ.get("XDG_SESSION_TYPE") == "wayland"`) get a clear "not supported" result from the
relevant functions once they land, not a silent no-op or a crash."""
