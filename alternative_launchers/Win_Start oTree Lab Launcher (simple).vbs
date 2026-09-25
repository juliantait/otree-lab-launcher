' oTree Lab Launcher (Windows) - double-click for a zero-window start.
'
' This runs the Tk launcher under pythonw.exe with a HIDDEN window (the "0" in
' the Run call below), so no black console box is left sitting behind the GUI.
' This is the SIMPLE (Tk) launcher, kept as an alternative.
'
' This script lives in "alternative_launchers\", one level below the repo root,
' so it goes up one folder to reach app\ (and data\). If the app fails to start
' before its window appears, look for the crash log at
'   <repo root>\data\otree-lab-launcher.log

Option Explicit
Dim fso, sh, q, scriptDir, rootDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
q = Chr(34)   ' a double-quote character, for quoting the path
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
' Go up one level (out of "alternative_launchers\") to the repo root.
rootDir = fso.GetParentFolderName(scriptDir)

' Run pythonw hidden (0) and do not wait (False), so this script exits at once.
sh.Run "pythonw " & q & rootDir & "\app\otree_lab_launcher.py" & q, 0, False
