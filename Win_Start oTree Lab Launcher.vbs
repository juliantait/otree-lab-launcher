' oTree Lab Launcher (Windows) - double-click for a zero-window start.
'
' This is the DEFAULT (browser) launcher, windowless. It runs the web UI in
' BROWSER mode: a tiny
' local HTTP server (standard library only) that opens the UI in your DEFAULT
' BROWSER. There is NO pywebview, NO venv and NO pip install, so it works on ANY
' Python on PATH (including 3.13 / 3.14). It runs under pythonw.exe with a HIDDEN
' window (the "0" in the Run call), so NO black console box is left behind.
'
' Use this .vbs for a clean, no-window start. Use
' "Alternative Launchers\Win_Start oTree Lab Launcher (debug).bat" instead when
' you want a VISIBLE console to watch the server or close it by hand.
'
' The server keeps running in the background after this script exits; close the
' browser tab and the server keeps serving. To stop it, use the (debug) .bat in
' "Alternative Launchers\" (whose console you can close) or end the pythonw.exe
' task.
'
' The script resolves its own folder (the repo root) and runs the app code in
' app\ with --browser. Config and maps live in data\ at the repo root. Because
' pythonw has no console, stdout/stderr are redirected to the logs; if nothing
' opens, look at
'   <repo root>\data\otree-lab-launcher.log   (startup crashes)
'   <repo root>\data\web_launcher.log         (detailed web log, incl. the URL)

Option Explicit
Dim fso, sh, q, scriptDir, appPath
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
q = Chr(34)   ' a double-quote character, for quoting the path
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appPath = scriptDir & "\app\otree_launcher_web.py"

' Run pythonw hidden (0) and do not wait (False), so this script exits at once.
' --browser forces the stdlib HTTP + default-browser mode (no pywebview).
sh.Run "pythonw " & q & appPath & q & " --browser", 0, False
