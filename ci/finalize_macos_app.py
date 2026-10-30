"""Gives the PyInstaller-built macOS app its proper name and bundle identifier, then signs it.

PyInstaller names the bundle after --name ("OBSAutoRecorder", also used as its bundle id), which
is what macOS shows in System Settings' permission lists (Accessibility, Screen Recording) --
so the app appeared as a bare "OBSAutoRecorder", easy to confuse with the "Python" entry a
from-source run creates. The bundle's file and executable names stay OBSAutoRecorder, which the
installer relies on.

It also adds the macOS-only native pieces built by macos/build.sh (the Wine & Java game audio
helper and its loopback driver) to Contents/Resources, and declares system-audio capture, which
macOS requires before it hands that helper any audio (a tap read without it is silent).

Signing -- this app holds Accessibility (it sees every keystroke), Screen Recording and System
Audio Recording, so its signature is what stops other programs borrowing those:
- With OAR_MACOS_SIGNING_IDENTITY set (see ci/create_macos_signing_identity.sh and
  ci/import_macos_signing_identity.sh), it's signed with that certificate. macOS then ties
  permission grants to "this bundle id AND this certificate": they survive updates, and nothing
  without the private key satisfies them.
- Without it (forks, local test builds), ad hoc: grants are tied to that exact build, so every
  rebuild needs them granted again -- safe, just less convenient.
Never an identifier-only requirement: confirmed live that it let an impostor app claiming this
bundle id get Accessibility and Screen Recording with no prompt.

Always with the hardened runtime, which refuses DYLD_INSERT_LIBRARIES -- otherwise any program
could inject code into this app (e.g. via its user-writable LaunchAgent plist) and use its
permissions. The exceptions it needs are in macos/app.entitlements.

Usage: python ci/finalize_macos_app.py dist/OBSAutoRecorder.app [macos/build]
"""
import os
import plistlib
import shutil
import subprocess
import sys

DISPLAY_NAME = "OBS Auto Recorder"
BUNDLE_ID = "com.obsautorecorder.app"
HELPER_NAME = "process_audio_tap"
DRIVER_NAME = "OBSAutoRecorderGameAudio.driver"
NATIVE_FILES = (HELPER_NAME, DRIVER_NAME)
AUDIO_CAPTURE_USAGE = (
    "OBS Auto Recorder records the audio of games macOS can't capture app by app (Wine games, "
    "Java Minecraft) so they get their own Game Audio track."
)
MACOS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "macos")
APP_ENTITLEMENTS = os.path.join(MACOS_DIR, "app.entitlements")
HELPER_ENTITLEMENTS = os.path.join(MACOS_DIR, "process_audio_tap.entitlements")


def codesign_command(path, identity=None, keychain=None, entitlements=None, deep=False, runtime=True):
    """The codesign invocation for one item: the given certificate identity, or ad hoc ("-")."""
    command = ["codesign", "--force", "--timestamp=none", "--sign", identity or "-"]
    if identity and keychain:
        command += ["--keychain", keychain]
    if runtime:
        command += ["--options", "runtime"]
    if entitlements:
        command += ["--entitlements", entitlements]
    if deep:
        command.append("--deep")
    return command + [path]


def sign(app_path, identity=None, keychain=None):
    """Signs everything inside out: all nested code with the app's entitlements, then the helper
    and driver with their own, then the bundle itself so its seal covers the final versions."""
    resources = os.path.join(app_path, "Contents", "Resources")
    subprocess.run(codesign_command(app_path, identity, keychain, APP_ENTITLEMENTS, deep=True), check=True)
    helper = os.path.join(resources, HELPER_NAME)
    if os.path.exists(helper):
        subprocess.run(codesign_command(helper, identity, keychain, HELPER_ENTITLEMENTS), check=True)
    driver = os.path.join(resources, DRIVER_NAME)
    if os.path.exists(driver):
        # Loaded into Core Audio's own driver host, which doesn't use this bundle's runtime flags.
        subprocess.run(codesign_command(driver, identity, keychain, runtime=False), check=True)
    subprocess.run(codesign_command(app_path, identity, keychain, APP_ENTITLEMENTS), check=True)


def finalize(app_path, native_dir=None, identity=None, keychain=None):
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
    sign(app_path, identity, keychain)


if __name__ == "__main__":
    identity = os.environ.get("OAR_MACOS_SIGNING_IDENTITY") or None
    keychain = os.environ.get("OAR_MACOS_KEYCHAIN") or None
    if sys.argv[1] == "--sign-only":
        # The installer app: same certificate and hardened runtime, nothing else changed.
        sign(sys.argv[2], identity, keychain)
    else:
        finalize(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, identity=identity, keychain=keychain)
