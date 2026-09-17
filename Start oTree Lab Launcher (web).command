#!/bin/bash
# Double-click launcher (macOS) for the web-tech oTree Lab Launcher.
# Uses a private virtualenv in your home folder (outside iCloud) so it never
# touches the Homebrew/system Python (which blocks system-wide pip installs).
cd "$(dirname "$0")" || exit 1
# Prefer Python 3.12/3.11 — pyobjc/pywebview wheels are reliably available there.
# Avoid 3.13/3.14: pythonnet crashes on 3.13+ (Windows), and wheels may be
# missing on very new Pythons. See _ai/WEB_FREEZE_DIAGNOSIS.md for the pins.
PYBIN="$(command -v python3.12 || command -v python3.11 || command -v python3)"
VENV="$HOME/.otree-lab-launcher-venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "First run: creating a private Python environment with $PYBIN (one-time)…"
  "$PYBIN" -m venv "$VENV" || { echo "Could not create the venv."; read -r _; exit 1; }
fi
"$VENV/bin/python" -c "import webview" 2>/dev/null || {
  echo "Installing pinned launcher deps (one-time — pyobjc is large, can take a few minutes)…"
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/python" -m pip install -r requirements-web.txt || {
    echo; echo "Install failed (see above). If it was trying to COMPILE pyobjc, install"
    echo "Homebrew python@3.12 (brew install python@3.12) and run this again."; read -r _; exit 1; }
}
exec "$VENV/bin/python" otree_launcher_web.py
