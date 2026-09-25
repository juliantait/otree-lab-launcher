@echo off
rem ===================================================================
rem  Win_Create desktop shortcut.bat
rem
rem  Double-click this ONCE from the oTree Lab Launcher folder. It works
rem  out its own location, then writes a correctly-targeted shortcut
rem
rem      Start oTree Lab Launcher.lnk
rem
rem  onto the current user's Desktop. The shortcut:
rem    * runs the windowless default launcher in this folder
rem        (Win_Start oTree Lab Launcher.vbs)
rem    * shows the lab logo (app\branding\logo.ico)
rem
rem  Nothing to edit by hand: the paths are computed from where THIS
rem  file lives, so it stays correct no matter where the folder is.
rem ===================================================================
setlocal EnableExtensions

rem --- This folder (drop the trailing backslash from %~dp0) ---
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

set "TARGET=%HERE%\Win_Start oTree Lab Launcher.vbs"
rem Single canonical logo. To rebrand, just regenerate app\branding\logo.ico.
set "ICON=%HERE%\app\branding\logo.ico"
set "WORKDIR=%HERE%"
set "LINKNAME=Start oTree Lab Launcher.lnk"

rem --- Sanity checks: the pieces we point at must exist ---
if not exist "%TARGET%" (
  echo ERROR: cannot find the launcher next to this file:
  echo        "%TARGET%"
  echo Make sure this .bat is inside the oTree Lab Launcher folder.
  pause
  exit /b 1
)
if not exist "%ICON%" (
  echo WARNING: the icon was not found:
  echo          "%ICON%"
  echo The shortcut will still be created, just without the oTree logo.
)

rem --- Build a tiny throwaway VBScript that creates the .lnk ---
set "MK=%TEMP%\_otree_make_shortcut_%RANDOM%.vbs"
> "%MK%" echo Set sh = CreateObject("WScript.Shell")
>>"%MK%" echo desktop = sh.SpecialFolders("Desktop")
>>"%MK%" echo Set lnk = sh.CreateShortcut(desktop ^& "\%LINKNAME%")
>>"%MK%" echo lnk.TargetPath = "%TARGET%"
>>"%MK%" echo lnk.WorkingDirectory = "%WORKDIR%"
>>"%MK%" echo lnk.IconLocation = "%ICON%, 0"
>>"%MK%" echo lnk.Description = "Start the oTree Lab Launcher"
>>"%MK%" echo lnk.WindowStyle = 1
>>"%MK%" echo lnk.Save

rem --- Show what will be written, then write it ---
echo.
echo Creating desktop shortcut...
echo   Shortcut : (your Desktop)\%LINKNAME%
echo   Target   : %TARGET%
echo   Icon     : %ICON%
echo.

cscript //nologo "%MK%"
set "RC=%ERRORLEVEL%"
del "%MK%" >nul 2>&1

if "%RC%"=="0" (
  echo Done. Look on your Desktop for "%LINKNAME%".
) else (
  echo Something went wrong creating the shortcut (code %RC%^).
)
echo.
pause
endlocal
