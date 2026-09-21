#!/bin/sh
# Builds the macOS translator helper.
set -e
cd "$(dirname "$0")"
swiftc -O main.swift -o appletranslate
echo "built $(pwd)/appletranslate"
