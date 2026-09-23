@echo off
REM ===========================================================================
REM  oTree Lab Launcher, web version (Windows) - BROWSER mode, ZERO installs.
REM
REM  Runs the SAME web UI, but served from a tiny local HTTP server and opened in
REM  your default browser instead of a pywebview window. It uses ONLY the Python
REM  standard library (plus the launcher's own code), so there is NO venv, NO pip
REM  install, and NO pywebview / pythonnet -- which means it works on ANY Python,
REM  including 3.13 and 3.14 where pythonnet has no build.
REM
REM  It runs under the SYSTEM python on PATH, in THIS visible console window.
REM  Leave the window open while you work; CLOSING the console stops the server.
REM
REM  Config and maps live in data\ at the repo root, exactly as the other
REM  launchers. %~dp0 resolves this script's folder (the repo root); the app code
REM  is in app\.
REM ===========================================================================

setlocal

set "APP=%~dp0app\otree_launcher_web.py"

REM Serve the UI locally and open the default browser (--browser = no pywebview).
python "%APP%" --browser

REM Keep the window open if python exited with an error so the message is visible.
echo.
pause
endlocal
