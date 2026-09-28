#!/usr/bin/env bash
# Double-click this in Finder to start MTG Compare — no Terminal typing needed.
# This window stays open while the app runs; close it (or press Ctrl-C) to stop.
# All the actual logic lives in run.sh — this just launches it.
cd "$(dirname "$0")"
exec ./run.sh "$@"
