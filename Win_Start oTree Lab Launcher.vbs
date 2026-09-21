' oTree Lab Launcher (Windows) - double-click for a zero-window start.
'
' This runs the Tk launcher under pythonw.exe with a HIDDEN window (the "0" in
' the Run call below), so no black console box is left sitting behind the GUI.
' The .bat launchers were removed; this .vbs is the Windows entry point.
'
' The script resolves its own folder (the repo root) and runs the app code in
' app\. Your config and maps live in data\ at the repo root. If the app fails to
' start before its window appears, look for the crash log at
'   <repo root>\data\otree-lab-launcher.log

Option Explicit
Dim fso, sh, q, scriptDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
q = Chr(34)   ' a double-quote character, for quoting the path
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Run pythonw hidden (0) and do not wait (False), so this script exits at once.
sh.Run "pythonw " & q & scriptDir & "\app\otree_lab_launcher.py" & q, 0, False
