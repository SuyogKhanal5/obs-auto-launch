"""Gives the PyInstaller-built macOS app its proper name and bundle identifier, then re-signs it.

PyInstaller names the bundle after --name ("OBSAutoRecorder", also used as its bundle id), which
is what macOS shows in System Settings' permission lists (Accessibility, Screen Recording) --
so the app appeared as a bare "OBSAutoRecorder", easy to confuse with the "Python" entry a
from-source run creates. The bundle's file and executable names stay OBSAutoRecorder, which the
installer relies on.

Usage: python ci/finalize_macos_app.py dist/OBSAutoRecorder.app
"""
import plistlib
import subprocess
import sys

DISPLAY_NAME = "OBS Auto Recorder"
BUNDLE_ID = "com.obsautorecorder.app"


def finalize(app_path):
    plist_path = f"{app_path}/Contents/Info.plist"
    with open(plist_path, "rb") as f:
        info = plistlib.load(f)
    info["CFBundleName"] = DISPLAY_NAME
    info["CFBundleDisplayName"] = DISPLAY_NAME
    info["CFBundleIdentifier"] = BUNDLE_ID
    with open(plist_path, "wb") as f:
        plistlib.dump(info, f)
    # Editing Info.plist invalidates the bundle's signature; macOS refuses to launch an
    # arm64 app with a broken one, so re-sign (ad hoc, same as PyInstaller's own signing).
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app_path], check=True)


if __name__ == "__main__":
    finalize(sys.argv[1])
