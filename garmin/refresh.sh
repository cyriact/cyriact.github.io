#!/usr/bin/env sh
# Pull new Garmin data into data/ and rebuild dashboard.html. Safe to run as often as you like.
cd "$(dirname "$0")" && python3 pull.py "$@" && python3 build.py
