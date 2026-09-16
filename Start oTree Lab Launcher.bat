@echo off
REM oTree Lab Launcher - double-click this file to open the launcher.
REM pythonw runs Python without leaving a black console window behind.

cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0otree_lab_launcher.py"
    goto :eof
)

REM Some machines only have the py launcher; pyw is its windowed version.
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "%~dp0otree_lab_launcher.py"
    goto :eof
)

echo Could not find pythonw or pyw on this machine.
echo Install Python 3 from python.org, or ask the lab manager.
pause
