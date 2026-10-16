"""Gives the PyInstaller-built macOS app its proper name and bundle identifier, then re-signs it.

PyInstaller names the bundle after --name ("OBSAutoRecorder", also used as its bundle id), which
is what macOS shows in System Settings' permission lists (Accessibility, Screen Recording) --
so the app appeared as a bare "OBSAutoRecorder", easy to confuse with the "Python" entry a
from-source run creates. The bundle's file and executable names stay OBSAutoRecorder, which the
installer relies on.

It also adds the macOS-only native pieces built by macos/build.sh (the Wine & Java game audio
helper and its loopback driver) to Contents/Resources, and declares system-audio capture, which
macOS requires before it hands that helper any audio (a tap read without it is silent).

Usage: python ci/finalize_macos_app.py dist/OBSAutoRecorder.app [macos/build]
"""
import os
import plistlib
import shutil
import subprocess
import sys

DISPLAY_NAME = "OBS Auto Recorder"
BUNDLE_ID = "com.obsautorecorder.app"
NATIVE_FILES = ("process_audio_tap", "OBSAutoRecorderGameAudio.driver")
AUDIO_CAPTURE_USAGE = (
    "OBS Auto Recorder records the audio of games macOS can't capture app by app (Wine games, "
    "Java Minecraft) so they get their own Game Audio track."
)


def finalize(app_path, native_dir=None):
    plist_path = f"{app_path}/Contents/Info.plist"
    with open(plist_path, "rb") as f:
        info = plistlib.load(f)
    info["CFBundleName"] = DISPLAY_NAME
    info["CFBundleDisplayName"] = DISPLAY_NAME
    info["CFBundleIdentifier"] = BUNDLE_ID
    info["NSAudioCaptureUsageDescription"] = AUDIO_CAPTURE_USAGE
    with open(plist_path, "wb") as f:
        plistlib.dump(info, f)
    if native_dir:
        resources = os.path.join(app_path, "Contents", "Resources")
        for name in NATIVE_FILES:
            source = os.path.join(native_dir, name)
            target = os.path.join(resources, name)
            if os.path.isdir(target):
                shutil.rmtree(target)
            if os.path.isdir(source):
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
    # Editing Info.plist invalidates the bundle's signature; macOS refuses to launch an
    # arm64 app with a broken one, so re-sign (ad hoc, same as PyInstaller's own signing).
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app_path], check=True)
    # An ad hoc signature's default requirement is the build's own hash, and macOS ties
    # Accessibility/Screen Recording grants to it -- so every update silently lost them (the
    # switch in System Settings stays on but no longer applies). Requiring only the bundle id
    # lets a grant carry over to later builds. Outer bundle only: nested code has its own ids.
    subprocess.run(
        ["codesign", "--force", "--sign", "-", "--requirements",
         f'=designated => identifier "{BUNDLE_ID}"', app_path],
        check=True,
    )


if __name__ == "__main__":
    finalize(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
