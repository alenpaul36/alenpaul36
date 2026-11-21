#!/bin/sh
# Double-click (Mac) or run (Linux) to start Jarvis. Needs Python 3.10+.
cd "$(dirname "$0")" || exit 1
command -v python3 >/dev/null || { echo "Python 3 is not installed: get it from https://www.python.org/downloads/"; read -r _; exit 1; }
echo "Installing what Jarvis needs (first time only)..."
python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt || { read -r _; exit 1; }
python3 jarvis_gui.py
