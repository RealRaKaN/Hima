#!/usr/bin/env bash
# HIMA launcher for the Jetson (and other Linux): bash launcher/start.sh [--fly] [--install-service] ...
# Options: python3 launcher/hima_start.py --help
cd "$(dirname "$0")/.." || exit 1
exec python3 launcher/hima_start.py "$@"
