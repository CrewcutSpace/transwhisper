#!/bin/sh
# Builds transwhisper.app (into /Applications, or the folder given). The app runs main.py from
# this project folder with its .venv, so after a git pull it runs the new code without a rebuild.
set -e
cd "$(dirname "$0")"
project="$(cd .. && pwd)"
destination="${1:-/Applications}"
if [ ! -x "$project/.venv/bin/python" ]; then
    echo "No .venv in $project: set up the Python environment first (README, step 3)" >&2
    exit 1
fi

build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
bundle="$build/transwhisper.app"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
swiftc -O launcher.swift -o "$bundle/Contents/MacOS/transwhisper"
sed "s|@PROJECT@|$project|" Info.plist > "$bundle/Contents/Info.plist"
"$project/.venv/bin/python" make_icon.py "$build/AppIcon.iconset"
iconutil -c icns "$build/AppIcon.iconset" -o "$bundle/Contents/Resources/AppIcon.icns"
codesign --force --sign - --identifier space.crewcut.transwhisper "$bundle"

rm -rf "$destination/transwhisper.app"
mv "$bundle" "$destination/"
echo "built $destination/transwhisper.app"
