#!/bin/bash
# Double-click launcher (macOS) for the web-tech oTree Lab Launcher.
#
# As of v1.1.0 this runs in BROWSER MODE: it serves the UI over a tiny local
# HTTP server (standard library only) and opens it in your DEFAULT BROWSER.
# There is NO pywebview and NO pyobjc, so nothing has to be compiled -- the
# first run is fast and cannot fail on a missing/mismatched native wheel.
#
# It still uses a private virtualenv in your home folder (outside iCloud) so it
# never touches the Homebrew/system Python (which blocks system-wide pip
# installs). The venv needs no third-party packages for browser mode.
#
# This script sits at the repo root; the app code lives in app/ and your config
# and maps live in data/ at the repo root.
cd "$(dirname "$0")" || exit 1
# Prefer Python 3.12/3.11, else whatever python3 is on PATH. Browser mode runs on
# any of them (standard library only).
PYBIN="$(command -v python3.12 || command -v python3.11 || command -v python3)"
VENV="$HOME/.otree-lab-launcher-venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "First run: creating a private Python environment with $PYBIN (one-time)..."
  "$PYBIN" -m venv "$VENV" || { echo "Could not create the venv."; read -r _; exit 1; }
fi
# Browser mode needs only the standard library, so this installs nothing heavy
# (requirements-web.txt lists no required packages). It stays here so any future
# lightweight dependency flows through, and it never tries to compile pyobjc.
"$VENV/bin/python" -m pip install -q -r app/requirements-web.txt >/dev/null 2>&1 || true
# --browser forces the stdlib HTTP + default-browser mode (it is also the
# default now; passed explicitly so an older build still starts in browser mode).
exec "$VENV/bin/python" app/otree_launcher_web.py --browser
