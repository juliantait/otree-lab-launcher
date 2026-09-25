#!/bin/bash
# oTree Lab Launcher (SIMPLE / Tk variant) - double-click to open on macOS.
# If double-clicking does nothing, run:
#   chmod +x "Mac_Start oTree Lab Launcher (simple).command"
#
# This script lives in "Alternative Launchers/", one level below the repo root,
# so it goes up one folder to reach app/ (and data/).

cd "$(dirname "$0")/.." || exit 1

for PY in python3 python; do
  if command -v "$PY" >/dev/null 2>&1; then
    exec "$PY" app/otree_lab_launcher.py
  fi
done

echo "Could not find python3 on this Mac."
echo "Install Python 3 from python.org, then try again."
read -r -p "Press return to close this window. "
