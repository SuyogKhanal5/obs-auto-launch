#!/bin/sh
# Builds the macOS-only native pieces into macos/build/ (or the folder given):
#   process_audio_tap                 -- feeds a game's audio into the Game Audio device
#   OBSAutoRecorderGameAudio.driver   -- the Game Audio loopback device
# The packaged app carries both in Contents/Resources; running from source finds them here.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/build}"
mkdir -p "$OUT"
swiftc -O -target "$(uname -m)-apple-macos14.2" "$HERE/process_audio_tap.swift" -o "$OUT/process_audio_tap"
codesign --force --sign - "$OUT/process_audio_tap"
"$HERE/game_audio_driver/build.sh" "$OUT"
