#!/usr/bin/env python3
"""oTree Lab Launcher: web-tech front end.

This is the re-skin of ``otree_lab_launcher.py``. Every button is HTML/CSS/JS
(``web/index.html``), but the real work (picking folders, validating the project,
saving configs, resetting the database and starting ``otree prodserver``) is done
here in Python, so the app keeps the full filesystem and process powers a browser
tab can never have.

    Web tech for the looks, a native Python process for the powers.

BROWSER MODE IS THE DEFAULT on every platform (v1.1.0): the UI is served over a
tiny standard-library HTTP server and opened in the operator's DEFAULT BROWSER.
That path uses ONLY the Python standard library (plus ``otree_core``) -- no
pywebview, no pythonnet, no pyobjc -- so it runs on any Python and never has to
build the heavy, fragile native GUI wheels that fail on macOS Python 3.12. Run
it with just::

    python otree_launcher_web.py

A native desktop window (pywebview) is an OPTIONAL opt-in via ``--window`` (or
``--pywebview``); it is never required, and if pywebview is not installed the
launcher falls back to browser mode automatically.

The pure logic (config model, DATABASE_URL, seat files, resetdb/prodserver
commands, the settings.py block, presets storage) is imported unchanged from
``otree_lab_launcher.py``; this file only wires that logic to the web UI.
"""

from __future__ import annotations

import functools
import getpass
import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback

import otree_core as core

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "web", "index.html")

# Browser-mode self-shutdown timing. The open page POSTs /api/heartbeat every
# ~HEARTBEAT_INTERVAL_MS ms, always carrying the tab's document.hidden state.
#
# The PRIMARY real-close signal is a navigator.sendBeacon to /api/quit (fired on
# pagehide/beforeunload), which shuts the server down INSTANTLY on an actual close.
# The heartbeat watchdog is only a GENEROUS FALLBACK for a crash / sleep / lost
# network: HEARTBEAT_TIMEOUT is deliberately well ABOVE browser background-tab
# timer throttling (~60s, not a few seconds) so a backgrounded-but-open tab -- whose
# heartbeats the browser throttles -- is NEVER falsely killed. In addition, the
# watchdog does not fire at all while the tab reports itself hidden (see
# BrowserBridge._heartbeat_watchdog): a hidden tab is expected to be throttled.
# The interval constant is exported so the served page and the watchdog stay in step.
HEARTBEAT_INTERVAL_MS = 1500
HEARTBEAT_TIMEOUT = 60.0
HEARTBEAT_CHECK_INTERVAL = 1.5
# After a successful in-place update the server relaunches a fresh instance and
# then shuts itself down; this short delay lets the run_git_pull HTTP response
# reach the page (so it can show the "restarting" confirmation) before we stop.
RELAUNCH_SHUTDOWN_DELAY = 1.5

# WebView devtools are a security exposure (they run JS against the privileged
# js_api), so they are OFF unless this env var is explicitly set to a truthy
# value. Debugging on a dev machine: set OTREE_LAB_LAUNCHER_DEBUG=1.
DEBUG_ENV_VAR = "OTREE_LAB_LAUNCHER_DEBUG"


def webview_debug_enabled(env=None):
    """True only when the debug opt-in env var is set to a truthy value.

    Truthy means "1", "true", "yes" or "on" (case-insensitive); anything else
    (including unset or "0") is False, so the shipped launcher never opens
    devtools by accident.
    """
    env = os.environ if env is None else env
    value = str(env.get(DEBUG_ENV_VAR, "")).strip().lower()
    return value in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Diagnostics. On Windows the pywebview console shows nothing, so we tee a full
# log to data/web_launcher.log: startup, every Api method entry/exit, and any
# exception with traceback. Julian reads this file after clicking a button. The
# log lives in the app's data/ folder like everything else the launcher writes.
# ---------------------------------------------------------------------------

LOG_DIR = core.data_dir()
LOG_PATH = os.path.join(LOG_DIR, "web_launcher.log")

# On Windows the windowless launcher runs under pythonw.exe (no console), so
# stdout/stderr are None and a startup crash would be invisible. Point them at
# the crash log before anything else (in particular before the StreamHandler
# below captures sys.stderr). A normal console run, where the streams already
# exist, is left untouched. The log lives in data/, falling back to the user's
# home folder only if data/ cannot be created or written.
def _resolve_crash_log_path():
    data_target = os.path.join(core.data_dir(), "otree-lab-launcher.log")
    try:
        os.makedirs(core.data_dir(), exist_ok=True)
        with open(data_target, "a", encoding="utf-8"):
            pass
        return data_target
    except OSError:
        return os.path.join(os.path.expanduser("~"), "otree-lab-launcher.log")


CRASH_LOG_PATH = _resolve_crash_log_path()


class _NullStream(object):
    """A no-op stdout/stderr used only as a last resort, when running under
    pythonw (no console, so the real streams are None) AND the crash log cannot
    be opened. It swallows every write so that ``sys.stdout``/``sys.stderr`` are
    never None -- otherwise a plain ``sys.stderr.write(...)`` later in startup
    (e.g. in run_browser) would raise AttributeError and kill the windowless
    launcher before anything appeared."""

    def write(self, *_a, **_k):
        return 0

    def flush(self):
        pass


def _install_crash_log():
    if sys.stdout is not None and sys.stderr is not None:
        return
    # pythonw gives no console, so the missing stream(s) are None. Point them at
    # the crash log if we can; if even that fails, use an inert sink so the
    # streams are guaranteed non-None (no AttributeError on a later write).
    try:
        stream = open(CRASH_LOG_PATH, "a", buffering=1, encoding="utf-8")
    except OSError:
        stream = _NullStream()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _log_startup_crash(exc):
    try:
        with open(CRASH_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("\n" + "=" * 60 + "\n")
            fh.write("otree_launcher_web startup crash %s\n"
                     % time.strftime("%Y-%m-%d %H:%M:%S"))
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
    except OSError:
        pass


_install_crash_log()


def _setup_logging():
    logger = logging.getLogger("otree_web")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    # Also echo to stderr, so a visible terminal shows the same lines. The FILE
    # handler above always keeps the full DEBUG log; the console is quiet by
    # DEFAULT (WARNING and up, so the terminal is not flooded with DEBUG http
    # logs and per-heartbeat lines) and only goes verbose (DEBUG) when the debug
    # opt-in env var OTREE_LAB_LAUNCHER_DEBUG is truthy -- that is what the
    # debug_ launcher scripts set.
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if webview_debug_enabled() else logging.WARNING)
    logger.addHandler(sh)
    return logger


LOG = _setup_logging()


def _close_logging():
    """Close and detach the launcher's file log handler(s).

    Called on a clean shutdown so the launcher does not keep an open handle on
    ``data/web_launcher.log``. On Windows an open file handle blocks deleting the
    ``data/`` folder, so releasing it lets the operator delete the folder once
    the launcher has closed. Fail-soft: any error is swallowed."""
    for handler in list(LOG.handlers):
        if isinstance(handler, logging.FileHandler):
            try:
                handler.close()
            except Exception:
                pass
            try:
                LOG.removeHandler(handler)
            except Exception:
                pass

# The value an Api method returns if its body raises. Returning *something*
# JSON-serializable is what keeps the JS promise from hanging forever (a hung
# promise is indistinguishable from a frozen UI on EdgeChromium).
_SAFE_ERROR = {"ok": False, "error": True,
               "message": "Internal launcher error. See data/web_launcher.log."}


def api_call(fn):
    """Wrap a JS-invoked Api method: log entry/exit, and never raise into the
    WebView bridge. Method names are logged, but NOT their arguments (the field
    dicts carry admin/db passwords). On any exception the traceback goes to the
    log and a safe JSON value is returned so the caller's promise resolves.

    The enter/exit lines are INFO for normal methods but DEBUG for ``heartbeat``:
    the page beats every ~1.5s, so logging those at INFO would spam the console.
    They still land in the DEBUG file log.
    """
    log_level = logging.DEBUG if fn.__name__ == "heartbeat" else logging.INFO

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        LOG.log(log_level, "Api.%s: enter", fn.__name__)
        try:
            result = fn(self, *args, **kwargs)
            LOG.log(log_level, "Api.%s: exit ok", fn.__name__)
            return result
        except Exception:
            LOG.exception("Api.%s: EXCEPTION", fn.__name__)
            return dict(_SAFE_ERROR)
    return wrapper


# ---------------------------------------------------------------------------
# Turning a form's field dict into a config the logic layer understands
# ---------------------------------------------------------------------------


def fields_to_config(fields):
    """Merge a partial field dict from the UI onto the defaults and normalize."""
    cfg = dict(core.DEFAULT_CONFIG)
    for key in core.FIELD_KEYS:
        if key in fields and fields[key] is not None:
            cfg[key] = fields[key]
    return core.normalize_config(cfg)


def project_status(path):
    """Validation summary for a project path, in the shape the UI wants."""
    level, message = core.validate_project(path)
    name = os.path.basename(os.path.normpath(path)) if path else ""
    apps = core.find_app_packages(path) if path and os.path.isdir(path) else []
    # The confirmation line names only the COUNT ("... and N app packages:"); it
    # ends with a colon that introduces the app-name bullet list rendered under
    # it (the UI renders `apps` there). This mirrors the Tk launcher, which also
    # builds a bracket-free line and lists the apps separately. core.validate_project
    # keeps the parenthetical for other callers/tests, so we rebuild the line here.
    if level == "ok" and apps:
        message = "Looks like an oTree project with settings.py and %d app package%s:" % (
            len(apps), "" if len(apps) == 1 else "s")
    return {"level": level, "message": message, "name": name, "apps": apps}


def _lab_rows(presets):
    """Lab-settings table rows (every lab, shown and hidden), in stored order.

    Soft-deleted presets are skipped, so a deleted lab vanishes from the
    settings table while staying in the store JSON (core.soft_delete_lab_preset).
    """
    return [{"id": p["id"], "name": p["name"], "ip": p["ip"],
             "seats": p["seats"], "display": p["display"],
             "default_room": p["default_room"], "shortcut_label": p["shortcut_label"]}
            for p in presets if not p.get("deleted")]


def _lab_tiles(presets, config_lab=None):
    """The lab selector tiles for a config, with their seat maps.

    Every DISPLAYED preset, PLUS the given config's own saved lab even when it
    is hidden (``config_lab``) -- so a config saved on a now-hidden lab keeps a
    tile for it (core.lab_options_for_config). With no ``config_lab`` this is
    exactly the displayed labs, unchanged."""
    return [{"id": p["id"], "name": p["name"], "host": p["ip"],
             "seats": p["seats"], "map": p["map"],
             "default_room": p["default_room"], "shortcut_label": p["shortcut_label"]}
            for p in core.lab_options_for_config(presets, config_lab)]


def preset_row(preset):
    cfg = core.normalize_config(preset)
    project_path = (cfg.get("project_path") or "").strip()
    folder = os.path.basename(os.path.normpath(project_path)) if project_path else ""
    builtin = core.is_builtin(preset)
    return {
        "name": preset.get("name", ""),
        # The built-in Lab default is a launch TEMPLATE, never a saved config, so
        # it NEVER shows a run time (Job 2) -- only researcher configs do.
        "when": "" if builtin else core.format_last_run(preset.get("last_run")),
        "author": preset.get("author", ""),
        "builtin": builtin,
        # The raw lab id; the UI derives the lab-name suffix from it at display
        # time (using the matching lab preset's name) and never stores it.
        "lab": cfg.get("lab", ""),
        # The loaded project's folder name, or "" when no project is set.
        "folder": folder,
    }


# ---------------------------------------------------------------------------
# Browser-mode native folder picker (opened ON THE SERVER MACHINE).
#
# A plain browser cannot hand a folder PATH back to the page -- that is a native
# power a browser tab never has. BUT the browser-mode HTTP server runs LOCALLY on
# the operator's own machine, so it can pop a native folder dialog here, on the
# server side, and return the chosen path over POST /api/browse_folder. That
# gives Windows users on Python 3.14 (no pywebview) a REAL Browse dialog instead
# of only pasting a path.
#
# The dialog runs in a SHORT-LIVED SUBPROCESS: a tiny helper that uses ONLY the
# standard library (tkinter.filedialog.askdirectory). It creates a hidden Tk
# root, lifts it -topmost so the dialog comes to the front, runs askdirectory(),
# prints the chosen path to stdout and exits. Running it OUT OF PROCESS sidesteps
# every tkinter-in-a-thread hazard inside the long-lived threaded HTTP server
# (Tk must own its own thread/mainloop), and the whole process is gone the moment
# the user answers. The helper is launched with the WINDOWLESS interpreter
# (pythonw.exe, derived from sys.executable) plus CREATE_NO_WINDOW so no extra
# console flashes while the GUI dialog still shows. Cancel or any error yields an
# empty path, so the UI just keeps the paste-the-path field.
# ---------------------------------------------------------------------------

# Shared helper-source fragments so every dialog (folder / custom-folder / save)
# comes to the FOREGROUND the same way. The dialog runs in a subprocess whose
# Tk window does NOT steal focus by default -- on aqua especially it opens
# BEHIND the browser -- so before showing the dialog we lift the hidden root
# -topmost, update/lift/focus_force it, and on macOS also activate THIS process
# to the front via osascript (Tk from a subprocess cannot do that itself; we use
# osascript, never pyobjc). The osascript call is wrapped so a failure never
# blocks the dialog. Afterwards we drop -topmost and destroy the root.
_DIALOG_RAISE_FRONT = (
    "try:\n"
    "    root.attributes('-topmost', True)\n"
    "    root.update()\n"
    "    root.lift()\n"
    "    root.focus_force()\n"
    "except Exception:\n"
    "    pass\n"
    "if sys.platform == 'darwin':\n"
    "    try:\n"
    "        import os as _os, subprocess as _sp\n"
    "        _sp.run(['osascript', '-e',\n"
    "            'tell application \"System Events\" to set frontmost of "
    "(first process whose unix id is %d) to true' % _os.getpid()],\n"
    "            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=5)\n"
    "    except Exception:\n"
    "        pass\n"
)

_DIALOG_DROP_FRONT = (
    "try:\n"
    "    root.attributes('-topmost', False)\n"
    "except Exception:\n"
    "    pass\n"
    "try:\n"
    "    root.destroy()\n"
    "except Exception:\n"
    "    pass\n"
)

# The helper program source, run as ``python -c``. stdlib ONLY (tkinter + sys,
# plus os/subprocess for the macOS activation): it never imports otree_core or
# any third-party module, so it runs on the barest Python. It withdraws the
# root, raises it to the FOREGROUND (see :data:`_DIALOG_RAISE_FRONT`), runs
# askdirectory(), then writes the chosen path (empty string on cancel) to stdout
# as its sole output and exits.
_FOLDER_DIALOG_HELPER = (
    "import sys\n"
    "import tkinter\n"
    "from tkinter import filedialog\n"
    "root = tkinter.Tk()\n"
    "root.withdraw()\n"
    + _DIALOG_RAISE_FRONT +
    "path = filedialog.askdirectory(title='Choose your oTree project folder')\n"
    + _DIALOG_DROP_FRONT +
    "sys.stdout.write(path or '')\n"
    "sys.stdout.flush()\n"
)


def _build_folder_dialog_helper(title):
    """A stdlib-tkinter folder-picker helper source with a custom window title
    (JSON-encoded so any quote/backslash is safe). Mirrors
    :data:`_FOLDER_DIALOG_HELPER` but lets the caller name what folder to choose
    (e.g. where to save the participant-PC shortcuts)."""
    title_lit = json.dumps(str(title or "Choose a folder"))
    return (
        "import sys\n"
        "import tkinter\n"
        "from tkinter import filedialog\n"
        "root = tkinter.Tk()\n"
        "root.withdraw()\n"
        + _DIALOG_RAISE_FRONT +
        "path = filedialog.askdirectory(title=%s)\n" % title_lit +
        _DIALOG_DROP_FRONT +
        "sys.stdout.write(path or '')\n"
        "sys.stdout.flush()\n"
    )


def _pythonw_executable():
    """The windowless interpreter to run the dialog helper with, so no console
    flashes on Windows while the GUI dialog still shows.

    Derives ``pythonw.exe`` from ``sys.executable`` (``python.exe`` ->
    ``pythonw.exe``) when that file exists; otherwise returns ``sys.executable``
    unchanged (non-Windows, a frozen build, or no pythonw sitting next to
    python).
    """
    exe = sys.executable or ""
    if exe and sys.platform.startswith("win"):
        directory, name = os.path.split(exe)
        if name.lower() == "python.exe":
            candidate = os.path.join(directory, "pythonw.exe")
            if os.path.isfile(candidate):
                return candidate
    return exe


def _native_folder_dialog_subprocess(helper=None, executable=None, timeout=600):
    """Open a native folder dialog in a short-lived subprocess and return the
    chosen absolute path ("" on cancel, timeout or ANY error).

    ``helper`` / ``executable`` are injectable for tests (a fake helper that
    prints a known path -- or nothing, for cancel -- proves the plumbing without
    a real dialog); production uses the stdlib-tkinter ``_FOLDER_DIALOG_HELPER``
    on the windowless interpreter. ``core._no_window_flags`` adds CREATE_NO_WINDOW
    on Windows (0 elsewhere) so the console stays hidden while the GUI dialog
    still appears. Nothing here ever raises into the HTTP handler.
    """
    exe = executable or _pythonw_executable()
    if not exe:
        return ""
    src = _FOLDER_DIALOG_HELPER if helper is None else helper
    try:
        proc = subprocess.run(
            [exe, "-c", src],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, creationflags=core._no_window_flags())
    except Exception:
        LOG.exception("folder-dialog subprocess failed")
        return ""
    try:
        out = (proc.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""
    # The helper writes only the path; take the last non-empty line so any stray
    # tkinter/deprecation chatter that lands on stdout is ignored.
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _build_save_dialog_helper(default_name, ext):
    """The stdlib-tkinter SAVE-dialog helper source, with the suggested filename
    and default extension embedded as JSON literals (so any quote/backslash is
    safe). Mirrors :data:`_FOLDER_DIALOG_HELPER` but calls
    ``filedialog.asksaveasfilename`` -- the browser-mode counterpart of pywebview's
    ``create_file_dialog(SAVE_DIALOG)``."""
    extension = str(ext or "")
    if extension and not extension.startswith("."):
        extension = "." + extension
    name_lit = json.dumps(str(default_name or ""))
    ext_lit = json.dumps(extension)
    label = ("Shortcut (*%s)" % extension) if extension else "All files (*.*)"
    label_lit = json.dumps(label)
    return (
        "import sys\n"
        "import tkinter\n"
        "from tkinter import filedialog\n"
        "root = tkinter.Tk()\n"
        "root.withdraw()\n"
        + _DIALOG_RAISE_FRONT +
        "ext = %s\n" % ext_lit +
        "ftypes = [(%s, ('*' + ext) if ext else '*.*'), ('All files', '*.*')]\n" % label_lit +
        "path = filedialog.asksaveasfilename(title='Save the one-click shortcut',\n"
        "    initialfile=%s, defaultextension=ext, filetypes=ftypes)\n" % name_lit +
        _DIALOG_DROP_FRONT +
        "sys.stdout.write(path or '')\n"
        "sys.stdout.flush()\n"
    )


def _native_save_dialog_subprocess(default_name, ext=".bat", helper=None,
                                   executable=None, timeout=600):
    """Open a native SAVE dialog in a short-lived subprocess; return the chosen
    absolute path ("" on cancel, timeout or ANY error).

    The browser-mode counterpart of pywebview's ``create_file_dialog(SAVE_DIALOG)``:
    the browser-mode server runs on the operator's OWN machine, so a real Save-as
    dialog can pop here even though a plain browser cannot return a path. Same
    windowless / ``-topmost`` / ``CREATE_NO_WINDOW`` treatment as
    :func:`_native_folder_dialog_subprocess`, so no console flashes on Windows.
    ``helper`` / ``executable`` are injectable for tests. Never raises into the
    HTTP handler.
    """
    exe = executable or _pythonw_executable()
    if not exe:
        return ""
    src = _build_save_dialog_helper(default_name, ext) if helper is None else helper
    try:
        proc = subprocess.run(
            [exe, "-c", src],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, creationflags=core._no_window_flags())
    except Exception:
        LOG.exception("save-dialog subprocess failed")
        return ""
    try:
        out = (proc.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[-1] if lines else ""


# ---------------------------------------------------------------------------
# The JS API: every method here is callable from the page as
# window.pywebview.api.<name>(...). Keep the returns JSON-serializable.
# ---------------------------------------------------------------------------


class Api(object):
    def __init__(self, store_path=None):
        self.store_path = store_path or core.presets_path()
        # ONE in-process lock for every read-modify-write-save of the store
        # (self.presets / self.store_extra). The UI (js_api) methods run on the
        # WebView thread while launch / database / dialog workers run on their
        # own daemon threads and ALSO mutate-and-save the store; without this a
        # background last_run stamp and a UI "save as new" could interleave so
        # one save clobbers the other's change. Re-entrant so a helper that calls
        # another store-mutating method on the same thread does not deadlock.
        self._store_lock = threading.RLock()
        self.presets, self.store_extra = core.load_store(self.store_path)
        # The presets.json mtime at load, so _mutate_store can tell when ANOTHER
        # process (e.g. the headless one-click shortcut) wrote the store and merge
        # its change in before re-applying ours (cross-process lost-update guard).
        self._store_mtime = self._store_mtime_now()
        # Resolve WHICH database is the lab-shared default from the store's
        # default_database reference, so core.LAB_DB (what every DB_MODE_LAB launch
        # uses) points at the chosen database -- the setup-wizard DB by default, or
        # a custom DB promoted in Lab Settings. Mirrors the Tk app exactly.
        core.apply_default_database(self.store_extra)
        # This machine's lab identity (lab.local) configures the built-in
        # default's lab AND its room (from that lab's default_room). Applied in
        # memory to the built-in only; user configs are untouched. A no-op on
        # first launch (marker unset).
        core.apply_lab_marker(
            self.presets, lab_presets=core.lab_presets_from_store(self.store_extra))
        # The built-in Lab default is a launch TEMPLATE: force its last_run to
        # None on load so a stamp a previous version wrote is cleared (Job 2).
        core.clear_builtin_last_run(self.presets)
        # ...and force its project folder blank so it always opens Browse-first
        # (the launcher opens selected on the built-in every start, Job 2).
        core.clear_builtin_project_path(self.presets)
        self.presets = core.order_presets_for_display(self.presets)
        if not os.path.exists(self.store_path):
            try:
                self._mutate_store(lambda: None)
            except OSError:
                pass
        self.window = None  # set by main() once the window exists

    # -- helpers -----------------------------------------------------------

    def _mutate_store(self, mutate):
        """Serialise one read-modify-write-save of the store under the app lock.

        Holds ``self._store_lock`` across BOTH the mutation callback and the
        ``save_store`` that persists it, so no two threads can interleave their
        read-modify-write-save sequences (a lost update) and no thread can mutate
        ``self.presets`` / ``self.store_extra`` while another is serialising them
        to disk. ``mutate`` does the in-place change (and may return a value the
        caller wants); the whole store is then written atomically. Any
        ``save_store`` OSError propagates so callers can report it -- the
        in-memory mutation has still been applied.
        """
        with self._store_lock:
            # Cross-process lost-update guard: if another process wrote the store
            # since our last save, merge its change in (identity-preserving) before
            # applying ours, so a headless --run stamp is not clobbered here.
            self._reconcile_store()
            result = mutate()
            core.save_store(self.presets, self.store_extra, self.store_path)
            self._store_mtime = self._store_mtime_now()
            return result

    def _store_mtime_now(self):
        """The presets.json modification time, or None when it does not exist."""
        try:
            return os.path.getmtime(self.store_path)
        except OSError:
            return None

    def _reconcile_store(self):
        """Re-read the store and merge in another process's changes when the file
        changed on disk since our last save (see core.merge_store_from_disk).
        Called under the store lock at the top of every _mutate_store."""
        current = self._store_mtime_now()
        if current is None or current == self._store_mtime:
            return
        try:
            disk_presets, disk_extra = core.load_store(self.store_path)
        except Exception:
            return
        self.presets, self.store_extra = core.merge_store_from_disk(
            self.presets, self.store_extra, disk_presets, disk_extra)
        # Keep the app-owned built-in and the live LAB_DB consistent after
        # adopting disk content (these are re-derived, never launched-from state).
        core.apply_default_database(self.store_extra)
        core.apply_lab_marker(
            self.presets, lab_presets=core.lab_presets_from_store(self.store_extra))
        core.clear_builtin_last_run(self.presets)
        core.clear_builtin_project_path(self.presets)

    def _find(self, name):
        for preset in self.presets:
            if preset.get("name") == name:
                return preset
        return None

    def _config_fields(self, preset):
        cfg = core.normalize_config(preset)
        return {key: cfg[key] for key in core.FIELD_KEYS}

    def _lab_room_for(self, cfg=None):
        """This config's (or this machine's) lab default room, for the settings
        inspector / room picker. Falls back to the machine marker lab, then
        "study"."""
        presets = core.lab_presets_from_store(self.store_extra)
        lab = ""
        if cfg is not None:
            lab = str(cfg.get("lab", "") or "").strip()
        lab = lab or core.read_lab_marker() or ""
        return core.lab_default_room(presets, lab)

    def _default_author(self):
        """The author to prefill in Save As: last used, else the OS username."""
        remembered = str(self.store_extra.get("last_author", "")).strip()
        if remembered:
            return remembered
        try:
            return getpass.getuser()
        except Exception:
            return ""

    # NOTE on threading / the EdgeChromium freeze:
    # ``evaluate_js`` must only ever be called from a BACKGROUND thread, never
    # from inside a method that JavaScript invoked. On the Windows WebView2
    # backend a js_api handler runs on the WebView message-loop thread; calling
    # evaluate_js (or opening a native dialog) from there re-enters that loop
    # and deadlocks the whole UI forever. So ``_call_js``/``_log``/``_status``
    # and ``_callback`` below are only used from worker threads we spawn
    # ourselves (the launch/resetdb streamers and the file-dialog workers).
    # JS-invoked methods instead RETURN plain JSON and let the page update its
    # own DOM.

    def _call_js(self, snippet):
        """Run a JS snippet in the page. Background threads only (see note)."""
        if self.window is None:
            return
        try:
            self.window.evaluate_js(snippet)
        except Exception:
            LOG.exception("evaluate_js failed: %s", snippet[:80])

    def _callback(self, fn_name, payload):
        """Deliver a result to a named window callback from a worker thread."""
        self._call_js("window.%s && window.%s(%s)"
                      % (fn_name, fn_name, json.dumps(payload)))

    def _spawn(self, target, name):
        """Run work off the WebView thread (dialogs, streaming launches)."""
        LOG.info("spawn worker: %s", name)
        threading.Thread(target=target, name=name, daemon=True).start()

    def _log(self, level, text):
        """Push one line into the page's activity log. Worker threads only."""
        self._call_js("window.pywLog && window.pywLog(%s,%s)"
                      % (json.dumps(level), json.dumps(text)))

    def _status(self, level, text):
        """Set the page status line. Worker threads only."""
        self._call_js("window.pywStatus && window.pywStatus(%s,%s)"
                      % (json.dumps(level), json.dumps(text)))

    # -- initial state -----------------------------------------------------

    @api_call
    def get_initial_state(self):
        # Order at DISPLAY time (built-in default pinned top, then most-recently-
        # launched first) and OPEN selected on the last-launched config, falling
        # back to the pinned default when nothing has been launched. Both come
        # from core so the Tk face agrees exactly.
        ordered = core.order_presets_for_display(self.presets)
        rows = [preset_row(p) for p in ordered]
        selected = core.select_on_open(self.presets)
        # The labs the selector should offer, seeded from lab_info.json and
        # narrowed to the displayed ones. The JS builds its lab buttons and seat
        # maps from this list; no lab data is hardcoded in the page.
        all_presets = core.lab_presets_from_store(self.store_extra)
        if selected:
            fields = self._config_fields(selected)
        else:
            # A brand-new config's room follows the ACTIVE lab's default_room (this
            # machine's lab), not the hardcoded "study".
            fields = dict(core.DEFAULT_CONFIG)
            fields["room_name"] = core.lab_default_room(
                all_presets, core.read_lab_marker() or fields.get("lab"))
        # The per-config selector tiles: the displayed labs PLUS the initially
        # selected config's own lab even if it is hidden, so a config saved on a
        # now-hidden lab still shows (and keeps) its own lab (BUG B).
        labs = _lab_tiles(all_presets, fields.get("lab"))
        # The full preset list (incl. hidden ones) + the Postgres admin config
        # feed the Lab Settings page. Both live in the store's extra, read here
        # through the existing core helpers (no new logic).
        lab_presets = [
            {"id": p["id"], "name": p["name"], "ip": p["ip"],
             "seats": p["seats"], "display": p["display"],
             "default_room": p["default_room"], "shortcut_label": p["shortcut_label"]}
            for p in all_presets
        ]
        pg_admin = core.pg_admin_from_store(self.store_extra)
        return {
            "native": True,
            "presets_path": self.store_path,
            "configs": rows,
            "selected": selected.get("name") if selected else "",
            "fields": fields,
            "project": project_status(fields.get("project_path", "")),
            "settings": core.inspect_settings(
                fields.get("project_path", ""),
                core.lab_default_room(all_presets, fields.get("lab"))),
            # False on first run (lab_info.json absent) → the UI shows the
            # first-run setup WIZARD instead of an empty lab selector / seat map.
            "lab_info_present": core.lab_info_present(),
            # The map names the setup wizard offers for a lab to reference
            # (same source the Tk FirstRunWizard uses). "" = plain grid.
            "maps": core.available_maps(),
            "labs": labs,
            # Save As prefill, and this machine's lab identity ("" when unset,
            # which triggers the one-time first-launch chooser in the UI).
            "default_author": self._default_author(),
            "lab_marker": core.read_lab_marker() or "",
            "lab_presets": lab_presets,
            "pg_admin": pg_admin,
            # Round 3 database registry (global list + roster + default), read
            # from the store's extra through the shared core helpers so the web
            # picker, create dialog and Lab Settings match the Tk app exactly.
            "databases": core.known_databases_from_store(self.store_extra),
            "researchers": core.list_researchers(self.store_extra, self.presets),
            # The lab-shared default is now a REFERENCE (an id) to one entry in the
            # one database list; default_db_options is the set a user may pick from
            # (the lab built-in + every custom DB; SQLite excluded).
            "default_database": core.default_database_id(self.store_extra),
            "default_db_options": core.default_database_options(self.store_extra),
            # The persisted light/dark theme (data/ui_prefs.json). In browser mode
            # the served page already applied it pre-paint from an injected marker;
            # this lets the native pywebview path apply it right after boot too.
            "theme": core.load_ui_theme(),
            # GitHub Organisation Sync opt-in (default OFF): the tick-box state and
            # the configured org name (ui_prefs.json). Drives whether the page shows
            # the GitHub Org. + Git update buttons.
            "github_sync": {"enabled": core.load_github_sync_enabled(),
                            "org": core.load_github_org()},
        }

    @api_call
    def select_config(self, name):
        preset = self._find(name)
        if not preset:
            return {"ok": False, "message": "No config named %r." % name}
        fields = self._config_fields(preset)
        return {
            "ok": True,
            "fields": fields,
            # The per-config selector tiles: displayed labs UNION this config's
            # own lab (even if hidden), so loading it never drops its lab (BUG B).
            "labs": _lab_tiles(
                core.lab_presets_from_store(self.store_extra), fields.get("lab")),
            "project": project_status(fields.get("project_path", "")),
            "settings": core.inspect_settings(
                fields.get("project_path", ""), self._lab_room_for(fields)),
        }

    # -- native file dialogs ----------------------------------------------

    # Native dialogs are opened on a WORKER thread and their result is pushed
    # back to a page callback, so the js_api method returns at once and never
    # blocks the WebView thread on a modal (the EdgeChromium deadlock).

    @staticmethod
    def _dialog_path(result):
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    @api_call
    def pick_project_folder(self):
        # Browser mode has no native file dialog (that is a pywebview-only
        # power), so the Browse button falls back to the paste-the-path field.
        # Never import/call pywebview on the browser path.
        if getattr(self, "browser_mode", False):
            return {"ok": False, "browser": True,
                    "message": "Type or paste the project folder path in the box "
                               "(native folder pickers need the desktop app)."}
        self._spawn(self._dialog_project, "dlg-project")
        return {"ok": True, "pending": True}

    def _dialog_project(self):
        import webview
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            LOG.exception("folder dialog failed")
            return
        path = self._dialog_path(result)
        if not path:
            LOG.info("folder dialog cancelled")
            return
        self._callback("pywOnProjectPicked",
                       {"path": path, "project": project_status(path),
                        "settings": core.inspect_settings(path, self._lab_room_for())})

    @api_call
    def browse_folder(self):
        """Browser-mode native folder picker, opened ON THE SERVER MACHINE.

        A real browser cannot return a folder PATH, but the browser-mode server
        runs LOCALLY on the operator's machine, so it pops a native folder dialog
        here (a short-lived stdlib-tkinter subprocess, see
        ``_native_folder_dialog_subprocess``) and hands the chosen absolute path
        back SYNCHRONOUSLY. This is the browser-mode counterpart of the pywebview
        ``pick_project_folder`` (which uses ``create_file_dialog``); the page's
        Browse button posts here when it is in browser mode. Blocking is fine:
        in browser mode every /api call runs on its own HTTP-server thread, not
        the WebView message loop, so there is no re-entrancy deadlock.

        Returns ``{"ok": True, "path": ...}`` -- ``path`` is "" on cancel/error
        (the UI then just keeps the paste-the-path field). On a real pick it also
        carries the project + settings summaries, so the page renders the folder
        exactly as the native ``pywOnProjectPicked`` push does.
        """
        path = _native_folder_dialog_subprocess()
        if not path or not os.path.isdir(path):
            return {"ok": True, "path": ""}
        return {"ok": True, "path": path,
                "project": project_status(path),
                "settings": core.inspect_settings(path, self._lab_room_for())}

    @api_call
    def pick_participant_file(self):
        # Browser mode: no native dialog -> paste the path instead (see
        # pick_project_folder). Never touch pywebview on the browser path.
        if getattr(self, "browser_mode", False):
            return {"ok": False, "browser": True,
                    "message": "Type or paste the participant file path in the box "
                               "(native file pickers need the desktop app)."}
        self._spawn(self._dialog_participant, "dlg-file")
        return {"ok": True, "pending": True}

    def _dialog_participant(self):
        import webview
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Text files (*.txt)", "All files (*.*)"))
        except Exception:
            LOG.exception("participant-file dialog failed")
            return
        path = self._dialog_path(result)
        if not path:
            LOG.info("participant-file dialog cancelled")
            return
        summary = core.seat_summary({"seat_mode": core.SEAT_FILE, "seat_file": path})
        self._callback("pywOnFilePicked", {"path": path, "summary": summary})

    @api_call
    def validate_project(self, path):
        return {"project": project_status(path),
                "settings": core.inspect_settings(path, self._lab_room_for())}

    # -- saving / deleting configs ----------------------------------------

    @api_call
    def save_config_as(self, name, fields, author=None):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "message": "Give the config a name."}
        if not core.unique_name(name, self.presets):
            return {"ok": False, "message": "A config called %r already exists." % name}
        author = (author or "").strip() or self._default_author()
        preset = core.preset_from_fields(name, fields_to_config(fields), author=author)

        def _apply():
            self.presets.append(preset)
            self.presets = core.order_presets_for_display(self.presets)
            # Remember the author across sessions (round-tripped through
            # store_extra), so the next Save As prefills it. Kept
            # JSON-serialisable; unknown keys in store_extra are preserved.
            if preset.get("author"):
                self.store_extra["last_author"] = preset["author"]

        try:
            # Lock-guarded: a background launch stamping last_run cannot interleave
            # with this save and lose either change.
            self._mutate_store(_apply)
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        # Log line is RETURNED for the page to append, not pushed via
        # evaluate_js from this JS-invoked method (that would deadlock).
        return {"ok": True, "configs": [preset_row(p) for p in self.presets],
                "selected": name,
                "log": [["ok", "Saved config %r." % name]]}

    @api_call
    def delete_config(self, name):
        preset = self._find(name)
        if not preset:
            return {"ok": False, "message": "No config named %r." % name}
        # Built-in / built-in configs are the shared, app-owned defaults:
        # they can never be deleted, so nobody can remove a colleague's baseline.
        if core.is_builtin(preset):
            return {"ok": False, "builtin": True,
                    "message": "%r is a built-in config and cannot be deleted." % name}
        def _apply():
            self.presets = [p for p in self.presets if p is not preset]
            if not self.presets:
                self.presets = [core.default_preset()]

        try:
            self._mutate_store(_apply)
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "configs": [preset_row(p) for p in self.presets],
                "selected": self.presets[0].get("name")}

    def _apply_lab_identity(self, lab):
        """BUG A: after lab.local is (re)written, collapse the DISPLAYED labs to
        ``lab`` and re-point the built-in Lab default at it, persisting both
        under the store lock. Mirrors the Tk ``_set_lab_identity`` display step
        (core.apply_lab_identity), which the web path used to skip. Returns the
        refreshed lab-preset list (normalized)."""
        def _apply():
            presets = core.lab_presets_from_store(self.store_extra)
            ok, _msg, presets = core.apply_lab_identity(presets, lab)
            if ok:
                self.store_extra["lab_presets"] = presets
            # Re-point the built-in default's lab (and room) at this machine's
            # lab, using the just-collapsed list.
            core.apply_lab_marker(self.presets, lab, lab_presets=presets)
            return presets
        try:
            return self._mutate_store(_apply)
        except OSError:
            return core.lab_presets_from_store(self.store_extra)

    def _lab_identity_response(self, lab, current_config_name="", dirty=False):
        """The shared payload for set_/change_lab_marker: refreshed config rows,
        the refreshed lab-settings rows + the per-config selector tiles (now
        collapsed to ``lab``) so the page repaints the main selector and the Shown
        column live (BUG A).

        Crucially it does NOT clobber the config open on screen: the page passes
        its ``current_config_name`` and whether the form is ``dirty``. When the
        form is clean the SAME config's fields are returned (so the page reselects
        exactly what was open, not the built-in), and when it is dirty NO ``fields``
        are returned at all, so the page keeps its unsaved on-screen edits (the old
        code always returned self.presets[0], silently replacing them). Mirrors the
        Tk _set_lab_identity, which only reselects when nothing is dirty.
        """
        presets = self._apply_lab_identity(lab)
        preset = (core.find_lab_preset(lab, presets)
                  or core.find_lab_preset(lab, core.default_lab_presets()))
        target = self._find(current_config_name) if current_config_name else None
        resp = {"ok": True, "lab_marker": lab,
                "lab_name": preset.get("name") if preset else lab,
                "configs": [preset_row(p) for p in self.presets],
                "selected": (target.get("name") if target else (current_config_name or "")),
                "lab_presets": _lab_rows(presets)}
        config_lab = core.normalize_config(target).get("lab") if target is not None else None
        if not dirty:
            chosen = target or (self.presets[0] if self.presets else None)
            if chosen is not None:
                fields = self._config_fields(chosen)
                resp["selected"] = chosen.get("name", "")
                resp["fields"] = fields
                config_lab = fields.get("lab")
        resp["labs"] = _lab_tiles(presets, config_lab)
        return resp

    @api_call
    def set_lab_marker(self, lab, current_config_name="", dirty=False):
        """First-launch operator choice of this machine's lab.

        Writes lab.local ONCE. Refuses if a valid marker already exists, so the
        identity can never revert silently; changing it is a hand-edit of the
        file. ``lab`` is a lab id from the data-driven list (lab_info.json), so
        no small/large names are hardcoded. On success the built-in default's lab
        is updated in memory and the refreshed rows + selected config are
        returned for the page to repaint.
        """
        lab = (lab or "").strip()
        presets = core.lab_presets_from_store(self.store_extra)
        if not lab or core.find_lab_preset(lab, presets) is None:
            return {"ok": False, "message": "Choose one of the available labs."}
        if core.read_lab_marker() is not None:
            return {"ok": False, "already": True,
                    "message": "This machine's lab is already set in lab.local."}
        try:
            written = core.write_lab_marker(lab)
        except OSError as error:
            return {"ok": False, "message": "Could not write lab.local: %s" % error}
        if not written:
            return {"ok": False, "already": True,
                    "message": "This machine's lab is already set in lab.local."}
        # Collapse the displayed labs to the chosen one + re-point the default,
        # then hand the page refreshed rows/tiles to repaint (BUG A). The page's
        # current config + dirty flag are passed so unsaved on-screen edits survive.
        return self._lab_identity_response(lab, current_config_name, dirty)

    @api_call
    def change_lab_marker(self, lab, current_config_name="", dirty=False):
        """Lab Settings "which lab is this computer" CHANGE path (OVERWRITE).

        Unlike ``set_lab_marker`` (the first-run, refuse-if-exists chooser), this
        is the change-it-later control: it calls ``core.set_lab_marker`` which
        OVERWRITES lab.local, so a machine that already has an identity can be
        re-pointed at a different lab without hand-editing the file. It then
        re-applies the marker to the in-memory built-in default (under the store
        lock, persisted) and returns refreshed rows + selected so the page
        repaints and the built-in "Lab default" immediately shows the new lab.
        """
        lab = (lab or "").strip()
        presets = core.lab_presets_from_store(self.store_extra)
        if not lab or core.find_lab_preset(lab, presets) is None:
            return {"ok": False, "message": "Choose one of the available labs."}
        try:
            core.set_lab_marker(lab)
        except (OSError, ValueError) as error:
            return {"ok": False, "message": "Could not write lab.local: %s" % error}
        # OVERWRITE path: same as first-run once the marker is written -- collapse
        # the displayed labs to the chosen one, re-point the default, return the
        # refreshed rows/tiles so the selector shifts live (BUG A). The page's
        # current config + dirty flag keep unsaved on-screen edits intact.
        return self._lab_identity_response(lab, current_config_name, dirty)

    # -- room picker / create-database / lab-settings bridges --------------
    # Each maps straight onto an existing otree_core function; no launch logic
    # lives here. Used by the rebuilt web UI (room picker, Create-a-database,
    # the Lab Settings page).

    @api_call
    def list_project_rooms(self, project_path):
        """The room names the project's own settings.py defines, plus the lab room
        when the project has a LIVE lab support block (which defines it at launch),
        so a just-appended block lets the user pick "study" (Feature 3)."""
        return core.enumerate_rooms_for_picker(project_path, self._lab_room_for())

    # -- the global database registry + researcher roster (Round 3) --------
    # These map straight onto the shared core helpers so the web picker, create
    # dialog and Lab Settings render the SAME global list as the Tk app. The
    # registry is passive (only create_database touches Postgres).

    def _current_database_id(self, cfg):
        """The registry id of the database this config uses, for the picker to
        tick (mirror of the Tk ``current_database_id``): the built-ins by mode,
        a custom by its connection (name + host + user), or "" when nothing
        matches."""
        mode = cfg.get("db_mode")
        if mode == core.DB_MODE_NONE:
            return core.DB_BUILTIN_SQLITE
        if mode == core.DB_MODE_LAB:
            return core.DB_BUILTIN_LAB
        name = (cfg.get("db_name") or "").strip()
        host = (cfg.get("db_host") or "").strip()
        user = (cfg.get("db_user") or "").strip()
        for entry in core.known_databases_from_store(self.store_extra):
            if (entry["db_name"] == name and entry["db_host"] == host
                    and entry["db_user"] == user):
                return entry["id"]
        return ""

    @api_call
    def list_databases(self, fields=None):
        """The whole database picker list (Round 3, Task 1): the SQLite built-in,
        the lab shared built-in, then every custom database in the global
        registry. Each carries its creator ``researcher`` (grey in the UI).
        ``current_id`` ticks the database the on-screen config uses now."""
        cfg = fields_to_config(fields or {})
        return {"ok": True,
                "databases": core.list_databases(self.store_extra),
                "current_id": self._current_database_id(cfg)}

    @api_call
    def database_config_fields(self, db_id):
        """The config field overrides to apply when a picker entry is chosen
        (Round 3, Task 1). One path for all three kinds via
        ``core.database_config_fields``; ``None`` when the id is unknown."""
        entry = core.find_database(self.store_extra, db_id)
        if entry is None:
            return {"ok": False, "message": "No such database."}
        return {"ok": True, "entry": entry,
                "fields": core.database_config_fields(entry)}

    @api_call
    def list_researchers(self):
        """The shared researcher roster (Round 3, Task 2): the same list that
        feeds Save-as-new's author field AND the create-database Researcher
        field. ``suggested`` prefills the last author used."""
        suggested = str(self.store_extra.get("last_author", "")).strip() or self._default_author()
        return {"ok": True,
                "researchers": core.list_researchers(self.store_extra, self.presets),
                "suggested": suggested}

    @api_call
    def launch_history(self, limit=200):
        """The recent launch history (fable review F), newest first, for the
        read-only viewer opened from the bottom of Lab Settings. Read-only: it
        never manages or re-runs anything. Fail-soft in core."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 200
        return {"ok": True, "entries": core.read_sessions(limit=limit)}

    @api_call
    def app_version(self):
        """Just the build version, NO network. Feeds the whole-app sidebar footer
        (identity only). The update CHECK is deliberately NOT here -- it runs only
        when Lab Settings is opened (version_info), so the footer never triggers a
        network call on app launch (review I, Julian's final design)."""
        return {"ok": True, "version": core.APP_VERSION}

    @api_call
    def version_info(self, force=False):
        """The build version + a quiet, fail-soft once-a-day update check (fable
        review I). Called when Lab Settings is OPENED (not on app launch), and with
        ``force=True`` from the on-demand "Check for update" button (which bypasses
        the once-a-day cache and hits the network now). The JS only renders what
        this returns; the network decision and the newer-than comparison stay here
        in Python (re-skin rule). The result is persisted in data/ by core so the
        flag reads the stored value and stays visible even offline."""
        try:
            update = core.check_for_update(force=bool(force))
        except Exception:
            update = {"update_available": False, "current": core.APP_VERSION,
                      "remote_version": "", "checked": False, "check_failed": True,
                      "label": "", "tooltip": core.UPDATE_TOOLTIP,
                      "repo_url": core.REPO_URL}
        # Resolve the install type UP FRONT so the page can render the Update
        # control's FINAL action in one click (git pull for a git working tree, the
        # GitHub download link otherwise). is_git_install never raises.
        return {"ok": True, "version": core.APP_VERSION, "update": update,
                "is_git_repo": core.is_git_install()}

    @api_call
    def git_repo_status(self):
        """Is the launcher's OWN install a git working tree? Drives the Update
        button: a git install offers a one-click ``git pull`` (the Run pill); a
        plain download points at the GitHub page instead. Fail-soft (a False
        answer just routes to the download path)."""
        return {"ok": True, "is_git_repo": core.is_git_install(),
                "repo_url": core.REPO_URL}

    @api_call
    def run_git_pull(self):
        """Run ``git pull`` on the launcher's OWN install directory (core.git_pull,
        fail-soft), and on SUCCESS auto-restart: spawn a fresh detached launcher
        on the just-pulled code and then cleanly shut this server down. This
        updates and restarts ONLY the launcher; it never touches any
        oTree/experiment process. Returns the captured output plus ``relaunching``
        so the page can show the 'restarting' confirmation before this server
        stops."""
        result = core.git_pull()
        result.setdefault("ok", False)
        if result.get("ok"):
            browser_mode = bool(getattr(self, "browser_mode", False))
            relaunching = self._relaunch_after_update()
            result["relaunching"] = relaunching
            if relaunching:
                # In browser mode the new instance comes up on the SAME port with
                # the browser auto-open suppressed; the page reloads THIS tab onto
                # it (one tab). A native window just closes and reopens native, so
                # there is no tab to reload.
                result["browser_reload"] = browser_mode
                result["message"] = ("Updated, restarting the launcher (it reopens "
                                      "on the new version).")
        return result

    def _relaunch_after_update(self):
        """Spawn a fresh detached launcher IN THE SAME MODE, then (after a short
        delay so the HTTP response reaches the page) cleanly shut THIS server down
        via the existing request_shutdown path. Returns True when the fresh
        instance was spawned.

        Browser mode relaunches on the CURRENT port with the browser auto-open
        suppressed (so the one open tab can reload onto it, no orphan); a native
        window relaunches as a native window (no silent native->browser downgrade).

        Fail-soft: if the spawn fails we log it and do NOT shut down, so the
        current launcher stays usable (the operator can restart by hand). Only the
        launcher is ever restarted/stopped here; no experiment process is touched.
        """
        browser_mode = bool(getattr(self, "browser_mode", False))
        port = int(getattr(self, "current_port", 0) or 0)
        try:
            spawn_new_launcher(browser_mode=browser_mode, port=port)
        except Exception:
            LOG.exception("relaunch: could not spawn a fresh launcher")
            return False
        shutdown = getattr(self.window, "request_shutdown", None)
        if callable(shutdown):
            timer = threading.Timer(RELAUNCH_SHUTDOWN_DELAY, shutdown)
            timer.daemon = True
            timer.start()
        else:
            # pywebview window path: close it (ends the process) after the delay.
            timer = threading.Timer(RELAUNCH_SHUTDOWN_DELAY,
                                    lambda: self._spawn(self._do_close, "relaunch-close"))
            timer.daemon = True
            timer.start()
        return True

    @api_call
    def get_theme(self):
        """The persisted light/dark theme (data/ui_prefs.json). Fail-soft: a
        missing/broken file returns the 'dark' default."""
        return {"ok": True, "theme": core.load_ui_theme()}

    @api_call
    def set_theme(self, theme):
        """Persist the chosen light/dark theme to a per-user prefs file in data/
        (ui_prefs.json), so it survives a restart. Needed because in browser mode
        the page's localStorage is tied to the server PORT, which changes on the
        next launch; a file in data/ is the durable store. Fail-soft (never raises;
        the UI has already updated instantly)."""
        return {"ok": True, "theme": core.save_ui_theme(theme)}

    @api_call
    def get_github_sync(self):
        """The persisted GitHub Organisation Sync opt-in (data/ui_prefs.json).
        Fail-soft: a missing/broken file returns the OFF default and an empty org.
        """
        return {"ok": True, "enabled": core.load_github_sync_enabled(),
                "org": core.load_github_org()}

    @api_call
    def set_github_sync(self, enabled, org=""):
        """Persist the GitHub Organisation Sync tick box AND the organisation name
        to the per-user prefs file (ui_prefs.json), same mechanism as the theme.
        The tick box only shows/hides the buttons; nothing runs here. Fail-soft."""
        stored = core.save_github_sync_prefs(enabled, org)
        return {"ok": True, "enabled": stored["enabled"], "org": stored["org"]}

    @api_call
    def git_update_study(self, project_path):
        """Per-config Git update: run ``git pull`` in the SELECTED study folder
        (core.git_update_study, fail-soft), NEVER the launcher app/ folder and never
        any oTree/experiment process. Returns the classified outcome (not_repo /
        current / updated / error) with a clear message for the page to show."""
        return core.git_update_study(project_path or "")

    @api_call
    def clone_org_repo(self, repo):
        """GitHub Organisation Sync: clone ``<org>/<repo>`` in the BACKGROUND.

        The org comes from the saved prefs (set in Settings). A native folder dialog
        picks the destination, then the clone runs on a worker thread (so the WebView
        thread never blocks) using the machine's read-only git credential. Progress
        goes to the page status/log; the final result (with the cloned folder + its
        project/settings summaries for auto-select) is pushed via pywOnCloneDone.
        Returns immediately with ``pending`` True, or an error when the org is unset.
        """
        org = core.load_github_org()
        if not org:
            return {"ok": False,
                    "message": ("Set the GitHub organisation name in Settings > "
                                "GitHub Organisation Sync first.")}
        repo = (repo or "").strip()
        if not repo:
            return {"ok": False, "message": "Enter the experiment repository name."}
        self._spawn(lambda: self._do_clone_org_repo(org, repo), "git-clone")
        return {"ok": True, "pending": True}

    def _do_clone_org_repo(self, org, repo):
        """Worker: pick the destination folder, clone, and push pywOnCloneDone.

        Browser mode uses the short-lived stdlib-tkinter folder dialog subprocess;
        the native pywebview path uses create_file_dialog. On cancel it reports a
        cancelled result. On success it also builds the project + settings summaries
        so the page can auto-select the cloned folder exactly like a Browse pick."""
        dest = self._pick_folder_for_clone()
        if not dest:
            self._callback("pywOnCloneDone", {"ok": False, "cancelled": True,
                                              "message": "GitHub clone cancelled."})
            return
        self._status("", "Cloning %s/%s into %s ..." % (org, repo, dest))
        result = core.git_clone_org_repo(org, repo, dest)
        if result.get("ok") and result.get("path"):
            path = result["path"]
            result["project"] = project_status(path)
            result["settings"] = core.inspect_settings(path, self._lab_room_for())
        self._callback("pywOnCloneDone", result)

    def _pick_folder_for_clone(self):
        """Return the chosen destination folder path (or "" on cancel), using the
        right picker for the current mode. Browser mode runs the local subprocess
        dialog; native pywebview uses create_file_dialog on this worker thread."""
        if getattr(self, "browser_mode", False):
            return _native_folder_dialog_subprocess()
        try:
            import webview
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            LOG.exception("clone destination dialog failed")
            return ""
        return self._dialog_path(result) or ""

    @api_call
    def save_default_db(self, db_id):
        """Promote a database to the lab-shared default BY REFERENCE (its id) and
        re-resolve core.LAB_DB, so every DB_MODE_LAB launch now uses it (Lab
        Settings > Lab shared (default) database). The chosen database may be the
        setup-wizard lab DB or any custom DB created later -- the latter is the bug
        this fixes. Mirrors the Tk ``_save_default_db``."""
        db_id = (db_id or "").strip() or core.DB_BUILTIN_LAB
        try:
            self._mutate_store(
                lambda: core.set_default_database(self.store_extra, db_id))
        except ValueError as error:
            return {"ok": False, "message": str(error)}
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True,
                "default_database": core.default_database_id(self.store_extra)}

    @api_call
    def create_database(self, new_db, new_user="", new_password="", researcher="",
                        already_exists=False):
        """Create a Postgres database with the stored admin config (Feature 2),
        then, on a confirmed create, register it in the global registry with its
        creator researcher and persist the store (Round 3, Task 2).

        The admin credentials come from the store's ``pg_admin`` (set on the Lab
        Settings page), exactly as the Tk ``create_database_dialog`` uses them.
        Mirrors the Tk ``_run_create_database``: the Postgres user (a credential)
        is recorded separately from the researcher (the person). Registration
        must never lose a created database, so a registry error is reported but
        does not fail the create. Returns core.create_database's result dict; on
        success ``fields`` holds the Custom DB config for the page to auto-fill.

        ``already_exists`` (the create dialog's "Database already exists" tick)
        REGISTERS the connection WITHOUT running CREATE DATABASE, mirroring the Tk
        ``_run_register_existing_database``. The registry entry is identical either
        way, so the database is selectable/editable afterward.
        """
        if already_exists:
            return self._register_existing_database(new_db, new_user, new_password,
                                                    researcher)
        admin = core.pg_admin_from_store(self.store_extra)
        result = core.create_database(admin, new_db, new_user, new_password)
        if result.get("ok"):
            fields = result.get("fields") or {}
            try:
                entry = self._mutate_store(lambda: core.register_database(
                    self.store_extra, title=new_db, researcher=researcher,
                    connection=fields, postgres_user=fields.get("db_user", "")))
                result["registered"] = entry
                result["databases"] = core.known_databases_from_store(self.store_extra)
                result["researchers"] = core.list_researchers(self.store_extra, self.presets)
            except Exception as error:   # registration must never lose the DB
                result["register_error"] = str(error)
        return result

    def _register_existing_database(self, new_db, new_user, new_password, researcher):
        """Register an ALREADY-EXISTING database (no CREATE DATABASE). Host/port
        come from the Lab Settings admin config (where the database lives) and a
        blank user/password falls back to the admin role, so the resulting registry
        entry is IDENTICAL in shape to a freshly-created one (mirror of the Tk
        ``_run_register_existing_database``)."""
        new_db = (new_db or "").strip()
        if not new_db:
            return {"ok": False, "message": "Enter a database name."}
        admin = core.pg_admin_from_store(self.store_extra)
        a_user = str(admin.get("admin_username", "")).strip()
        a_pw = str(admin.get("admin_password", ""))
        user = (new_user or "").strip()
        fields = {
            "db_mode": core.DB_MODE_CUSTOM,
            "db_name": new_db,
            "db_user": user or a_user,
            "db_password": (new_password or "") if user else a_pw,
            "db_host": str(admin.get("admin_host", "")).strip(),
            "db_port": str(admin.get("admin_port", "")).strip(),
        }
        result = {"ok": True, "created_db": False, "registered_only": True,
                  "fields": fields,
                  "message": "Registered the existing database %r (not created)." % new_db}
        self._register_conn(fields, researcher, result)
        if result.get("register_error"):
            return {"ok": False,
                    "message": "Could not register the database: %s"
                               % result["register_error"]}
        return result

    @api_call
    def edit_database(self, db_id, fields):
        """Edit an EXISTING database entry (Feature 2): a custom registry database
        OR the lab shared built-in. All shaping/persistence is
        ``core.edit_database`` -- a custom entry is written back to the store and a
        lab-shared edit goes to lab_info.json (re-resolving the live LAB_DB).
        Returns the refreshed registry + default lists so the page can repaint.
        """
        fields = fields or {}
        updates = {}
        for key in ("title", "researcher", "db_name", "db_user", "db_password",
                    "db_host", "db_port"):
            if key in fields and fields[key] is not None:
                updates[key] = fields[key]
        try:
            entry = self._mutate_store(
                lambda: core.edit_database(self.store_extra, db_id, **updates))
        except ValueError as error:
            return {"ok": False, "message": str(error)}
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "entry": entry,
                "databases": core.known_databases_from_store(self.store_extra),
                "default_db_options": core.default_database_options(self.store_extra),
                "default_database": core.default_database_id(self.store_extra)}

    @api_call
    def delete_database(self, db_id):
        """Soft-delete a custom database from the Lab Settings custom-databases
        list. Flags the registry entry ``deleted`` (core.soft_delete_database) so
        it is hidden from every UI list but KEPT in presets.json (recoverable by
        hand-editing). It never touches Postgres or any real database -- only the
        launcher's record of it is hidden. If the database was the lab-shared
        default, the default reference is reset to the lab built-in. Returns the
        refreshed registry + default lists so the page can repaint."""
        try:
            ok, message = self._mutate_store(
                lambda: core.soft_delete_database(self.store_extra, db_id))
        except ValueError as error:
            return {"ok": False, "message": str(error)}
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": ok, "message": message,
                "databases": core.known_databases_from_store(self.store_extra),
                "default_db_options": core.default_database_options(self.store_extra),
                "default_database": core.default_database_id(self.store_extra)}

    @api_call
    def save_pg_admin(self, fields):
        """Persist the Postgres admin config to the store's extra (Lab Settings).

        Only used by Create-a-database, never as launch environment. Mirrors the
        Tk Lab Settings auto-save.
        """
        fields = fields or {}
        admin = {k: str(fields.get(k, "") or "") for k in core.PG_ADMIN_KEYS}
        try:
            self._mutate_store(
                lambda: self.store_extra.__setitem__("pg_admin", admin))
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "pg_admin": core.pg_admin_from_store(self.store_extra)}

    # -- first-run setup wizard -------------------------------------------

    def _register_conn(self, fields, researcher, result):
        """Append a provisioned database to the global registry + roster, in
        place, and attach the refreshed lists to ``result``. A registry error
        must never lose the database, so it is reported, not raised."""
        try:
            entry = self._mutate_store(lambda: core.register_database(
                self.store_extra, title=fields.get("db_name", ""),
                researcher=researcher, connection=fields,
                postgres_user=fields.get("db_user", "")))
            result["registered"] = entry
            result["databases"] = core.known_databases_from_store(self.store_extra)
            result["researchers"] = core.list_researchers(self.store_extra, self.presets)
        except Exception as error:   # registration must never lose the DB
            result["register_error"] = str(error)

    @api_call
    def provision_database(self, connection, create_new=False, admin=None,
                           researcher="", register=False):
        """The setup wizard's two-fold database control (also reusable by the
        Lab Settings add-database flow). All Postgres work stays in core.

        ``create_new`` False = LINK an existing database: the connection details
        are recorded and Postgres is never touched. ``create_new`` True = CREATE
        it: the admin credentials are saved (``save_pg_admin``, so later creates
        work) and ``core.create_database`` runs. An already-existing database of
        that name is treated as SUCCESS and used (not a hard error). A real
        failure returns the create's own message so the UI can show the reason.
        With ``register`` the confirmed database is added to the global registry
        (used for the optional extra databases; the lab default is instead
        recorded in lab_info.json by ``create_lab_info``).
        """
        conn = connection or {}
        fields = {
            "db_mode": core.DB_MODE_CUSTOM,
            "db_name": str(conn.get("db_name", "") or "").strip(),
            "db_user": str(conn.get("db_user", "") or "").strip(),
            "db_password": str(conn.get("db_password", "") or ""),
            "db_host": str(conn.get("db_host", "") or "localhost").strip() or "localhost",
            "db_port": str(conn.get("db_port", "") or "5432").strip() or "5432",
        }
        if not fields["db_name"]:
            return {"ok": False, "message": "Enter a database name."}

        if not create_new:
            result = {"ok": True, "created": False, "existed": None,
                      "fields": fields,
                      "message": "Using the existing database %r." % fields["db_name"]}
            if register:
                self._register_conn(fields, researcher, result)
            return result

        # Create-new: persist the admin creds, then create the database.
        saved = self.save_pg_admin(admin or {})
        if not saved.get("ok"):
            return saved
        admin_cfg = core.pg_admin_from_store(self.store_extra)
        res = core.create_database(admin_cfg, fields["db_name"],
                                   fields["db_user"], fields["db_password"])
        if res.get("ok"):
            result = {"ok": True, "created": True, "existed": False,
                      "fields": res.get("fields") or fields,
                      "message": res.get("message", "") or "Database created."}
            if register:
                self._register_conn(result["fields"], researcher, result)
            return result
        if res.get("reason") == "db_exists":
            # Already present: treat as success and use the details as entered.
            result = {"ok": True, "created": False, "existed": True,
                      "fields": fields,
                      "message": "Database %r already exists; using it." % fields["db_name"]}
            if register:
                self._register_conn(fields, researcher, result)
            return result
        # A real failure (bad admin creds / unreachable / privilege): surface it.
        return {"ok": False, "created": False, "reason": res.get("reason", ""),
                "message": res.get("message") or "Could not create the database."}

    @api_call
    def create_lab_info(self, labs, database, admin):
        """Write lab_info.json from the setup wizard, then reload so the app
        picks up the labs/credentials without a restart (web parity with the Tk
        FirstRunWizard's ``_create``). Thin wrapper: all shaping is
        ``core.build_lab_info``; all file IO + reload is core. Returns ``ok``
        plus a fresh initial state so the JS can re-render straight into the
        normal lab-present UI and drop the wizard.
        """
        # Gate on the SAME shared validator the Lab Settings "Add lab" path uses,
        # so a lab with an empty Host/IP (or no/invalid seats) can never be saved
        # here -- it would render <host> in the participant links and then hide
        # this wizard on every future start.
        ok, message = core.validate_wizard_labs(labs or [])
        if not ok:
            return {"ok": False, "message": message}
        try:
            data = core.build_lab_info(labs or [], database or {}, admin or {})
            core.save_lab_info(data)
            core.reload_lab_info()
            core.apply_default_database(self.store_extra)
        except Exception as error:
            LOG.exception("create_lab_info failed")
            return {"ok": False,
                    "message": "Could not save lab settings: %s" % error}
        return {"ok": True, "state": self.get_initial_state()}

    @api_call
    def use_example_lab_info(self):
        """Convenience mirror of the Tk wizard's "Use example values": write the
        shipped lab_info.example.json as lab_info.json and reload. Thin wrapper
        over ``core.load_example_lab_info`` + save + reload."""
        example = core.load_example_lab_info()
        if not example:
            return {"ok": False,
                    "message": "Could not read lab_info.example.json."}
        example.pop("_comment", None)
        try:
            core.save_lab_info(example)
            core.reload_lab_info()
            core.apply_default_database(self.store_extra)
        except Exception as error:
            LOG.exception("use_example_lab_info failed")
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "state": self.get_initial_state()}

    @api_call
    def set_lab_display(self, lab_id, display, config_lab=None):
        """Show/hide a lab in the main selector from the Lab Settings table.

        ``config_lab`` is the lab of the config currently open on screen; the
        returned selector tiles keep it even when it was just hidden, so hiding a
        lab does not drop the open config's own lab tile (BUG B, Tk parity)."""
        presets = core.lab_presets_from_store(self.store_extra)
        ok, message, presets = core.set_lab_display(presets, lab_id, display)
        if not ok:
            return {"ok": False, "message": message}
        try:
            self._mutate_store(
                lambda: self.store_extra.__setitem__("lab_presets", presets))
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "presets": _lab_rows(presets),
                "labs": _lab_tiles(presets, config_lab)}

    @api_call
    def add_lab(self, name, ip, seats, cols=0, default_room=None,
                shortcut_label=None, config_lab=None):
        """Add a lab preset from the Lab Settings page (web parity with Tk).

        Wraps core.add_lab_preset (validate → append), persists to the store, and
        returns the refreshed preset rows + selector tiles for the page to
        repaint. Also carries the per-lab default_room + optional shortcut_label.
        """
        presets = core.lab_presets_from_store(self.store_extra)
        ok, message, presets, preset = core.add_lab_preset(
            presets, name, ip, seats, display=True, cols=cols or 0,
            default_room=default_room, shortcut_label=shortcut_label)
        if not ok:
            return {"ok": False, "message": message}
        try:
            self._mutate_store(
                lambda: self._store_lab_presets(presets))
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "message": message, "presets": _lab_rows(presets),
                "labs": _lab_tiles(presets, config_lab),
                "configs": [preset_row(p) for p in self.presets],
                "new_id": preset["id"] if preset else ""}

    @api_call
    def update_lab(self, lab_id, name=None, ip=None, seats=None, cols=None,
                   default_room=None, shortcut_label=None, config_lab=None):
        """Edit an existing lab preset from the Lab Settings page (web/Tk parity).

        A changed ``default_room`` also re-derives the built-in Lab default's room
        live (via ``_store_lab_presets`` -> apply_lab_marker), so the default
        config follows the lab immediately."""
        presets = core.lab_presets_from_store(self.store_extra)
        ok, message, presets, preset = core.update_lab_preset(
            presets, lab_id, name=name, ip=ip, seats=seats, cols=cols,
            default_room=default_room, shortcut_label=shortcut_label)
        if not ok:
            return {"ok": False, "message": message}
        try:
            self._mutate_store(
                lambda: self._store_lab_presets(presets))
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "message": message, "presets": _lab_rows(presets),
                "labs": _lab_tiles(presets, config_lab),
                "configs": [preset_row(p) for p in self.presets]}

    def _store_lab_presets(self, presets):
        """Store new lab presets AND re-derive the built-in Lab default's room
        from the machine lab (a lab's default_room may have just changed).
        Called under the store lock (via _mutate_store)."""
        self.store_extra["lab_presets"] = presets
        core.apply_lab_marker(self.presets, lab_presets=presets)

    @api_call
    def delete_lab(self, lab_id, selected_lab=None, config_lab=None):
        """Soft-delete a lab preset from the Lab Settings Edit view (web/Tk parity).

        Flags the preset ``deleted`` (core.soft_delete_lab_preset) so it is
        hidden from every UI list but KEPT in presets.json (recoverable by
        hand-editing). Refuses to remove the last displayed lab, so the selector
        always keeps a lab. Returns the next lab to select when the deleted one
        was the current selection. ``config_lab`` keeps the open config's own
        (possibly hidden) lab in the returned tiles when it still exists (BUG B,
        Tk parity)."""
        presets = core.lab_presets_from_store(self.store_extra)
        ok, message, presets, next_selected = core.soft_delete_lab_preset(
            presets, lab_id, selected_lab=selected_lab)
        if not ok:
            return {"ok": False, "message": message}
        try:
            self._mutate_store(
                lambda: self.store_extra.__setitem__("lab_presets", presets))
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "message": message, "presets": _lab_rows(presets),
                "labs": _lab_tiles(presets, config_lab), "next_selected": next_selected}

    @api_call
    def export_pc_shortcuts(self, lab_id, hotkey_key=None):
        """Write a folder of per-seat Windows kiosk .lnk shortcuts for a lab.

        Web/Tk parity for review feature D. Reuses the lab's own host, room, seat
        list and shortcut label (core.export_participant_shortcuts) and always
        produces Windows .lnk files, whatever OS the launcher runs on. The
        destination folder is chosen with the SAME native folder-dialog machinery
        the project picker uses: ``create_file_dialog`` under pywebview (async,
        via ``pywOnShortcutsResult``), the server-side folder subprocess in
        browser mode (returned synchronously). Two entry points call this: the
        Add/Edit-lab tick box and the Lab Settings "Export PC shortcuts" button.

        ``hotkey_key`` is the optional single key from the "Global shortcut key"
        field; blank/None means no hotkey (the default). The SAME Ctrl+Alt+<key>
        hotkey is written to every seat .lnk when it is set.
        """
        presets = core.lab_presets_from_store(self.store_extra)
        preset = core.find_lab_preset(lab_id, presets)
        if preset is None:
            return {"ok": False, "message": "No lab with that id."}
        if not preset.get("seats"):
            return {"ok": False,
                    "message": "This lab has no seats, so there is nothing to make shortcuts for."}
        port = core.DEFAULT_CONFIG["port"]
        # Browser mode: pop a native folder dialog on the server machine and write
        # the bundle synchronously (every /api call runs on its own HTTP thread).
        if getattr(self, "browser_mode", False):
            dest = _native_folder_dialog_subprocess(
                helper=_build_folder_dialog_helper(
                    "Choose where to save the participant-PC shortcuts"))
            if not dest or not os.path.isdir(dest):
                return {"ok": True, "path": ""}      # cancelled
            return core.export_participant_shortcuts(
                dest, preset["name"], preset["ip"], preset["seats"],
                room=preset.get("default_room"), port=port,
                shortcut_label=preset.get("shortcut_label", ""),
                hotkey_key=hotkey_key)
        # Desktop (pywebview): pick the folder off the WebView thread, then push
        # the result back to the page.
        self._spawn(lambda: self._dialog_export_shortcuts(preset, port, hotkey_key),
                    "dlg-pcshortcuts")
        return {"ok": True, "pending": True}

    def _dialog_export_shortcuts(self, preset, port, hotkey_key=None):
        import webview
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            LOG.exception("shortcut folder dialog failed")
            self._callback("pywOnShortcutsResult",
                           {"ok": False, "message": "Folder dialog failed. See the log."})
            return
        dest = self._dialog_path(result)
        if not dest:
            LOG.info("shortcut folder dialog cancelled")
            return
        res = core.export_participant_shortcuts(
            dest, preset["name"], preset["ip"], preset["seats"],
            room=preset.get("default_room"), port=port,
            shortcut_label=preset.get("shortcut_label", ""),
            hotkey_key=hotkey_key)
        self._callback("pywOnShortcutsResult", res)

    # -- settings.py block -------------------------------------------------

    @api_call
    def settings_status(self, project_path):
        # Read-only: we detect whether the researcher's settings.py has the
        # block and report it. The launcher never writes into their code.
        return core.inspect_settings(project_path, self._lab_room_for())

    @api_call
    def settings_block_text(self):
        return core.LAB_BLOCK

    @api_call
    def append_settings_block(self, project_path):
        """Append the lab support block to the project's settings.py (Deliverable 3).

        This is the one action that writes into a researcher's own code, so it
        mirrors the Tk ``add_block`` flow exactly: refuse if there is no
        readable settings.py, refuse to append a second time when the block
        markers are already present, otherwise take a timestamped ``.bak``
        backup (in ``core.append_block``) and append. The confirmation click
        lives in the web UI's modal, the same way the Tk confirmation lives in
        its GUI handler (PRINCIPLES principle 4).
        """
        project_path = (project_path or "").strip()
        lab_room = self._lab_room_for()
        state = core.inspect_settings(project_path, lab_room)
        if not state["readable"]:
            return {"ok": False, "message": "Could not read %s" % state["path"]}
        if state["has_block"]:
            # Refuse to append twice: the markers are already in the file.
            return {"ok": False, "already": True,
                    "settings": state,
                    "message": "settings.py already has the oTree lab support block, "
                               "so it was left unchanged."}
        try:
            backup, path = core.append_block(project_path)
        except core.BlockAlreadyPresent:
            # A race: the block was appended between the check above and the lock
            # inside core.append_block. Report it as already-present, not an error.
            return {"ok": False, "already": True,
                    "settings": core.inspect_settings(project_path, lab_room),
                    "message": "settings.py already has the oTree lab support block, "
                               "so it was left unchanged."}
        except OSError as error:
            return {"ok": False, "message": "Could not add the block: %s" % error,
                    "log": [["err", "Could not add the block: %s" % error]]}
        return {"ok": True, "backup": backup, "path": path,
                "settings": core.inspect_settings(project_path, lab_room),
                "log": [["ok", "Backed up settings.py to %s" % backup],
                        ["ok", "Appended the oTree lab support block to %s" % path]]}

    @api_call
    def refresh_settings_block(self, project_path):
        """Replace an OUTDATED lab support block with the current one (Pass 7).

        Mirrors ``append_settings_block`` but for a project stuck on an old/stale
        block: ``core.append_block`` refuses when a marker is present, so this
        uses ``core.refresh_block`` (same lock + timestamped .bak + atomic write)
        to remove the old block region and append the current ``core.LAB_BLOCK``.
        Fully revertible from the backup. Wired to the get-ready "Refresh lab
        block" button.
        """
        project_path = (project_path or "").strip()
        lab_room = self._lab_room_for()
        state = core.inspect_settings(project_path, lab_room)
        if not state["readable"]:
            return {"ok": False, "message": "Could not read %s" % state["path"]}
        try:
            backup, path = core.refresh_block(project_path)
        except OSError as error:
            return {"ok": False, "message": "Could not refresh the block: %s" % error,
                    "log": [["err", "Could not refresh the block: %s" % error]]}
        return {"ok": True, "backup": backup, "path": path,
                "settings": core.inspect_settings(project_path, lab_room),
                "log": [["ok", "Backed up settings.py to %s" % backup],
                        ["ok", "Refreshed the oTree lab support block in %s" % path]]}

    # -- save a one-click shortcut -----------------------------------------

    @api_call
    def save_shortcut(self, name, fields):
        """Save a LIVE one-click shortcut for the current SAVED config.

        Mirrors the Tk ``save_shortcut``: the shortcut calls the launcher
        headlessly (``otree_lab_launcher.py --run "<name>"``), so it always
        reflects the latest saved settings and the DB password is NOT written
        into the file -- the secret stays in presets.json. It therefore requires
        a saved, unmodified config: an unsaved or edited setup is refused with a
        clear "save it first" message rather than a password-baked snapshot.
        """
        name = (name or "").strip()
        match = None
        for preset in self.presets:
            if str(preset.get("name", "")).strip() == name:
                match = preset
                break
        if match is None:
            return {"ok": False, "needs_save": True,
                    "message": "Save these settings as a named config first (Save as "
                               "new config), then create the one-click shortcut."}
        if core.configs_differ(fields_to_config(fields), match):
            return {"ok": False, "needs_save": True,
                    "message": 'This config ("%s") has unsaved changes. Save it first, '
                               "then create the one-click shortcut." % name}
        # The shortcut invokes the Tk launcher's headless entry point, resolved to
        # an absolute path next to this file (both apps ship in the same folder).
        launcher_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "otree_lab_launcher.py")
        shortcut = core.headless_shortcut(name, launcher_path)
        # Browser mode: no native Save dialog -> hand the file back to the page,
        # which downloads it through the browser. (A .command still needs a
        # `chmod +x` afterwards, which the page notes; a Windows .bat does not.)
        if getattr(self, "browser_mode", False):
            return {"ok": True, "browser": True, "download": True,
                    "filename": shortcut["filename"], "ext": shortcut["ext"],
                    "content": shortcut["content"]}
        self._spawn(lambda: self._dialog_export(shortcut["content"], shortcut["filename"],
                                                shortcut["ext"]), "dlg-export")
        return {"ok": True, "pending": True}

    def _dialog_export(self, text, default_name, ext=".bat"):
        import webview
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name,
                file_types=("One click shortcut (*%s)" % ext, "All files (*.*)"))
        except Exception:
            LOG.exception("save dialog failed")
            self._callback("pywOnBatResult",
                           {"ok": False, "message": "Save dialog failed. See the log."})
            return
        path = result if isinstance(result, str) else (result[0] if result else None)
        if not path:
            LOG.info("save dialog cancelled")
            return
        written = self._write_export_file(path, text)
        if written.get("ok"):
            self._callback("pywOnBatResult", {"ok": True, "path": written["path"]})
        else:
            self._callback("pywOnBatResult",
                           {"ok": False, "message": written.get("message", "Save failed.")})

    def _write_export_file(self, path, text):
        """Write an exported shortcut to ``path`` (utf-8, no newline translation)
        and set the executable bit for a shell shortcut. ONE writer, shared by the
        pywebview ``_dialog_export`` and the browser-mode ``save_file_dialog`` so
        both save the exact same bytes and permissions. Returns a small result
        dict; never raises."""
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError as error:
            return {"ok": False, "message": "Could not write %s: %s" % (path, error)}
        # A macOS/Unix shell shortcut (.command/.sh, or a shebang script) must be
        # executable or the OS refuses to run it ("no appropriate access
        # privileges"). Windows .vbs/.bat/.txt need no exec bit -- leave them.
        lower = path.lower()
        if lower.endswith(".command") or lower.endswith(".sh") or text.startswith("#!"):
            try:
                os.chmod(path, os.stat(path).st_mode | 0o111)
            except OSError:
                LOG.exception("could not set executable bit on %s", path)
        return {"ok": True, "path": path, "filename": os.path.basename(path)}

    @api_call
    def save_file_dialog(self, content, default_name, ext=".bat"):
        """BROWSER-MODE server-side Save-as for an exported file (the one-click
        shortcut, and any future export).

        pywebview's ``create_file_dialog(SAVE_DIALOG)`` does not exist in browser
        mode, so a plain browser cannot choose WHERE to save. The browser-mode
        server runs on the operator's OWN machine, so this pops a native Save
        dialog here (:func:`_native_save_dialog_subprocess`) and writes ``content``
        to the chosen path with the SAME bytes + executable-bit handling as the
        pywebview save (:meth:`_write_export_file`). Returns
        ``{"ok": True, "path": <abs>, "filename": ...}`` on save,
        ``{"ok": True, "path": ""}`` on cancel, or ``{"ok": False, "message": ...}``
        on a write error. Blocking is fine: in browser mode every /api call runs on
        its own HTTP-server thread.
        """
        content = "" if content is None else str(content)
        path = _native_save_dialog_subprocess(default_name, ext)
        if not path:
            return {"ok": True, "path": ""}      # cancelled
        return self._write_export_file(path, content)

    # -- the launch --------------------------------------------------------

    @api_call
    def launch_briefing(self, fields):
        """What to tell the experimenter BEFORE the server starts (Deliverable 4).

        A pure description: it starts nothing. The web UI shows this in a
        confirmation modal and only calls ``launch`` after an explicit OKAY.
        The behaviour described is the real, verified one: the lab machines
        open a per-seat link
        ``http://HOST:PORT/room/ROOM?participant_label=SEAT&welcome_page_ok=1``
        (always active; welcome_page_ok=1 skips oTree 6's Welcome page so the
        seat auto-admits), and a participant arriving turns their seat from a grey
        to a green presence badge on the monitor. If the room the researcher
        chose is NOT the ``study`` room the desktop shortcuts point at, that is
        flagged so they know the computers must open the chosen room's link
        instead of the shortcut.
        """
        cfg = fields_to_config(fields)
        # The whole host/room/caution decision stays in core.launch_briefing so
        # the web UI only renders it, the same dict the Tk app uses. Resolved
        # against THIS store's lab presets, so added labs and the Local host work.
        lab_presets = core.lab_presets_from_store(self.store_extra)
        briefing = core.launch_briefing(cfg, lab_presets)
        # The JS reads shared_db; core names it caution (True on the shared lab DB).
        briefing["shared_db"] = briefing.get("caution", False)
        return briefing

    @api_call
    def launch_issues(self, fields):
        """Everything to tell the user before a launch, as ONE ordered list.

        This is the data for the consolidated "Before you launch" screen, and it
        mirrors the Tk ``LauncherApp.gather_launch_issues`` exactly: the hard
        blocks (no fix), the missing-lab-support-block case (a warning with an
        inline one-click fix), and the fail-SOFT ``core.preflight`` warnings
        (psycopg2-missing, database, port, project/room, oTree). Each entry is
        ``{level:'block'|'warn', title, hint, fix, fix_label, info}`` where
        ``fix`` is a string the page maps to an inline action ('add_block') or ''
        for a hint-only item. Every host/room/db/psycopg2/oTree decision lives in
        core; nothing here decides whether to launch; the page only renders.

        A hard blocker on a field SUPPRESSES the softer warning about that same
        field, so the two are mutually exclusive: with no usable project folder
        the list carries only the blocking "choose your folder" item (with its
        [Choose folder…] action), never also the preflight "Project folder not
        found" warning that fires on the identical condition.
        """
        cfg = fields_to_config(fields)
        lab_presets = core.lab_presets_from_store(self.store_extra)
        issues = []
        folder_problem = self._project_folder_problem(cfg)
        # Preflight checks whose field already has a hard blocker above them.
        blocked_checks = {"project"} if folder_problem else set()
        for message in self._hard_block_problems(cfg, lab_presets):
            issue = {"level": "block", "title": message, "hint": "",
                     "fix": "", "fix_label": "", "info": ""}
            if folder_problem and message == folder_problem:
                # Fixable right on the screen: the page maps 'choose_folder' to a
                # [Choose folder…] button that opens the SAME native folder picker
                # as the main Browse (pick_project_folder), then re-checks.
                issue["fix"] = "choose_folder"
                issue["fix_label"] = "Choose folder…"
            issues.append(issue)
        block_state = self._block_state(cfg)
        if block_state.get("readable") and not block_state.get("has_block") \
                and core.effective_seat_mode(cfg) != core.SEAT_NONE:
            issues.append({
                "level": "warn",
                "title": "This project has no oTree lab support block, so the lab "
                         "room, seat board and lab database won’t take effect "
                         "without it.",
                "hint": "Adds a clearly-marked block to the end of settings.py, "
                        "after a timestamped .bak backup (fully revertible). The "
                        "manual copy/paste is on the info screen.",
                "fix": "add_block", "fix_label": "Add it for me", "info": "block"})
        elif block_state.get("needs_refresh"):
            # The block is present but OUTDATED (stale body / cut off): a refresh
            # replaces it in place. append_block refuses when a marker exists.
            issues.append({
                "level": "warn",
                "title": "This project's oTree lab support block is out of date, so "
                         "the lab room (especially with no seats) may not work.",
                "hint": "Refreshes the block in place to the current version, after "
                        "a timestamped .bak backup (fully revertible).",
                "fix": "refresh_block", "fix_label": "Refresh lab block",
                "info": "block"})
        for failure in core.preflight_failures(core.preflight(cfg, core.load_lab_info())):
            if failure.get("check") in blocked_checks:
                continue
            issue = {"level": "warn", "title": failure.get("message", ""),
                     "hint": str(failure.get("detail", "")),
                     "fix": "", "fix_label": "", "info": "", "rooms": []}
            # The "no settings.py / not an oTree project" check is a warning (you
            # CAN still launch), but it is very likely a wrong-folder mistake, so
            # flag it for RED error styling on the pre-launch screen.
            if failure.get("kind") == "not_otree":
                issue["danger"] = True
            # Inline action so the user can resolve it and Launch at once: the
            # page maps 'change_to_sqlite' / 'recheck' / 'pick_room' to a button
            # or (for pick_room) a "Rooms found" selector. Shared with the Tk
            # launcher via core.issue_fix_for so both agree.
            meta = core.issue_fix_for(failure)
            if meta.get("fix"):
                issue["fix"] = meta["fix"]
                issue["fix_label"] = meta.get("fix_label", "Fix")
                issue["rooms"] = list(meta.get("rooms", []))
                # Addendum (Round 3, Task 5): on a room-not-in-ROOMS issue, also
                # offer to DEFINE the chosen room in the project via the SAME safe
                # settings.py append path (timestamped .bak, marker-guarded,
                # refuse-if-present, revertible). Offered only when seats are used
                # (the block defines the room from the seat file) and the block is
                # not there yet. The page renders an "Add <room> room" button that
                # calls append_settings_block then re-checks. Mirror of the Tk
                # ``_configure_issue`` add_room addendum.
                if meta["fix"] == "pick_room":
                    room = (cfg.get("room_name") or "").strip()
                    path = (cfg.get("project_path") or "").strip()
                    if room and path and core.effective_seat_mode(cfg) != core.SEAT_NONE:
                        state = core.inspect_settings(path, self._lab_room_for(cfg))
                        if state.get("readable") and not state.get("has_block"):
                            issue["add_room"] = room
            issues.append(issue)
        # Non-blocking: the lab's default room changed since this config was saved.
        # One click switches THIS config to the lab default; launching as-is is
        # fine (silent when the rooms already match).
        mismatch = core.lab_room_mismatch(cfg, lab_presets)
        if mismatch:
            issues.append({
                "level": "warn", "title": mismatch["message"],
                "hint": "Existing saved configs keep their room; this switches only "
                        "this config. You can also just launch as-is.",
                "fix": "use_lab_room", "fix_label": "Use new default", "info": "",
                "lab_room": mismatch["lab_room"]})
        return {"ok": True, "issues": issues}

    def _block_state(self, cfg):
        """inspect_settings for this config's project, against its lab room. "" /
        missing folder -> an unreadable state (so no block warning fires)."""
        path = (cfg.get("project_path") or "").strip()
        if not path or not os.path.isdir(path):
            return {"readable": False, "has_block": False, "needs_refresh": False}
        return core.inspect_settings(path, self._lab_room_for(cfg))

    @staticmethod
    def _project_folder_problem(cfg):
        """The project-folder must-fix message, or "" when the folder is fine
        (mirror of the Tk ``_project_folder_problem``; same two conditions as
        before, split out so ``launch_issues`` can attach the inline
        [Choose folder…] fix to exactly this item)."""
        path = (cfg.get("project_path") or "").strip()
        if not path:
            return ("Choose your oTree project folder before launching (the folder that "
                    "holds settings.py).")
        if not os.path.isdir(path):
            return ('The project folder does not exist: "%s". Choose it again, or check '
                    "whether the drive is connected." % path)
        return ""

    def _hard_block_problems(self, cfg, lab_presets):
        """Launch-blocking reasons with no one-click fix (mirror of the Tk
        ``_hard_block_problems``). oTree-not-installed and the missing lab block
        are fail-soft warnings handled elsewhere, so they are NOT repeated here.
        """
        problems = []
        folder_problem = self._project_folder_problem(cfg)
        if folder_problem:
            problems.append(folder_problem)
        if cfg.get("lab") == core.LAB_CUSTOM and not (cfg.get("custom_host") or "").strip():
            problems.append(
                "Other host is selected but the address box is empty. Type an "
                "address, or choose a lab.")
        # Seats are NEVER a hard block (Julian): no seat file / no default seats
        # falls back to the open (none) room. The ONLY genuine seat block is a
        # chosen FILE that DOES exist but holds labels oTree would reject.
        mode = cfg.get("seat_mode")
        if mode == core.SEAT_FILE:
            seat_file = (cfg.get("seat_file") or "").strip()
            if seat_file and os.path.isfile(seat_file):
                try:
                    bad = core.invalid_seats(core.read_seat_file(seat_file))
                except OSError:
                    bad = []
                if bad:
                    problems.append(
                        "The chosen participant file has labels oTree would reject "
                        "(only letters, numbers and underscores are allowed): %s"
                        % ", ".join(bad[:6]))
        elif mode in (core.SEAT_DEFAULT, core.SEAT_EDIT):
            bad = core.invalid_seats(core.resolve_seats(cfg, lab_presets))
            if bad:
                problems.append(
                    "oTree only accepts letters, numbers and underscores in a "
                    "seat label. These would be rejected: %s" % ", ".join(bad[:6]))
        return problems

    def _project_needs_block(self, cfg):
        """True when a seat-using launch needs the lab support block but the
        project's settings.py does not have it yet (mirror of the Tk helper)."""
        if core.effective_seat_mode(cfg) == core.SEAT_NONE:
            return False
        path = (cfg.get("project_path") or "").strip()
        if not path or not os.path.isdir(path):
            return False
        state = core.inspect_settings(path)
        return bool(state.get("readable")) and not state.get("has_block")

    @api_call
    def launch(self, fields, force=False, config_name=""):
        """Validate, run the pre-launch preflight, then launch on a worker thread.

        Validation errors and preflight failures are RETURNED to the page (which
        shows them); nothing is pushed via evaluate_js from this JS-invoked
        method. When preflight finds problems and ``force`` is not set, the launch
        is NOT started: the failures come back so the page can show a "Launch
        anyway" / "Cancel" prompt (mirroring the Tk PreflightWarningDialog). This
        catches an unreachable DB / missing oTree / busy port / bad project up
        front, BEFORE anything runs and before any handoff banner. The actual
        work runs on a daemon thread, and only that thread streams the log and
        reports the REAL outcome via window.pywOnLaunchResult.
        """
        cfg = fields_to_config(fields)
        project = cfg.get("project_path", "").strip()
        level, message = core.validate_project(project)
        if level == "error":
            return {"ok": False, "message": message}

        if not force:
            results = core.preflight(cfg, core.load_lab_info())
            failures = core.preflight_failures(results)
            if failures:
                return {"ok": False, "preflight": [
                    {"check": f.get("check", ""), "message": f.get("message", ""),
                     "detail": f.get("detail", "")}
                    for f in failures]}

        # config_name is the SELECTED config's identity (the page's
        # currentConfigName). A server-ready launch stamps last_run on THAT
        # config, even if its room/fields were edited on-screen and never saved.
        self._spawn(lambda: self._run_launch(cfg, config_name), "launch")
        return {"ok": True, "launching": True}

    @api_call
    def reopen_dashboard(self):
        """Re-open the admin dashboard (the handoff 'No dashboard? Click here'
        link). Re-runs the real authenticated open with the last launch's params
        (fresh form-login + one-shot cookie relay), on a worker thread so this
        JS-invoked method returns at once and never re-enters the WebView loop.
        """
        last = getattr(self, "_last_launch", None)
        if not last:
            return {"ok": False, "message": "No dashboard to open yet."}

        def worker():
            result = core.open_dashboard_authenticated(
                core.AUTOLOGIN_HOST, last["port"], last["room_name"],
                last["admin_username"], last["admin_password"],
                auto_login=last["auto_login"])
            if result["method"] == "cookie":
                self._log("ok", "Re-opened the dashboard already logged in.")
            else:
                self._log("info", "Re-opened the dashboard login page. %s"
                          % result["reason"])

        self._spawn(worker, "reopen-dashboard")
        return {"ok": True}

    @api_call
    def close(self):
        """Close the launcher window (used by the hand-off takeover screen).

        ``destroy`` is marshalled onto a worker thread so this js_api call
        returns immediately rather than tearing the window down from inside the
        WebView message loop.
        """
        self._spawn(self._do_close, "close")
        return {"ok": True}

    def _do_close(self):
        try:
            self.window.destroy()
        except Exception:
            LOG.exception("window.destroy failed")

    @api_call
    def heartbeat(self, hidden=False):
        """Browser-mode liveness ping from the open page (sent every few seconds and
        immediately on every Page Visibility change).

        ``hidden`` carries the page's document.hidden state. The server's watchdog
        uses the beats to notice a closed tab, but is only a GENEROUS ~60s fallback
        and never fires while the tab reports itself hidden -- so a backgrounded but
        open tab is never falsely killed (item 8). A real close is caught promptly by
        the /api/quit sendBeacon instead. This is the ONLY process the launcher ever
        stops. No-op under the pywebview window (whose own close ends the process)."""
        beat = getattr(self.window, "heartbeat", None)
        if callable(beat):
            beat(bool(hidden))
        return {"ok": True}

    @api_call
    def quit(self):
        """Explicit, clean shutdown of the launcher's OWN web-UI server (the Quit
        button). Stops ONLY this Python process; it NEVER stops or kills any
        oTree/experiment process, not even one the launcher started. Under the
        pywebview window this closes the window instead (same effect)."""
        shutdown = getattr(self.window, "request_shutdown", None)
        if callable(shutdown):
            shutdown()
            return {"ok": True}
        # pywebview window path: close the native window (ends the process).
        self._spawn(self._do_close, "quit")
        return {"ok": True}

    def _run_launch(self, cfg, config_name=""):
        LOG.info("_run_launch: begin (resetdb=%s, open_browser=%s)",
                 cfg.get("resetdb"), cfg.get("open_browser"))
        try:
            project = cfg["project_path"]
            self._status("", "Launching…")
            label_file, note = core.prepare_label_file(cfg)
            self._log("muted", note)
            env = core.build_env(cfg, label_file=label_file)

            for key in core.launcher_env_keys(cfg, label_file):
                if key in env:
                    self._log("out", "%s = %s" % (key, core.describe_env_value(key, env[key])))

            if cfg.get("resetdb"):
                self._log("cmd", "$ otree resetdb   (answering \"y\" on stdin)")
                code = self._run_resetdb(project, env)
                if code != 0:
                    msg = ("otree resetdb failed (exit code %s). Nothing was started." % code)
                    self._status("err", msg)
                    self._log("err", "otree resetdb exited with code %s." % code)
                    # Drive the handoff banner with the REAL failure, so it never
                    # claims "dashboard opening" when nothing actually started.
                    self._launch_result(False, msg)
                    return
                self._log("ok", "otree resetdb finished with exit code 0.")

            launch = core.build_server_launch(cfg, project, env)
            self._log("info", "Starting the server: %s" % launch["description"])
            popen_kwargs = dict(
                cwd=launch["cwd"], env=env,
                creationflags=launch.get("creationflags", 0), shell=launch.get("shell", False),
            )
            if sys.platform.startswith("win"):
                # Start the server console MINIMISED and without stealing focus, so
                # it runs in the background and the oTree dashboard (opened next) is
                # what takes the screen.
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE
                popen_kwargs["startupinfo"] = startup
            server_start = time.monotonic()
            server_proc = subprocess.Popen(launch["cmd"], **popen_kwargs)
            self._log("ok", "otree prodserver started in its OWN terminal window (in the "
                            "background). Watch that window for live server logs and any errors.")

            # Auto login (default ON): open the admin room monitor already
            # authenticated via a REAL form-login + one-shot localhost cookie relay
            # (core.open_dashboard_authenticated). oTree ignores Basic Auth, so the
            # old credentials-in-URL trick was dead; this replays a real login and
            # plants the session cookie in the SYSTEM DEFAULT BROWSER. With auto
            # login off, or if the login fails, it opens the plain login page and
            # the operator logs in by hand. Opened on localhost (the operator's own
            # machine); the per-seat participant links keep the lab host. Mirror of
            # the Tk otree_lab_launcher launch.
            self._remember_launch(cfg)
            monitor_url = "http://%s:%s%s" % (
                core.AUTOLOGIN_HOST, cfg.get("port", "8000"),
                core.room_monitor_path(cfg.get("room_name", "")))
            result = {"ok": True, "method": "manual", "monitor_url": monitor_url,
                      "server_ready": False}
            if cfg.get("open_browser"):
                # Open the dashboard the instant the server responds. The readiness
                # poll lives in core.open_dashboard_authenticated (wait_for_server),
                # which now RETURNS whether the server actually came up.
                self._log("muted", "Waiting for the server to respond, then opening the dashboard …")
                result = core.open_dashboard_authenticated(
                    core.AUTOLOGIN_HOST, cfg.get("port", "8000"), cfg.get("room_name", ""),
                    cfg.get("admin_username", ""), cfg.get("admin_password", ""),
                    auto_login=cfg.get("auto_login", True),
                    on_wait=lambda s: self._log(
                        "muted", "Still waiting for the server to respond (%d s)…" % s))
                if result["method"] == "cookie":
                    self._log("ok", "Opened the oTree dashboard already logged in "
                                    "(auto-login: form-login + cookie relay).")
                else:
                    self._log("info", "Opened the oTree dashboard login page. %s Log in with "
                                      "the admin username and password shown here."
                                      % result["reason"])
            else:
                # No browser open: still confirm the server really came up before
                # reporting success, so a crash is not mis-reported as "Launched".
                self._log("muted", "Open in browser is off. Confirming the server is up …")
                result["server_ready"] = core.wait_for_server(
                    core.AUTOLOGIN_HOST, cfg.get("port", "8000"),
                    on_wait=lambda s: self._log(
                        "muted", "Still waiting for the server to respond (%d s)…" % s))

            if not result.get("server_ready"):
                # A startup crash / missing project / port race: prodserver did not
                # answer. Do NOT stamp last_run or claim "Launched" -- the real
                # traceback is in the server terminal window. Only the
                # linux-background child is our own pollable prodserver; on Windows
                # (detached console) and macOS (osascript, which exits 0 the moment
                # it has told Terminal to open) server_proc is NOT prodserver, so
                # polling it would falsely annotate "already exited with code 0".
                poll_proc = server_proc if launch.get("kind") == "linux-background" else None
                msg = self._startup_failure_message(poll_proc)
                self._status("err", msg)
                self._log("err", msg)
                # Fail-soft: a page may already be open (manual fallback), so keep
                # the url so the operator can retry, but report the honest failure.
                self._launch_result(False, msg, url=result.get("monitor_url", monitor_url),
                                    method=result.get("method", "manual"))
                self._record_session(cfg, config_name, "fail", None)
                return

            matched = self._mark_run(cfg, config_name)
            self._record_session(cfg, config_name, "ok", time.monotonic() - server_start)
            self._status("ok", "Launched: the oTree dashboard is opening in your browser. The "
                               "server runs in its own window; watch there for live logs. You "
                               "can close this launcher.")
            # Only a real, server-ready success reaches here: tell the handoff
            # banner, with the method so it reports honestly (already logged in vs
            # at the login page). Also push the REFRESHED config rows + selected so
            # the sidebar shows the just-launched config's new run time and its
            # updated recency slot LIVE, without a relaunch. Only here (a ready
            # success) -- a failed/not-ready launch below never restamps/refreshes.
            configs, selected = self._launched_config_state(matched)
            self._launch_result(True, "", url=result["monitor_url"],
                                method=result["method"],
                                configs=configs, selected=selected)
        except Exception:
            LOG.exception("_run_launch: EXCEPTION")
            self._log("err", traceback.format_exc())
            self._status("err", "Launch failed. See the activity log.")
            self._launch_result(False, "Launch failed. See the activity log.")
        finally:
            LOG.info("_run_launch: end")

    def _launch_result(self, ok, message, url="", method="manual",
                       configs=None, selected=None):
        """Deliver the REAL launch outcome to the page's handoff banner.

        Worker-thread only (it uses evaluate_js via _callback). ``ok`` True means
        the server actually started; ``url`` is then the open-dashboard link and
        ``method`` is "cookie" (opened already logged in) or "manual" (opened the
        login page). On failure ``message`` explains what went wrong; there is NO
        false success.
        """
        payload = {"ok": bool(ok), "message": message, "url": url,
                   "method": method}
        # On a READY success the caller passes the refreshed rows so the page can
        # repaint the sidebar live (Bug E). Absent on failure -> the JS leaves the
        # config list untouched (no restamp/reorder on a failed launch).
        if configs is not None:
            payload["configs"] = configs
            payload["selected"] = selected or ""
        self._callback("pywOnLaunchResult", payload)

    def _remember_launch(self, cfg):
        """Stash the params the takeover 'Click here' needs to re-open the
        dashboard (the cookie relay is one-shot, so a fresh form-login is done
        each time)."""
        self._last_launch = {
            "port": cfg.get("port", "8000"),
            "room_name": cfg.get("room_name", ""),
            "admin_username": cfg.get("admin_username", ""),
            "admin_password": cfg.get("admin_password", ""),
            "auto_login": cfg.get("auto_login", True),
        }

    def _run_resetdb(self, project, env):
        # CREATE_NO_WINDOW so the resetdb child never flashes its own console when
        # this launcher runs windowless under pythonw (the browser-mode .vbs). Its
        # stdio is fully piped here, so no console is needed. No-op off Windows.
        proc = subprocess.Popen(
            core.resetdb_command(), cwd=project, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=core._no_window_flags(),
        )
        try:
            proc.stdin.write("y\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            if line:
                self._log("out", line)
        proc.stdout.close()
        return proc.wait()

    def _startup_failure_message(self, server_proc=None):
        """The message shown when prodserver never became ready.

        Points the operator to the server terminal window (which holds the real
        traceback) and, when the child has already exited (easy to see on the
        Linux background path), surfaces its exit code.
        """
        base = ("The oTree server did not become ready — it may have crashed on "
                "startup (a missing project, a database error, or the port already "
                "in use). Nothing was marked as launched. Check the server terminal "
                "window for the real error.")
        try:
            if server_proc is not None:
                code = server_proc.poll()
                if code is not None:
                    base += " (The server process already exited with code %s.)" % code
        except Exception:
            pass
        return base

    def _mark_run(self, cfg, config_name=""):
        """Stamp the just-launched saved config as run now, and return it.

        Prefers the SELECTED config's identity (``config_name``, the page's
        currentConfigName): a launch stamps that config even if its room (or any
        field) was edited on-screen and never saved back to the preset, so the
        sidebar no longer shows "never run" after a "(lab default)" refresh. Only
        when no selected identity is given (e.g. a headless one-click run) does it
        fall back to matching by config content.

        Runs on the launch WORKER thread, so it goes through the store lock: a
        UI-thread "save as new" cannot interleave with this stamp-and-save and
        clobber either change.
        """
        matched = {"preset": None}

        def _apply():
            preset = self._find(config_name) if config_name else None
            if preset is None:
                for candidate in self.presets:
                    if not core.configs_differ(candidate, cfg):
                        preset = candidate
                        break
            if preset is not None:
                matched["preset"] = preset
                # The built-in Lab default is a launch TEMPLATE you launch FROM,
                # never a saved config, so it must NEVER record a run (Job 2).
                # Keep it as the matched/selected config, but do not stamp it.
                if not core.is_builtin(preset):
                    preset["last_run"] = core.now_iso()

        try:
            self._mutate_store(_apply)
        except OSError:
            pass
        return matched["preset"]

    def _record_session(self, cfg, config_name, outcome, ready_seconds):
        """Append ONE launch line to data/sessions.jsonl (fable review F).

        Fail-soft in core, so it can never break a launch; logged alongside the
        _mark_run last_run stamp so every launch (success or ready-failure) is
        recorded. The author is the selected config's saved author when known.
        Mirror of the Tk LauncherApp._record_session.
        """
        preset = self._find(config_name) if config_name else None
        author = preset.get("author", "") if preset else ""
        core.record_session(
            cfg, config_name=config_name, author=author, outcome=outcome,
            server_ready_seconds=ready_seconds,
            lab_presets=core.lab_presets_from_store(self.store_extra))

    def _launched_config_state(self, matched):
        """Refreshed config rows + selected name for a live post-launch repaint.

        Re-derives the display order (built-in default pinned, then most-recently-
        launched first) so the just-stamped config jumps to its recency slot, and
        selects the config that was launched (the freshly stamped one) so the
        sidebar highlights it. READY-launch only -- the caller does not build this
        for a failed/not-ready launch, so a failure never restamps or reorders.
        """
        ordered = core.order_presets_for_display(self.presets)
        rows = [preset_row(p) for p in ordered]
        selected = ""
        if matched is not None:
            selected = matched.get("name", "")
        elif ordered:
            chosen = core.select_on_open(self.presets)
            selected = chosen.get("name", "") if chosen else ""
        return rows, selected


# ---------------------------------------------------------------------------
# Browser mode: the SAME Api served from a tiny stdlib HTTP server and opened in
# the default browser, so the web launcher runs on ANY Python (3.13 / 3.14
# included) with ZERO third-party dependencies -- no pywebview, no pythonnet.
#
# The page talks to Python two ways, exactly mirroring the pywebview bridge:
#   * REQUEST/RESPONSE:  POST /api/<method> with a JSON array of positional args
#     dispatches to the SAME Api object the pywebview js_api uses and returns the
#     method's JSON result. (The page's transport shim posts here when
#     window.pywebview is absent.)
#   * PUSH (worker -> page):  the Api's worker threads already deliver log lines,
#     status and launch/handoff results by calling ``self.window.evaluate_js(...)``.
#     In browser mode ``self.window`` is a BrowserBridge whose ``evaluate_js``
#     queues the snippet; a Server-Sent-Events stream (GET /events) hands each
#     snippet to the page, which evals it. So the entire existing launch /
#     resetdb streaming machinery works UNCHANGED -- only the transport differs.
#
# Only the Python standard library is used here (http.server, socketserver via
# ThreadingHTTPServer, threading, queue, json, webbrowser) plus otree_core.
# ---------------------------------------------------------------------------

WEB_DIR = os.path.join(HERE, "web")

# The browser-mode marker: injected as the very first thing inside <head> of the
# served index.html by the --browser HTTP server ONLY. It lets the page KNOW,
# deterministically and synchronously (before first paint), that it is running as
# the plain-browser launcher -- where there is no native folder picker -- so it
# hides Browse and shows the paste-the-path field. The pywebview native path
# (create_window(INDEX_HTML)) and a plain file:// open never inject it, so there
# Browse stays visible. This replaces the old, fragile (http/https + no
# window.pywebview.api) heuristic, which misfired on macOS pywebview (it serves
# the page over its own internal http server and injects window.pywebview.api
# only asynchronously, after first paint).
BROWSER_MODE_MARKER = "<script>window.__LAUNCHER_BROWSER_MODE__=true;</script>"


def _browser_mode_marker(token="", theme=""):
    """The <head> script the --browser server injects: it flags the page as the
    plain-browser launcher, hands it the per-run CSRF-style token the page must
    echo back on every POST /api (see the origin/token check in the handler), and
    hands it the persisted light/dark theme so the head script can apply it BEFORE
    first paint (no flash; browser-mode localStorage does not survive a new port,
    so the theme must come from the server)."""
    parts = ("window.__LAUNCHER_BROWSER_MODE__=true;"
             "window.__LAUNCHER_TOKEN__=%s;" % json.dumps(str(token or "")))
    if theme:
        parts += "window.__LAUNCHER_THEME__=%s;" % json.dumps(str(theme))
    return "<script>%s</script>" % parts


def _index_html_browser_mode(token=""):
    """Read index.html and inject the browser-mode marker right after <head>.

    Placed immediately after the opening ``<head>`` tag, BEFORE the page's own
    detection script, so ``window.__LAUNCHER_BROWSER_MODE__`` (and the per-run
    ``window.__LAUNCHER_TOKEN__``) are set synchronously before the detection
    script runs and before first paint (no Browse-button flicker). Only the
    --browser server calls this.
    """
    with open(INDEX_HTML, "r", encoding="utf-8") as fh:
        html = fh.read()
    try:
        theme = core.load_ui_theme()
    except Exception:
        theme = ""
    idx = html.find("<head>")
    if idx != -1:
        insert_at = idx + len("<head>")
        html = html[:insert_at] + "\n" + _browser_mode_marker(token, theme) + html[insert_at:]
    return html


class BrowserBridge(object):
    """Stand-in for a pywebview ``window`` on the browser path.

    The Api's worker threads call ``self.window.evaluate_js(snippet)`` to push
    log/status/handoff updates into the page. Here that snippet is fanned out to
    every connected Server-Sent-Events subscriber (usually one: the open page),
    which evaluates it -- the same effect pywebview's evaluate_js has, over HTTP.
    """

    def __init__(self):
        import queue  # noqa: F401  (imported lazily; stdlib only)
        self._queue_cls = queue.Queue
        self._subscribers = []
        self._lock = threading.Lock()
        # Clean self-shutdown driven by the page HEARTBEAT (browser mode). The open
        # page POSTs /api/heartbeat every few seconds; when the beats stop for
        # _hb_timeout seconds -- because the tab was closed -- the server shuts
        # ITSELF down (clean flush then exit). This is the ONLY process the launcher
        # ever stops; it never touches any oTree/experiment process. The Quit button
        # takes the same path via request_shutdown. Armed by enable_self_shutdown.
        self._shutdown = None
        self._hb_timeout = HEARTBEAT_TIMEOUT
        self._hb_interval = 3.0
        self._hb_last = None
        self._hb_seen = False
        # The tab's last-reported Page Visibility state. While True the watchdog
        # does NOT fire from missed beats (a hidden tab is expected to be throttled).
        self._hb_hidden = False
        self._shutting_down = False

    def enable_self_shutdown(self, shutdown, heartbeat_timeout=HEARTBEAT_TIMEOUT,
                             check_interval=HEARTBEAT_CHECK_INTERVAL):
        """Arm the heartbeat watchdog: after the page's heartbeats stop for
        ``heartbeat_timeout`` seconds, call ``shutdown`` (httpd.shutdown) exactly
        once. Also the target of the Quit button (request_shutdown). Browser mode
        only; the pywebview window ends the process by its own close."""
        self._shutdown = shutdown
        self._hb_timeout = float(heartbeat_timeout)
        self._hb_interval = float(check_interval)
        threading.Thread(target=self._heartbeat_watchdog,
                         name="heartbeat-watchdog", daemon=True).start()

    def heartbeat(self, hidden=False):
        """Record a liveness beat from the open page (Api.heartbeat calls this).

        ``hidden`` is the page's current document.hidden state; it is remembered so
        the watchdog can suppress a shutdown while the tab is a throttled background
        tab (see _heartbeat_watchdog)."""
        with self._lock:
            self._hb_last = time.monotonic()
            self._hb_seen = True
            self._hb_hidden = bool(hidden)

    def request_shutdown(self):
        """Shut the launcher's own server down now (the Quit button / hand-off)."""
        self._trigger_shutdown("quit requested")

    def _heartbeat_watchdog(self):
        # Poll for a heartbeat gap. Only AFTER the first beat (a page actually
        # connected) does a gap count, so a slow browser open never trips it; once
        # the beats stop for longer than _hb_timeout the tab is gone -> shut down.
        #
        # A HIDDEN tab is never killed by this watchdog (item 8): the browser
        # throttles a background tab's timers, so its beats legitimately slow down or
        # pause. While the last-reported state is hidden we simply skip the gap check;
        # a real close still fires the /api/quit sendBeacon promptly. The tab tells us
        # the moment it is shown again (an un-throttled visibilitychange beat), which
        # resets the countdown.
        while True:
            time.sleep(self._hb_interval)
            with self._lock:
                if self._shutting_down or self._shutdown is None:
                    return
                seen = self._hb_seen
                last = self._hb_last
                hidden = self._hb_hidden
            if hidden:
                continue
            if seen and last is not None and (time.monotonic() - last) > self._hb_timeout:
                self._trigger_shutdown("heartbeat lost (browser tab closed)")
                return

    def _trigger_shutdown(self, reason):
        with self._lock:
            if self._shutting_down or self._shutdown is None:
                return
            self._shutting_down = True
            shutdown = self._shutdown
        LOG.info("browser mode: %s; shutting down the launcher's OWN server", reason)
        # shutdown() (httpd.shutdown) must not run on a serving thread; give it its
        # own. serve_forever then returns and run_browser's finally cleans up.
        threading.Thread(target=shutdown, name="server-shutdown", daemon=True).start()

    def subscribe(self):
        q = self._queue_cls()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def evaluate_js(self, snippet):
        """Fan a JS snippet out to every connected page (worker threads only)."""
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            q.put(snippet)

    def destroy(self):
        """The hand-off 'close the launcher' button. A browser tab cannot be
        force-closed by script the way a native window can, so we ask the page to
        try window.close(); then we shut the launcher's OWN server down cleanly
        (this stops only this Python process, never any oTree/experiment process)."""
        self.evaluate_js("window.close && window.close()")
        self.request_shutdown()


def _make_browser_handler(api, bridge, token="", allowed_origins=()):
    """Build the request handler class bound to this Api + bridge.

    ``token`` is the per-run secret the served page echoes back as the
    ``X-Launcher-Token`` header on every POST /api; ``allowed_origins`` is the set
    of ``Origin`` values a POST may carry (the server's own loopback origins).
    Together they reject a cross-site request another page in the same browser
    could otherwise fire at the local API (see do_POST); the genuine page always
    passes because it is same-origin and carries the injected token.
    """
    from http.server import BaseHTTPRequestHandler

    # Kept as a live reference (not copied) so run_browser can add the server's
    # real loopback origins to it AFTER the free port is known.

    class Handler(BaseHTTPRequestHandler):
        # Quiet the default one-line-per-request stderr spam; route to our log.
        def log_message(self, fmt, *args):
            LOG.debug("http: " + fmt, *args)

        def _send_json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, OSError):
                pass

        def _send_file(self, path):
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError:
                self.send_error(404, "Not found")
                return
            ctype = "text/html; charset=utf-8" if path.endswith(".html") \
                else "text/plain; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, OSError):
                pass

        def _send_index(self):
            # Serve index.html WITH the browser-mode marker injected, so the page
            # deterministically knows it is the plain-browser launcher (Browse
            # hidden). Only this --browser server does this injection.
            try:
                body = _index_html_browser_mode(token).encode("utf-8")
            except OSError:
                self.send_error(404, "Not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, OSError):
                pass

        def _serve_static(self):
            # "/" -> index.html; otherwise a file under web/, path-traversal safe.
            rel = self.path.split("?", 1)[0].lstrip("/")
            if rel in ("", "index.html"):
                self._send_index()
                return
            target = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not target.startswith(os.path.abspath(WEB_DIR) + os.sep):
                self.send_error(403, "Forbidden")
                return
            if os.path.isfile(target):
                self._send_file(target)
            else:
                self.send_error(404, "Not found")

        def _serve_events(self):
            # A long-lived Server-Sent-Events stream: each queued JS snippet is
            # delivered as one `data:` event for the page to eval. json.dumps in
            # the Api escapes newlines, so every snippet is a single SSE line.
            import queue as _queue
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = bridge.subscribe()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        snippet = q.get(timeout=15)
                    except _queue.Empty:
                        self.wfile.write(b": ping\n\n")  # heartbeat
                        self.wfile.flush()
                        continue
                    self.wfile.write(("data: %s\n\n" % snippet).encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                bridge.unsubscribe(q)

        # Icon paths the browser auto-requests but we do not ship. Answer them with
        # an empty 204 so they stop logging "code 404, message Not found" (item 11).
        _ICON_PATHS = frozenset((
            "/favicon.ico", "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png"))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/events":
                self._serve_events()
            elif path in self._ICON_PATHS:
                # No content: quiet the harmless favicon / touch-icon probes.
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._serve_static()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/"):
                self.send_error(404, "Not found")
                return
            # Reject a cross-site request another page in the same browser could
            # fire at this local API (its side effect would run even though the
            # response is unreadable). Three cheap, independent checks, ALL of which
            # the genuine same-origin page passes:
            #   * Content-Type application/json -- a simple cross-site POST can only
            #     set text/plain, form or multipart without triggering a CORS
            #     preflight this server never answers;
            #   * Origin (when the browser sends one) must be one of our own
            #     loopback origins -- a foreign page's Origin never matches;
            #   * X-Launcher-Token must equal the per-run secret injected into the
            #     served page -- a custom header also forces a preflight, so a
            #     cross-origin caller cannot even send it.
            # The pywebview js_api path is unaffected (it never reaches this server).
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._send_json({"ok": False, "error": True,
                                 "message": "Bad content type."}, code=403)
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                self._send_json({"ok": False, "error": True,
                                 "message": "Bad origin."}, code=403)
                return
            # The genuine page sends the per-run token in the X-Launcher-Token
            # header. A page-close beacon (navigator.sendBeacon -> /api/quit) cannot
            # set custom headers, so the token is ALSO accepted from a ?token= query
            # parameter; the Origin check above still rejects a cross-site beacon.
            supplied = self.headers.get("X-Launcher-Token")
            if supplied is None:
                from urllib.parse import urlparse, parse_qs
                supplied = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            if supplied != token:
                self._send_json({"ok": False, "error": True,
                                 "message": "Bad or missing launcher token."}, code=403)
                return
            method = path[len("/api/"):]
            # Only public, callable Api methods are reachable -- never a private
            # helper or a data attribute.
            fn = getattr(api, method, None)
            if method.startswith("_") or not callable(fn):
                self._send_json({"ok": False, "error": True,
                                 "message": "Unknown method %r." % method}, code=404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                args = json.loads(raw.decode("utf-8")) if raw else []
            except ValueError:
                args = []
            if not isinstance(args, list):
                args = [args]
            try:
                result = fn(*args)
            except Exception:
                LOG.exception("api dispatch %s failed", method)
                result = dict(_SAFE_ERROR)
            self._send_json(result)

    return Handler


def _detached_popen_kwargs():
    """Popen kwargs that fully DETACH a child so it outlives this process.

    On Windows: DETACHED_PROCESS + a new process group (no shared console). On
    POSIX: a new session (start_new_session). Used only to relaunch the LAUNCHER
    itself after an update; never for an oTree/experiment process."""
    if sys.platform.startswith("win"):
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def build_relaunch_command(browser_mode, port=0, python=None, script=None):
    """Build the argv that re-execs THIS launcher for an in-place relaunch.

    Re-execs the CURRENT Python directly rather than the platform double-click
    script, because those scripts hardcode browser mode + a random free port,
    which would (a) orphan the open browser tab on a dead port and (b) silently
    downgrade a native-window session to a browser tab. Instead:

      * browser mode  -> ``--browser --port <current> --no-open``: come back on the
        SAME port with NO new tab, so the one open tab can reload onto it;
      * native window -> ``--window``: come back as a native window.

    The running process is already the launcher's own (venv) Python, so
    ``sys.executable`` is the right interpreter to re-exec.
    """
    python = python or sys.executable
    script = script or os.path.join(core.app_dir(), "otree_launcher_web.py")
    if browser_mode:
        return [python, script, "--browser", "--port", str(int(port)), "--no-open"]
    return [python, script, "--window"]


def spawn_new_launcher(browser_mode=True, port=0):
    """Start a FRESH, DETACHED launcher instance re-execing THIS launcher with the
    current Python, so an in-place update comes back running the just-pulled code
    IN THE SAME MODE the operator was using (browser tab reused on the same port,
    or a native window stays native). See :func:`build_relaunch_command`.

    Returns the spawned Popen. Raises on failure (the caller logs and skips the
    shutdown so the current launcher stays up). This ONLY ever starts another
    launcher; it never touches any oTree/experiment process.
    """
    kwargs = _detached_popen_kwargs()
    argv = build_relaunch_command(browser_mode, port)
    LOG.info("relaunch: spawning %s", argv)
    return subprocess.Popen(argv, **kwargs)


def run_browser(host="127.0.0.1", port=0, open_browser=True):
    """Serve the web UI over a local HTTP server and open it in the browser.

    Binds to 127.0.0.1 only (never a public interface). ``port=0`` picks a free
    port. Returns 0 when the server stops. Uses only the standard library.
    """
    from http.server import ThreadingHTTPServer

    if not os.path.isfile(INDEX_HTML):
        LOG.error("UI not found at %s", INDEX_HTML)
        sys.stderr.write("Cannot find the UI at %s\n" % INDEX_HTML)
        return 2

    import secrets

    api = Api()
    bridge = BrowserBridge()
    api.window = bridge          # worker-thread pushes go through the SSE bridge
    api.browser_mode = True      # dialog methods fall back to paste-the-path

    # A per-run secret the served page echoes back on every POST /api, plus the
    # set of loopback origins a POST may carry -- filled in after the port is known.
    token = secrets.token_urlsafe(32)
    allowed_origins = set()

    class _Server(ThreadingHTTPServer):
        daemon_threads = True    # SSE threads never block shutdown
        allow_reuse_address = True

    # Robust bind: right after an auto-restart the just-quit instance may still be
    # releasing a FIXED --port for a moment, so retry a few times, then fall back to
    # an OS-chosen free port rather than fail to come up. (port=0 never collides.)
    handler = _make_browser_handler(api, bridge, token, allowed_origins)
    httpd = None
    for attempt in range(6):
        try:
            httpd = _Server((host, port), handler)
            break
        except OSError as error:
            LOG.warning("port %s busy (attempt %d/6): %s", port, attempt + 1, error)
            time.sleep(0.3)
    if httpd is None:
        LOG.warning("requested port %s stayed busy; taking an OS-chosen free port", port)
        httpd = _Server((host, 0), handler)
    actual_port = httpd.server_address[1]
    # The self-update relaunch re-execs on THIS port (browser tab reuse), so the
    # Api needs to know it. (browser_mode was set above.)
    api.current_port = actual_port
    for origin_host in (host, "127.0.0.1", "localhost"):
        allowed_origins.add("http://%s:%d" % (origin_host, actual_port))
    # Closing the browser tab must actually END this Python process. A clean close
    # fires an instant /api/quit beacon (the primary signal); the heartbeat watchdog
    # is only a generous ~60s fallback for a crash / sleep / lost network, and never
    # fires while the tab reports itself hidden, so a backgrounded but open tab is
    # never falsely killed (item 8). The Quit button takes the same path. This is the
    # ONLY process the launcher ever stops -- it never touches any oTree process.
    bridge.enable_self_shutdown(httpd.shutdown, heartbeat_timeout=HEARTBEAT_TIMEOUT,
                                check_interval=HEARTBEAT_CHECK_INTERVAL)
    url = "http://%s:%d/" % (host, actual_port)
    LOG.info("browser mode serving at %s", url)
    sys.stderr.write("\noTree Lab Launcher (browser mode) is serving at:\n  %s\n" % url)
    sys.stderr.write("Your browser should open automatically. Leave THIS window "
                     "open while you work; closing it stops the server.\n\n")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            LOG.exception("webbrowser.open failed (open %s manually)", url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("browser mode: interrupted, shutting down")
    finally:
        httpd.server_close()
        # Release the launcher's OWN log file handle so the data/ folder is not
        # left locked (Windows) and can be deleted once the launcher is closed.
        _close_logging()
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # BROWSER MODE IS THE DEFAULT ON EVERY PLATFORM (v1.1.0). The launcher serves
    # its UI over a tiny stdlib HTTP server and opens it in the operator's DEFAULT
    # BROWSER, so it runs on ANY Python with ZERO third-party dependencies -- no
    # pywebview, no pythonnet, no pyobjc (the heavy, fragile deps that fail to
    # build on macOS Python 3.12). Flags:
    #   --window / --pywebview : OPT IN to the native pywebview desktop window
    #                            instead (only if pywebview is installed; it is
    #                            NOT needed for the normal path and the launcher
    #                            falls back to browser mode if it is missing).
    #   --browser              : force browser mode. Now the default, so this is
    #                            redundant; kept so existing shortcuts/scripts
    #                            (e.g. the Windows .vbs) that pass it still work.
    #   --port N               : bind the browser-mode server to a fixed port
    #                            (0 = free port).
    #   --no-open              : do not auto-open the browser (headless self-test).
    want_window = ("--window" in argv) or ("--pywebview" in argv)
    no_open = "--no-open" in argv
    # Browser mode unless the operator explicitly opts into the pywebview window.
    browser_mode = not want_window
    port = 0
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (ValueError, IndexError):
            port = 0

    LOG.info("=" * 60)
    LOG.info("startup: otree_launcher_web on %s, python %s (browser_mode=%s)",
             sys.platform, sys.version.split()[0], browser_mode)
    LOG.info("log file: %s", LOG_PATH)
    core.reload_lab_info()

    if want_window:
        try:
            import webview  # noqa: F401
        except ImportError:
            # Opted into the window but pywebview is not installed -> fall back to
            # browser mode instead of failing. Browser mode needs NOTHING beyond
            # the standard library.
            LOG.warning("pywebview not importable; falling back to browser mode")
            sys.stderr.write(
                "pywebview is not available, so starting in BROWSER mode "
                "(no pywebview / pythonnet needed).\n")
            browser_mode = True

    if browser_mode:
        return run_browser(port=port, open_browser=not no_open)

    # Opt-in pywebview window path: reached only when --window/--pywebview was
    # passed AND the import above succeeded (otherwise browser_mode is True and we
    # returned via run_browser).
    import webview
    LOG.info("pywebview %s", getattr(webview, "__version__", "?"))

    if not os.path.isfile(INDEX_HTML):
        LOG.error("UI not found at %s", INDEX_HTML)
        sys.stderr.write("Cannot find the UI at %s\n" % INDEX_HTML)
        return 2

    api = Api()
    window = webview.create_window(
        core.APP_NAME, INDEX_HTML, js_api=api,
        width=1120, height=820, min_size=(960, 680),
        background_color="#1b1b1c",
    )
    api.window = window
    # devtools (right-click -> Inspect) let JS run against the privileged js_api
    # (e.g. append_settings_block), so they are OFF by default in the shipped
    # launcher and only enabled by an explicit opt-in env var for debugging.
    debug = webview_debug_enabled()
    if debug:
        LOG.warning("WebView devtools ENABLED via %s -- the js_api is reachable "
                    "from the browser Console. Do not use on a shared machine.",
                    DEBUG_ENV_VAR)
    LOG.info("webview.start(debug=%s)", debug)
    try:
        webview.start(debug=debug)
    except Exception:
        LOG.exception("webview.start crashed")
        raise
    LOG.info("webview.start returned; exiting")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort startup diagnostics
        _log_startup_crash(exc)
        raise
