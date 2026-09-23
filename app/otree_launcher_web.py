#!/usr/bin/env python3
"""oTree Lab Launcher: web-tech front end (pywebview host).

This is the re-skin of ``otree_lab_launcher.py``. The window and every button
are HTML/CSS/JS (``web/index.html``), but the real work (picking folders,
validating the project, saving configs, resetting the database and starting
``otree prodserver``) is done here in Python, so the app keeps the full
filesystem and process powers a browser tab can never have.

    Web tech for the looks, a native Python process for the powers.

On Windows 10/11 pywebview renders through the built-in Edge WebView2 runtime,
so there is no Chromium bundle and no Node build. Run it with::

    pip install pywebview
    python otree_launcher_web.py

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
    # Also echo to stderr, so a visible terminal shows the same lines.
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


LOG = _setup_logging()

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
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        LOG.info("Api.%s: enter", fn.__name__)
        try:
            result = fn(self, *args, **kwargs)
            LOG.info("Api.%s: exit ok", fn.__name__)
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
    return {"level": level, "message": message, "name": name, "apps": apps}


def _lab_rows(presets):
    """Lab-settings table rows (every lab, shown and hidden), in stored order."""
    return [{"id": p["id"], "name": p["name"], "ip": p["ip"],
             "seats": p["seats"], "display": p["display"],
             "default_room": p["default_room"], "shortcut_label": p["shortcut_label"]}
            for p in presets]


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
            result = mutate()
            core.save_store(self.presets, self.store_extra, self.store_path)
            return result

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

    def _lab_identity_response(self, lab):
        """The shared payload for set_/change_lab_marker: refreshed config rows,
        the selected config's fields, AND the refreshed lab-settings rows + the
        per-config selector tiles (now collapsed to ``lab``) so the page can
        repaint the main selector and the Shown column live (BUG A)."""
        presets = self._apply_lab_identity(lab)
        selected = self.presets[0] if self.presets else None
        fields = self._config_fields(selected) if selected else dict(core.DEFAULT_CONFIG)
        preset = (core.find_lab_preset(lab, presets)
                  or core.find_lab_preset(lab, core.default_lab_presets()))
        return {"ok": True, "lab_marker": lab,
                "lab_name": preset.get("name") if preset else lab,
                "configs": [preset_row(p) for p in self.presets],
                "selected": selected.get("name") if selected else "",
                "fields": fields,
                "lab_presets": _lab_rows(presets),
                "labs": _lab_tiles(presets, fields.get("lab"))}

    @api_call
    def set_lab_marker(self, lab):
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
        # then hand the page refreshed rows/tiles to repaint (BUG A).
        return self._lab_identity_response(lab)

    @api_call
    def change_lab_marker(self, lab):
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
        # refreshed rows/tiles so the selector shifts live (BUG A).
        return self._lab_identity_response(lab)

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
    def create_database(self, new_db, new_user="", new_password="", researcher=""):
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
        """
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
        """Delete a lab preset from the Lab Settings page (web/Tk parity).

        Refuses to remove the last displayed lab (core.delete_lab_preset), so the
        selector always keeps a lab. Returns the next lab to select when the
        deleted one was the current selection. ``config_lab`` keeps the open
        config's own (possibly hidden) lab in the returned tiles when it still
        exists (BUG B, Tk parity)."""
        presets = core.lab_presets_from_store(self.store_extra)
        ok, message, presets, next_selected = core.delete_lab_preset(
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
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError as error:
            self._callback("pywOnBatResult",
                           {"ok": False, "message": "Could not write %s: %s" % (path, error)})
            return
        # A macOS/Unix shell shortcut (.command/.sh, or a shebang script) must be
        # executable or the OS refuses to run it ("no appropriate access
        # privileges"). Windows .vbs/.bat/.txt need no exec bit -- leave them.
        lower = path.lower()
        if lower.endswith(".command") or lower.endswith(".sh") or text.startswith("#!"):
            try:
                os.chmod(path, os.stat(path).st_mode | 0o111)
            except OSError:
                LOG.exception("could not set executable bit on %s", path)
        self._callback("pywOnBatResult", {"ok": True, "path": path})

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
                    auto_login=cfg.get("auto_login", True))
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
                    core.AUTOLOGIN_HOST, cfg.get("port", "8000"))

            if not result.get("server_ready"):
                # A startup crash / missing project / port race: prodserver did not
                # answer. Do NOT stamp last_run or claim "Launched" -- the real
                # traceback is in the server terminal window.
                msg = self._startup_failure_message(server_proc)
                self._status("err", msg)
                self._log("err", msg)
                # Fail-soft: a page may already be open (manual fallback), so keep
                # the url so the operator can retry, but report the honest failure.
                self._launch_result(False, msg, url=result.get("monitor_url", monitor_url),
                                    method=result.get("method", "manual"))
                return

            matched = self._mark_run(cfg, config_name)
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
        try window.close() and otherwise the operator just closes the tab; the
        server keeps running until its console window is closed."""
        self.evaluate_js("window.close && window.close()")


def _make_browser_handler(api, bridge):
    """Build the request handler class bound to this Api + bridge."""
    from http.server import BaseHTTPRequestHandler

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

        def _serve_static(self):
            # "/" -> index.html; otherwise a file under web/, path-traversal safe.
            rel = self.path.split("?", 1)[0].lstrip("/")
            if rel in ("", "index.html"):
                self._send_file(INDEX_HTML)
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

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/events":
                self._serve_events()
            else:
                self._serve_static()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/"):
                self.send_error(404, "Not found")
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

    api = Api()
    bridge = BrowserBridge()
    api.window = bridge          # worker-thread pushes go through the SSE bridge
    api.browser_mode = True      # dialog methods fall back to paste-the-path

    class _Server(ThreadingHTTPServer):
        daemon_threads = True    # SSE threads never block shutdown
        allow_reuse_address = True

    httpd = _Server((host, port), _make_browser_handler(api, bridge))
    actual_port = httpd.server_address[1]
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
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # --browser  : force the stdlib HTTP + default-browser mode (no pywebview).
    # --port N   : bind the browser-mode server to a fixed port (0 = free port).
    # --no-open  : do not auto-open the browser (used by the headless self-test).
    browser_mode = "--browser" in argv
    no_open = "--no-open" in argv
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

    if not browser_mode:
        try:
            import webview
        except ImportError:
            # No pywebview (e.g. Python 3.13/3.14 on Windows, where pythonnet has
            # no wheel) -> fall back to browser mode automatically instead of
            # failing. Browser mode needs NOTHING beyond the standard library.
            LOG.warning("pywebview not importable; falling back to browser mode")
            sys.stderr.write(
                "pywebview is not available, so starting in BROWSER mode "
                "(no pywebview / pythonnet needed).\n")
            browser_mode = True

    if browser_mode:
        return run_browser(port=port, open_browser=not no_open)

    import webview  # already importable (checked above)
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
