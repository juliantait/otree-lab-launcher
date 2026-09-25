' oTree Lab Launcher (Windows) - double-click for a zero-window start.
'
' This is the DEFAULT (browser) launcher, windowless. It runs the web UI in
' BROWSER mode: a tiny
' local HTTP server (standard library only) that opens the UI in your DEFAULT
' BROWSER. There is NO pywebview and NO venv (it runs on the system Python), so
' it works on ANY Python on PATH (including 3.13 / 3.14). The one pip step is a
' best-effort install of certifi (pure Python, no build) for the update check;
' see the note by the sh.Run line below. It runs under pythonw.exe with a HIDDEN
' window (the "0" in the Run call), so NO black console box is left behind.
'
' Use this .vbs for a clean, no-window start. Use
' "alternative_launchers\debug_Win_Start oTree Lab Launcher.bat" instead when
' you want a VISIBLE console to watch the server or close it by hand.
'
' The server keeps running in the background after this script exits; close the
' browser tab and the server shuts itself down within about 12 seconds (it
' watches for a heartbeat from the open page). To stop it sooner, use the Quit
' button in the launcher, use the debug .bat in "alternative_launchers\" (whose
' console you can close), or end the pythonw.exe task.
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

' Best-effort: make sure certifi (a bundled CA-roots package) is installed so
' the in-app update check can verify GitHub's HTTPS certificate. Unlike the Mac
' launcher, this .vbs runs the app on the system Python (no venv), so we install
' into that Python's site-packages here. certifi is PURE PYTHON (no build step),
' so this is safe on ANY Python including 3.13/3.14. Run it HIDDEN (0) and WAIT
' (True) so it finishes before launch; when certifi is already present pip does
' a quick local check and exits with no network. We ignore any failure: if pip
' or Python is missing, or there is no network, the app falls back to the
' Windows system trust store (which already verifies GitHub) and launches
' normally. On Error Resume Next keeps a "python not found" error from aborting
' the launch below.
On Error Resume Next
sh.Run "python -m pip install --quiet --disable-pip-version-check ""certifi>=2024.7.4""", 0, True
On Error GoTo 0

' Run pythonw hidden (0) and do not wait (False), so this script exits at once.
' --browser forces the stdlib HTTP + default-browser mode (no pywebview).
sh.Run "pythonw " & q & appPath & q & " --browser", 0, False
