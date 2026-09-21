' oTree Lab Launcher, web version (Windows) - double-click for a zero-window start.
'
' The web app needs its own private virtualenv. This script creates and populates
' it on first run (invisibly), then launches the app under pythonw.exe with a
' HIDDEN window, so no black console box is left behind. The .bat launchers were
' removed; this .vbs is the Windows entry point and does the setup itself.
'
' VERSION PINS (see _ai\WEB_FREEZE_DIAGNOSIS.md for why): use Python 3.11 or 3.12
' (pythonnet crashes on 3.13+), with pywebview==5.4 and pythonnet==3.0.5, pinned
' in app\requirements-web.txt.
'
' The script resolves its own folder (the repo root) and runs the app code in
' app\. Your config and maps live in data\ at the repo root. If the app fails to
' start before its window appears, look for the crash log at
'   <repo root>\data\otree-lab-launcher.log
' and the detailed web log at  <repo root>\data\web_launcher.log

Option Explicit
Dim fso, sh, q, scriptDir, venv, py, pyw, appPath, req, rc
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
q = Chr(34)   ' a double-quote character, for quoting paths
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

venv = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.otree-lab-launcher-venv"
py = venv & "\Scripts\python.exe"
pyw = venv & "\Scripts\pythonw.exe"
appPath = scriptDir & "\app\otree_launcher_web.py"
req = scriptDir & "\app\requirements-web.txt"

' 1) First run: create the private venv (hidden, wait for it to finish).
If Not fso.FileExists(py) Then
  sh.Run "python -m venv " & q & venv & q, 0, True
End If

' 2) Install pinned deps only when pywebview is not already importable.
rc = sh.Run(q & py & q & " -c ""import webview""", 0, True)
If rc <> 0 Then
  sh.Run q & py & q & " -m pip install --upgrade pip", 0, True
  sh.Run q & py & q & " -m pip install -r " & q & req & q, 0, True
End If

' 3) Launch windowless (hidden) and do not wait, so this script exits at once.
sh.Run q & pyw & q & " " & q & appPath & q, 0, False
