#!/usr/bin/env bash
# HIMA ground station on Linux: bash launcher/start_station.sh [--no-dashboard] [--listen 0.0.0.0:8765]
# Receives the drone's mission packages over the base Wi-Fi, checks them, adds them to the archive, opens the dashboard.
cd "$(dirname "$0")/.." || exit 1
exec python3 launcher/hima_start.py --station "$@"
