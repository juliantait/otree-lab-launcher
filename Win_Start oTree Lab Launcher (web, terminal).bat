@echo off
REM ===========================================================================
REM  oTree Lab Launcher, web version (Windows) - BROWSER mode, VISIBLE console.
REM
REM  This is the complement to "Win_Start oTree Lab Launcher (web).vbs". Both run
REM  the SAME web UI in BROWSER mode: a tiny local HTTP server (standard library
REM  only) that opens the UI in your DEFAULT BROWSER. There is NO pywebview, NO
REM  venv and NO pip install, so it works on ANY Python on PATH (including 3.13 /
REM  3.14 where pythonnet has no build).
REM
REM  The .vbs runs it windowless under pythonw.exe (no console). THIS .bat runs it
REM  under python.exe in a VISIBLE console window instead, so you can watch the
REM  server log and CLOSE the console to stop the server.
REM
REM  Leave this window open while you work; CLOSING the console stops the server.
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
