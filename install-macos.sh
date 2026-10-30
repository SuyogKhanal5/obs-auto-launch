#!/bin/sh
# One-line macOS install for OBS Auto Recorder:
#
#   curl -fsSL https://github.com/SuyogKhanal5/obs-auto-launch/releases/latest/download/install-macos.sh | sh
#
# Downloads the installer for this Mac (Apple Silicon or Intel) from the same release as this
# script and opens it. The app isn't signed with an Apple Developer ID, so a copy downloaded
# through a browser is blocked by Gatekeeper until approved under Privacy & Security; a download
# made with curl isn't marked as coming from the internet, so the installer opens straight away.
# Because that skips Gatekeeper, this script checks what it downloaded itself before opening it:
#   - the DMG against the SHA-256 published alongside it in the release, and
#   - the installer app against the project's signing certificate, whose fingerprint the release
#     build writes into this script (SIGNING_CERT_SHA1 below; a copy straight from the repo
#     has none, and skips that check).
#
# For testing: OAR_INSTALLER_URL overrides where the DMG comes from (file:// works; its
# checksum is fetched from the same URL + ".sha256"), and OAR_DRY_RUN=1 stops before opening
# the installer and prints where it was prepared.
set -eu

REPO="SuyogKhanal5/obs-auto-launch"
SIGNING_CERT_SHA1="@SIGNING_CERT_SHA1@"

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
curl -fsSL -o "$WORK/installer.dmg.sha256" "$URL.sha256"
EXPECTED="$(awk '{print $1}' "$WORK/installer.dmg.sha256")"
ACTUAL="$(shasum -a 256 "$WORK/installer.dmg" | awk '{print $1}')"
if [ -z "$EXPECTED" ] || [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "The download doesn't match its published checksum -- not opening it." >&2
    exit 1
fi

mkdir -p "$MOUNT"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MOUNT" "$WORK/installer.dmg"
ditto "$MOUNT/$APP_NAME" "$WORK/$APP_NAME"
hdiutil detach -quiet "$MOUNT"

case "$SIGNING_CERT_SHA1" in
    @*) echo "(No signing certificate pinned in this copy of the script; skipping the signature check.)" ;;
    *)
        if ! codesign --verify --deep --strict -R="certificate leaf = H\"$SIGNING_CERT_SHA1\"" "$WORK/$APP_NAME" 2>/dev/null; then
            echo "The installer isn't signed with OBS Auto Recorder's certificate -- not opening it." >&2
            exit 1
        fi
        ;;
esac

if [ "${OAR_DRY_RUN:-}" = "1" ]; then
    echo "Prepared (not opened): $WORK/$APP_NAME"
    exit 0
fi

echo "Opening the installer..."
open -W "$WORK/$APP_NAME"
echo "Done."
