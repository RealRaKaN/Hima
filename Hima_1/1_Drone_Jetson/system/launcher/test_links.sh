#!/usr/bin/env bash
# HIMA link tests on the Jetson (and other Linux): bash launcher/test_links.sh [--no-hands] [--compare] ...
# Every test of the links to the Pixhawk (TELEM2 UART, USB, telemetry radio); report in runs/link_tests/
# Options: bash launcher/test_links.sh --help
cd "$(dirname "$0")/.." || exit 1
exec python3 launcher/hima_start.py --links "$@"
