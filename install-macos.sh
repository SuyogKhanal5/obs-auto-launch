#!/bin/sh
# One-line macOS install for OBS Auto Recorder:
#
#   curl -fsSL https://raw.githubusercontent.com/SuyogKhanal5/obs-auto-launch/main/install-macos.sh | sh
#
# Downloads the installer for this Mac (Apple Silicon or Intel) from the latest release and opens
# it. The app isn't signed with an Apple Developer ID, so a copy downloaded through a browser is
# blocked by Gatekeeper until approved under Privacy & Security; a download made with curl isn't
# marked as coming from the internet, so the installer opens straight away.
#
# For testing: OAR_INSTALLER_URL overrides where the DMG comes from (file:// works), and
# OAR_DRY_RUN=1 stops before opening the installer and prints where it was prepared.
set -eu

REPO="SuyogKhanal5/obs-auto-launch"

case "$(uname -m)" in
    arm64) ARCH=arm64 ;;
    x86_64) ARCH=x86_64 ;;
    *) echo "Unsupported Mac architecture: $(uname -m)" >&2; exit 1 ;;
esac
URL="${OAR_INSTALLER_URL:-https://github.com/$REPO/releases/latest/download/OBSAutoRecorderInstaller-macos-$ARCH.dmg}"
APP_NAME="OBS Auto Recorder Installer.app"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/obsautorecorder-install.XXXXXX")"
MOUNT="$WORK/mount"
cleanup() {
    hdiutil detach -quiet "$MOUNT" 2>/dev/null || true
    if [ "${OAR_DRY_RUN:-}" != "1" ]; then rm -rf "$WORK"; fi
}
trap cleanup EXIT

echo "Downloading the OBS Auto Recorder installer ($ARCH)..."
curl -fL --progress-bar -o "$WORK/installer.dmg" "$URL"

mkdir -p "$MOUNT"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MOUNT" "$WORK/installer.dmg"
ditto "$MOUNT/$APP_NAME" "$WORK/$APP_NAME"
hdiutil detach -quiet "$MOUNT"

if [ "${OAR_DRY_RUN:-}" = "1" ]; then
    echo "Prepared (not opened): $WORK/$APP_NAME"
    exit 0
fi

echo "Opening the installer..."
open -W "$WORK/$APP_NAME"
echo "Done."
