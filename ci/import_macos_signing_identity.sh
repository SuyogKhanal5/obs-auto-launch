#!/bin/sh
# Loads the signing certificate (see create_macos_signing_identity.sh) into a separate, temporary
# keychain that codesign can use without prompts, and prints the two environment variables
# ci/finalize_macos_app.py reads. Used by CI and for local release builds:
#
#   eval "$(ci/import_macos_signing_identity.sh <signing.p12> <password file> <keychain path>)"
#
# Prints nothing secret: the keychain's own password is random and thrown away (the keychain is
# left unlocked for this session only).
set -eu
P12="$1"; PASSWORD_FILE="$2"; KEYCHAIN="$3"
KEYCHAIN_PASSWORD="$(openssl rand -hex 24)"
security delete-keychain "$KEYCHAIN" >/dev/null 2>&1 || true
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security set-keychain-settings -lut 21600 "$KEYCHAIN"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security import "$P12" -k "$KEYCHAIN" -P "$(cat "$PASSWORD_FILE")" -T /usr/bin/codesign >/dev/null
security set-key-partition-list -S apple-tool:,apple: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
# codesign looks up the private key through the user's keychain search list, even with
# --keychain (confirmed: "The specified item could not be found in the keychain" otherwise).
# Added in front of the existing list, which is kept.
EXISTING="$(security list-keychains -d user | tr -d '"' | grep -v "^ *$KEYCHAIN\$" | xargs)"
security list-keychains -d user -s "$KEYCHAIN" $EXISTING
# Self-signed, so it's listed as untrusted (no -v); codesign signs with it by hash all the same.
IDENTITY="$(security find-identity -p codesigning "$KEYCHAIN" | awk '/OBS Auto Recorder Code Signing/ {print $2; exit}')"
if [ -z "$IDENTITY" ]; then echo "No signing identity found in $P12" >&2; exit 1; fi
echo "export OAR_MACOS_SIGNING_IDENTITY=$IDENTITY"
echo "export OAR_MACOS_KEYCHAIN=\"$KEYCHAIN\""
