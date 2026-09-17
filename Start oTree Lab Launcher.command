#!/bin/bash
# oTree Lab Launcher - double-click this file to open the launcher on macOS.
# If double-clicking does nothing, run: chmod +x "Start oTree Lab Launcher.command"

cd "$(dirname "$0")" || exit 1

for PY in python3 python; do
  if command -v "$PY" >/dev/null 2>&1; then
    exec "$PY" otree_lab_launcher.py
  fi
done

echo "Could not find python3 on this Mac."
echo "Install Python 3 from python.org, then try again."
read -r -p "Press return to close this window. "
