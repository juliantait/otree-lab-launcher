' oTree Lab Launcher, web version (Windows) - double-click for a zero-window start.
'
' The web app needs its private virtualenv (created and populated by the .bat on
' first run), so this script runs the matching .bat with a HIDDEN window (the "0"
' in the Run call below). First run does the one-time setup invisibly; later runs
' skip straight to launching the app under pythonw.exe, leaving no console box.
'
' The script resolves its own folder, so the repo can live anywhere. If the app
' fails to start before its window appears, look for the crash log at
'   %USERPROFILE%\otree-lab-launcher.log
' and the detailed web log at  _ai\web_launcher.log

Option Explicit
Dim fso, sh, q, scriptDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
q = Chr(34)   ' a double-quote character, for quoting the path
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Run the .bat hidden (0) and do not wait (False), so this script exits at once.
sh.Run q & scriptDir & "\Start oTree Lab Launcher (web).bat" & q, 0, False
