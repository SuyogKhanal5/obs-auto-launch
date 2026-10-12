#!/bin/sh
# Builds OBSAutoRecorderGameAudio.driver (universal, ad hoc signed) into the given folder.
# Usage: macos/game_audio_driver/build.sh <output dir>
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$1/OBSAutoRecorderGameAudio.driver"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
cp "$HERE/Info.plist" "$OUT/Contents/Info.plist"
cp "$HERE/LICENSE-Apple-sample.txt" "$OUT/Contents/Resources/"
clang -bundle -O2 -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 \
    -framework CoreAudio -framework CoreFoundation \
    -o "$OUT/Contents/MacOS/OBSAutoRecorderGameAudio" "$HERE/GameAudioLoopback.c"
codesign --force --sign - "$OUT"
