#!/usr/bin/env bash
# Set up HIMA on the drone computer (Jetson Orin, JetPack 6). Run from the system/ folder:
#   bash deploy/setup_jetson.sh
# The launcher does all of it (apt packages, .venv with the Jetson torch build, Ollama and its models, weights,
# camera, Pixhawk, preflight) and installs the hima-fly boot service (not enabled). One install path: see
# launcher/hima_start.py. This file stays so the old command keeps working.
exec python3 "$(cd "$(dirname "$0")/.." && pwd)/launcher/hima_start.py" --install-service "$@"
