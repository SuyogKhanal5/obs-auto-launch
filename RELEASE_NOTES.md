### Clip Editor
- Fixed the app freezing (and getting killed by Windows as unresponsive) when opening a new video while one was already loaded. VLC's own stop/open work could block the UI thread for several seconds on a long or complex recording; it now runs off the main thread, verified live that the window stays responsive loading two large recordings back-to-back.
- Raised the Recent recordings cap from 30 to 500 -- Previous was dead-ending into "No older recordings" on any folder with more than 30 files, even though older ones were still right there on disk.

Covered by the automated test suite (756 tests); the freeze fix was additionally verified live with a responsiveness check against real, large recordings.
