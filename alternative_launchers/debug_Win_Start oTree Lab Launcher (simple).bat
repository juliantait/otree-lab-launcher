@echo off
REM ===========================================================================
REM  oTree Lab Launcher (Windows) - SIMPLE (Tk) launcher, DEBUG variant.
REM
REM  This is the visible-console complement to the simple .vbs. The .vbs runs the
REM  Tk launcher under pythonw.exe with a HIDDEN window (no console). THIS .bat
REM  instead runs it under python.exe in a VISIBLE console window, so you can
REM  watch the logs and see any crash on screen (hence "debug").
REM
REM  This script lives in "alternative_launchers\", one level below the repo root,
REM  so %~dp0..\ points back at the root; the app code is in app\ and your config
REM  and maps live in data\ at the repo root. If the app fails, the console stays
REM  open (pause below) so the error is readable, and there is also a crash log at
REM    <repo root>\data\otree-lab-launcher.log
REM ===========================================================================

setlocal

REM This is the DEBUG variant, so turn on verbose logging via the shared opt-in
REM env var (kept in step with the web debug launcher).
set "OTREE_LAB_LAUNCHER_DEBUG=1"

REM Run the Tk launcher in THIS console window (visible) under python.exe.
python "%~dp0..\app\otree_lab_launcher.py"

REM Keep the window open so any error / traceback stays on screen.
echo.
pause
endlocal
