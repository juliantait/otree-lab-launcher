' oTree Lab Launcher (Windows) - double-click for a zero-window start.
'
' This runs the Tk launcher under pythonw.exe with a HIDDEN window (the "0" in
' the Run call below), so no black console box is left sitting behind the GUI.
' The matching .bat still works (with a brief flash) for anyone who prefers it.
'
' The script resolves its own folder, so the repo can live anywhere. If the app
' fails to start before its window appears, look for the crash log at
'   %USERPROFILE%\otree-lab-launcher.log

Option Explicit
Dim fso, sh, q, scriptDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
q = Chr(34)   ' a double-quote character, for quoting the path
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Run pythonw hidden (0) and do not wait (False), so this script exits at once.
sh.Run "pythonw " & q & scriptDir & "\otree_lab_launcher.py" & q, 0, False
