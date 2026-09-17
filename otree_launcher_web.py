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


def _install_crash_log():
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        stream = open(CRASH_LOG_PATH, "a", buffering=1, encoding="utf-8")
    except OSError:
        return
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
             "seats": p["seats"], "display": p["display"]} for p in presets]


def _lab_tiles(presets):
    """The lab selector tiles: only the DISPLAYED presets, with their seat map."""
    return [{"id": p["id"], "name": p["name"], "host": p["ip"],
             "seats": p["seats"], "map": p["map"]}
            for p in core.displayed_lab_presets(presets)]


def preset_row(preset):
    cfg = core.normalize_config(preset)
    project_path = (cfg.get("project_path") or "").strip()
    folder = os.path.basename(os.path.normpath(project_path)) if project_path else ""
    return {
        "name": preset.get("name", ""),
        "when": core.format_last_run(preset.get("last_run")),
        "author": preset.get("author", ""),
        "builtin": core.is_builtin(preset),
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
        self.presets, self.store_extra = core.load_store(self.store_path)
        # This machine's lab identity (lab.local) configures the built-in
        # default's lab. Applied in memory to the built-in only; user configs
        # are untouched. A no-op on first launch (marker unset).
        core.apply_lab_marker(self.presets)
        self.presets = core.sort_presets(self.presets)
        if not os.path.exists(self.store_path):
            try:
                core.save_store(self.presets, self.store_extra, self.store_path)
            except OSError:
                pass
        self.window = None  # set by main() once the window exists

    # -- helpers -----------------------------------------------------------

    def _find(self, name):
        for preset in self.presets:
            if preset.get("name") == name:
                return preset
        return None

    def _config_fields(self, preset):
        cfg = core.normalize_config(preset)
        return {key: cfg[key] for key in core.FIELD_KEYS}

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
        rows = [preset_row(p) for p in self.presets]
        selected = self.presets[0] if self.presets else None
        fields = self._config_fields(selected) if selected else dict(core.DEFAULT_CONFIG)
        # The labs the selector should offer, seeded from lab_info.json and
        # narrowed to the displayed ones. The JS builds its lab buttons and seat
        # maps from this list; no lab data is hardcoded in the page.
        all_presets = core.lab_presets_from_store(self.store_extra)
        labs = [
            {"id": p["id"], "name": p["name"], "host": p["ip"],
             "seats": p["seats"], "map": p["map"]}
            for p in core.displayed_lab_presets(all_presets)
        ]
        # The full preset list (incl. hidden ones) + the Postgres admin config
        # feed the Lab Settings page. Both live in the store's extra, read here
        # through the existing core helpers (no new logic).
        lab_presets = [
            {"id": p["id"], "name": p["name"], "ip": p["ip"],
             "seats": p["seats"], "display": p["display"]}
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
            "settings": core.inspect_settings(fields.get("project_path", "")),
            # False on first run (lab_info.json absent) → the UI shows a setup
            # notice instead of an empty lab selector / seat map.
            "lab_info_present": core.lab_info_present(),
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
            "default_database": str(self.store_extra.get(
                "default_database", core.DB_BUILTIN_LAB)),
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
            "project": project_status(fields.get("project_path", "")),
            "settings": core.inspect_settings(fields.get("project_path", "")),
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
                        "settings": core.inspect_settings(path)})

    @api_call
    def pick_participant_file(self):
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
                "settings": core.inspect_settings(path)}

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
        self.presets.append(preset)
        self.presets = core.sort_presets(self.presets)
        # Remember the author across sessions (round-tripped through store_extra),
        # so the next Save As prefills it. Kept JSON-serialisable; unknown keys
        # in store_extra are preserved by save_store.
        if preset.get("author"):
            self.store_extra["last_author"] = preset["author"]
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
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
        self.presets = [p for p in self.presets if p is not preset]
        if not self.presets:
            self.presets = [core.default_preset()]
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "configs": [preset_row(p) for p in self.presets],
                "selected": self.presets[0].get("name")}

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
        presets = core.default_lab_presets()
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
        core.apply_lab_marker(self.presets, lab)
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
        except OSError:
            pass
        selected = self.presets[0] if self.presets else None
        fields = self._config_fields(selected) if selected else dict(core.DEFAULT_CONFIG)
        preset = core.find_lab_preset(lab, presets)
        return {"ok": True, "lab_marker": lab,
                "lab_name": preset.get("name") if preset else lab,
                "configs": [preset_row(p) for p in self.presets],
                "selected": selected.get("name") if selected else "",
                "fields": fields}

    # -- room picker / create-database / lab-settings bridges --------------
    # Each maps straight onto an existing otree_core function; no launch logic
    # lives here. Used by the rebuilt web UI (room picker, Create-a-database,
    # the Lab Settings page).

    @api_call
    def list_project_rooms(self, project_path):
        """The room names the project's own settings.py defines (Feature 3)."""
        return core.enumerate_project_rooms(project_path)

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
        """Persist which built-in database a brand-new config starts on (Lab
        Settings > Default database). Mirrors the Tk ``_save_default_db``."""
        db_id = (db_id or "").strip() or core.DB_BUILTIN_LAB
        self.store_extra["default_database"] = db_id
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "default_database": db_id}

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
                entry = core.register_database(
                    self.store_extra, title=new_db, researcher=researcher,
                    connection=fields, postgres_user=fields.get("db_user", ""))
                core.save_store(self.presets, self.store_extra, self.store_path)
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
        self.store_extra["pg_admin"] = admin
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "pg_admin": core.pg_admin_from_store(self.store_extra)}

    @api_call
    def set_lab_display(self, lab_id, display):
        """Show/hide a lab in the main selector from the Lab Settings table."""
        presets = core.lab_presets_from_store(self.store_extra)
        ok, message, presets = core.set_lab_display(presets, lab_id, display)
        if not ok:
            return {"ok": False, "message": message}
        self.store_extra["lab_presets"] = presets
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "presets": _lab_rows(presets), "labs": _lab_tiles(presets)}

    @api_call
    def add_lab(self, name, ip, seats, cols=0):
        """Add a lab preset from the Lab Settings page (web parity with Tk).

        Wraps core.add_lab_preset (validate → append), persists to the store, and
        returns the refreshed preset rows + selector tiles for the page to
        repaint. The web rebuild previously left this button a UI stub.
        """
        presets = core.lab_presets_from_store(self.store_extra)
        ok, message, presets, preset = core.add_lab_preset(
            presets, name, ip, seats, display=True, cols=cols or 0)
        if not ok:
            return {"ok": False, "message": message}
        self.store_extra["lab_presets"] = presets
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "message": message, "presets": _lab_rows(presets),
                "labs": _lab_tiles(presets),
                "new_id": preset["id"] if preset else ""}

    @api_call
    def update_lab(self, lab_id, name=None, ip=None, seats=None, cols=None):
        """Edit an existing lab preset from the Lab Settings page (web/Tk parity)."""
        presets = core.lab_presets_from_store(self.store_extra)
        ok, message, presets, preset = core.update_lab_preset(
            presets, lab_id, name=name, ip=ip, seats=seats, cols=cols)
        if not ok:
            return {"ok": False, "message": message}
        self.store_extra["lab_presets"] = presets
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "message": message, "presets": _lab_rows(presets),
                "labs": _lab_tiles(presets)}

    @api_call
    def delete_lab(self, lab_id, selected_lab=None):
        """Delete a lab preset from the Lab Settings page (web/Tk parity).

        Refuses to remove the last displayed lab (core.delete_lab_preset), so the
        selector always keeps a lab. Returns the next lab to select when the
        deleted one was the current selection.
        """
        presets = core.lab_presets_from_store(self.store_extra)
        ok, message, presets, next_selected = core.delete_lab_preset(
            presets, lab_id, selected_lab=selected_lab)
        if not ok:
            return {"ok": False, "message": message}
        self.store_extra["lab_presets"] = presets
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
        except OSError as error:
            return {"ok": False, "message": "Could not save: %s" % error}
        return {"ok": True, "message": message, "presets": _lab_rows(presets),
                "labs": _lab_tiles(presets), "next_selected": next_selected}

    # -- settings.py block -------------------------------------------------

    @api_call
    def settings_status(self, project_path):
        # Read-only: we detect whether the researcher's settings.py has the
        # block and report it. The launcher never writes into their code.
        return core.inspect_settings(project_path)

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
        state = core.inspect_settings(project_path)
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
        except OSError as error:
            return {"ok": False, "message": "Could not add the block: %s" % error,
                    "log": [["err", "Could not add the block: %s" % error]]}
        return {"ok": True, "backup": backup, "path": path,
                "settings": core.inspect_settings(project_path),
                "log": [["ok", "Backed up settings.py to %s" % backup],
                        ["ok", "Appended the oTree lab support block to %s" % path]]}

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
        self._spawn(lambda: self._dialog_export(shortcut["content"], shortcut["filename"],
                                                shortcut["ext"]), "dlg-export")
        return {"ok": True, "pending": True}

    def _dialog_export(self, text, default_name, ext=".bat"):
        import webview
        try:
            result = self.window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name,
                file_types=("One-click shortcut (*%s)" % ext, "All files (*.*)"))
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
        self._callback("pywOnBatResult", {"ok": True, "path": path})

    # -- the launch --------------------------------------------------------

    @api_call
    def launch_briefing(self, fields):
        """What to tell the experimenter BEFORE the server starts (Deliverable 4).

        A pure description: it starts nothing. The web UI shows this in a
        confirmation modal and only calls ``launch`` after an explicit OKAY.
        The behaviour described is the real, verified one: the lab machines
        open a per-seat link ``http://HOST:PORT/room/ROOM?participant_label=SEAT``
        (always active), and a participant arriving turns their seat from a grey
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
        if self._project_needs_block(cfg):
            issues.append({
                "level": "warn",
                "title": "This project has no oTree lab support block, so the lab "
                         "room, seat board and lab database won’t take effect "
                         "without it.",
                "hint": "Adds a clearly-marked block to the end of settings.py, "
                        "after a timestamped .bak backup (fully revertible). The "
                        "manual copy/paste is on the info screen.",
                "fix": "add_block", "fix_label": "Add it for me", "info": "block"})
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
                        state = core.inspect_settings(path)
                        if state.get("readable") and not state.get("has_block"):
                            issue["add_room"] = room
            issues.append(issue)
        return {"ok": True, "issues": issues}

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
    def launch(self, fields, force=False):
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

        self._spawn(lambda: self._run_launch(cfg), "launch")
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

    def _run_launch(self, cfg):
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
            subprocess.Popen(launch["cmd"], **popen_kwargs)
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
            result = {"ok": True, "method": "manual",
                      "monitor_url": "http://%s:%s%s" % (
                          core.AUTOLOGIN_HOST, cfg.get("port", "8000"),
                          core.room_monitor_path(cfg.get("room_name", "")))}
            if cfg.get("open_browser"):
                # Open the dashboard the instant the server responds. The readiness
                # poll lives in core.open_dashboard_authenticated (wait_for_server),
                # so there is no blind fixed pre-open delay any more.
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

            self._mark_run(cfg)
            self._status("ok", "Launched: the oTree dashboard is opening in your browser. The "
                               "server runs in its own window; watch there for live logs. You "
                               "can close this launcher.")
            # Only a real success reaches here: tell the handoff banner, with the
            # method so it reports honestly (already logged in vs at the login page).
            self._launch_result(True, "", url=result["monitor_url"],
                                method=result["method"])
        except Exception:
            LOG.exception("_run_launch: EXCEPTION")
            self._log("err", traceback.format_exc())
            self._status("err", "Launch failed. See the activity log.")
            self._launch_result(False, "Launch failed. See the activity log.")
        finally:
            LOG.info("_run_launch: end")

    def _launch_result(self, ok, message, url="", method="manual"):
        """Deliver the REAL launch outcome to the page's handoff banner.

        Worker-thread only (it uses evaluate_js via _callback). ``ok`` True means
        the server actually started; ``url`` is then the open-dashboard link and
        ``method`` is "cookie" (opened already logged in) or "manual" (opened the
        login page). On failure ``message`` explains what went wrong; there is NO
        false success.
        """
        self._callback("pywOnLaunchResult",
                       {"ok": bool(ok), "message": message, "url": url,
                        "method": method})

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
        proc = subprocess.Popen(
            core.resetdb_command(), cwd=project, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
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

    def _mark_run(self, cfg):
        """Stamp the matching saved config as run just now, if one matches."""
        for preset in self.presets:
            if not core.configs_differ(preset, cfg):
                preset["last_run"] = core.now_iso()
                break
        try:
            core.save_store(self.presets, self.store_extra, self.store_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    LOG.info("=" * 60)
    LOG.info("startup: otree_launcher_web on %s, python %s",
             sys.platform, sys.version.split()[0])
    LOG.info("log file: %s", LOG_PATH)
    # Pull any pre-data/ files (lab.local, lab_info.json, presets.json, seats/)
    # into data/ before anything reads them, then refresh lab_info.
    core.migrate_legacy_data()
    core.reload_lab_info()
    try:
        import webview
    except ImportError:
        LOG.error("pywebview not installed")
        sys.stderr.write(
            "pywebview is not installed. Run:  pip install pywebview\n"
            "(On Windows it also needs the Edge WebView2 runtime, which ships "
            "with Windows 10/11.)\n"
        )
        return 2

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
    # debug=True turns on the WebView2 devtools (right-click → Inspect) so the
    # Console is reachable while we chase the freeze on the lab machine.
    LOG.info("webview.start(debug=True)")
    try:
        webview.start(debug=True)
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
