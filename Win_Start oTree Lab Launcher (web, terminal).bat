@echo off
REM ===========================================================================
REM  oTree Lab Launcher, web version (Windows) - VISIBLE-TERMINAL variant.
REM
REM  This is the complement to "Win_Start oTree Lab Launcher (web).vbs". The .vbs
REM  does all the setup invisibly and runs the app under pythonw.exe with a
REM  HIDDEN window. THIS .bat mirrors the same logic but stays VISIBLE: it runs
REM  under python.exe in a console window so lab staff can watch the venv build,
REM  the pip install, and any crash on screen.
REM
REM  Use the .vbs for a clean, no-window start; use this .bat when you want to
REM  see what the launcher is doing (first-run venv build, troubleshooting).
REM
REM  VERSION PINS (see _ai\WEB_FREEZE_DIAGNOSIS.md for why): use Python 3.11 or
REM  3.12 (pythonnet crashes on 3.13+), with pywebview==5.4 and pythonnet==3.0.5,
REM  pinned in app\requirements-web.txt.
REM
REM  The script resolves its own folder (the repo root) with %~dp0 and runs the
REM  app code in app\. Your config and maps live in data\ at the repo root. Crash
REM  logs: <repo root>\data\otree-lab-launcher.log and the detailed web log at
REM  <repo root>\data\web_launcher.log
REM ===========================================================================

setlocal

set "VENV=%USERPROFILE%\.otree-lab-launcher-venv"
set "PY=%VENV%\Scripts\python.exe"
set "APP=%~dp0app\otree_launcher_web.py"
set "REQ=%~dp0app\requirements-web.txt"

REM 1) First run: create the private venv if its python.exe is not there yet.
if not exist "%PY%" (
  echo Creating private virtualenv at "%VENV%" ...
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo.
    echo ERROR: could not create the virtualenv. Is Python 3.11 or 3.12 on PATH?
    echo.
    pause
    endlocal
    exit /b 1
  )
)

REM 2) Install pinned deps only when pywebview is not already importable.
"%PY%" -c "import webview" 1>nul 2>nul
if errorlevel 1 (
  echo Installing web dependencies ^(first run^) ...
  "%PY%" -m pip install --upgrade pip
  if errorlevel 1 (
    echo.
    echo ERROR: could not upgrade pip in the virtualenv.
    echo.
    pause
    endlocal
    exit /b 1
  )
  "%PY%" -m pip install -r "%REQ%"
  if errorlevel 1 (
    echo.
    echo ERROR: could not install the pinned web dependencies from
    echo   "%REQ%"
    echo.
    pause
    endlocal
    exit /b 1
  )
)

REM 3) Launch the web app in THIS console window (visible) under python.exe
REM    (NOT pythonw.exe), so logs and any crash stay on screen.
"%PY%" "%APP%"

REM Keep the window open so any error / traceback stays on screen.
echo.
pause
endlocal
