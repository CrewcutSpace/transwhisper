#!/bin/sh
# Builds the system audio helper. The Info.plist and the (ad-hoc) signature are what lets
# macOS recognise the binary when asking for the system audio recording permission.
set -e
cd "$(dirname "$0")"
swiftc -O main.swift -o audiotap \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist
codesign --force --sign - --identifier space.crewcut.transwhisper.audiotap audiotap
echo "built $(pwd)/audiotap"
