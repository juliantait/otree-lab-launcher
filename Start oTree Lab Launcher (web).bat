@echo off
REM Double-click launcher (Windows) for the web-tech oTree Lab Launcher.
REM Uses a private virtualenv in the user profile so it never touches the
REM system Python install.
REM
REM VERSION PINS (see _ai\WEB_FREEZE_DIAGNOSIS.md for why): the WebView2 freeze
REM on Browse is a pythonnet/WebView2 COM+accessibility bug. The fix needs:
REM   * Python 3.11 or 3.12  (NOT 3.13/3.14 - pythonnet crashes on 3.13+)
REM   * pywebview==5.4  and  pythonnet==3.0.5  (never pythonnet 3.0.0/3.0.0.post1;
REM     if 3.0.5 misbehaves, try 3.0.3)  -- pinned in requirements-web.txt
REM   * Edge WebView2 Evergreen runtime updated to latest, then REBOOT
REM TODO(Julian): if a newer pin combo is confirmed, update requirements-web.txt.
cd /d "%~dp0"
set "VENV=%USERPROFILE%\.otree-lab-launcher-venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo First run: creating a private Python environment ^(one-time^)...
  python -m venv "%VENV%"
)

REM --- Python version guard: pythonnet crashes on 3.13+, so warn loudly. ---
for /f "delims=" %%V in ('"%VENV%\Scripts\python.exe" -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])"') do set "PYVER=%%V"
if %PYVER% GEQ 313 (
  echo.
  echo ============================================================
  echo  WARNING: this venv is Python %PYVER:~0,1%.%PYVER:~1% - pythonnet crashes on 3.13+.
  echo  Install Python 3.11 or 3.12, delete "%VENV%", and re-run.
  echo ============================================================
  echo.
)

REM Install pinned deps if pywebview is not already importable.
"%VENV%\Scripts\python.exe" -c "import webview" 2>nul || (
  echo Installing pinned launcher dependencies ^(one-time^)...
  "%VENV%\Scripts\python.exe" -m pip install --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install -r requirements-web.txt
)
REM Launch windowless: pythonw.exe has no console, so no black box is left
REM behind the GUI. "start" then exit means at most a brief flash for anyone who
REM double-clicked this .bat; the .vbs (which runs this .bat hidden) is the true
REM zero-window entry point. A startup crash before the window shows is written
REM to %USERPROFILE%\otree-lab-launcher.log.
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0otree_launcher_web.py"
