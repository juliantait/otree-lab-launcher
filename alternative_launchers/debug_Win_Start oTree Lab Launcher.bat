@echo off
REM ===========================================================================
REM  oTree Lab Launcher (Windows) - BROWSER mode, VISIBLE console (DEBUG variant).
REM
REM  This is the visible-console complement to the default "Win_Start oTree Lab
REM  Launcher.vbs". Both run the SAME web UI in BROWSER mode: a tiny local HTTP
REM  server (standard library only) that opens the UI in your DEFAULT BROWSER.
REM  There is NO pywebview and NO venv (it runs on the system Python), so it
REM  works on ANY Python on PATH (including 3.13 / 3.14 where pythonnet has no
REM  build). The one pip step is a best-effort certifi install (pure Python, no
REM  build) for the update check; see the note by the pip line below.
REM
REM  The default .vbs runs it windowless under pythonw.exe (no console). THIS .bat
REM  runs it under python.exe in a VISIBLE console window instead, so you can
REM  watch the server log and CLOSE the console to stop the server (hence "debug").
REM
REM  Leave this window open while you work; CLOSING the console stops the server.
REM  This script lives in "alternative_launchers\", one level below the repo root,
REM  so %~dp0..\ points back at the root; the app code is in app\ and config and
REM  maps live in data\ at the repo root.
REM ===========================================================================

setlocal

set "APP=%~dp0..\app\otree_launcher_web.py"

REM This is the DEBUG variant, so turn on verbose console logging: the launcher
REM keeps the console handler at WARNING by default (quiet), and this env var
REM promotes it to DEBUG so the full http + heartbeat log shows on screen.
set "OTREE_LAB_LAUNCHER_DEBUG=1"

REM Best-effort: install certifi (a bundled CA-roots package) into the system
REM Python so the in-app update check can verify GitHub's HTTPS certificate.
REM certifi is pure Python (no build step), so this is safe on any Python. When
REM it is already present pip does a quick local check and exits with no network.
REM Failures are ignored (the app falls back to the Windows system trust store).
python -m pip install --quiet --disable-pip-version-check "certifi>=2024.7.4"

REM Serve the UI locally and open the default browser (--browser = no pywebview).
python "%APP%" --browser

REM Keep the window open if python exited with an error so the message is visible.
echo.
pause
endlocal
