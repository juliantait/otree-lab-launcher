@echo off
REM ===========================================================================
REM  oTree Lab Launcher (Windows) - BROWSER mode, VISIBLE console (DEBUG variant).
REM
REM  This is the visible-console complement to the default "Win_Start oTree Lab
REM  Launcher.vbs". Both run the SAME web UI in BROWSER mode: a tiny local HTTP
REM  server (standard library only) that opens the UI in your DEFAULT BROWSER.
REM  There is NO pywebview, NO venv and NO pip install, so it works on ANY Python
REM  on PATH (including 3.13 / 3.14 where pythonnet has no build).
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

REM Serve the UI locally and open the default browser (--browser = no pywebview).
python "%APP%" --browser

REM Keep the window open if python exited with an error so the message is visible.
echo.
pause
endlocal
