@echo off
REM ===========================================================================
REM  oTree Lab Launcher (Windows) - VISIBLE-TERMINAL variant of the .vbs.
REM
REM  This is the complement to "Win_Start oTree Lab Launcher.vbs". The .vbs runs
REM  the Tk launcher under pythonw.exe with a HIDDEN window (no console). THIS
REM  .bat instead runs it under python.exe in a VISIBLE console window, so lab
REM  staff can watch the logs and see any crash on screen.
REM
REM  Use the .vbs for a clean, no-window start; use this .bat when you want to
REM  see what the launcher is doing (troubleshooting, first setup, odd crashes).
REM
REM  The script resolves its own folder (the repo root) with %~dp0 and runs the
REM  app code in app\. Your config and maps live in data\ at the repo root. If
REM  the app fails, the console stays open (pause below) so the error is readable,
REM  and there is also a crash log at  <repo root>\data\otree-lab-launcher.log
REM ===========================================================================

setlocal

REM Run the Tk launcher in THIS console window (visible) under python.exe.
python "%~dp0app\otree_lab_launcher.py"

REM Keep the window open so any error / traceback stays on screen.
echo.
pause
endlocal
