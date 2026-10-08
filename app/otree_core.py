#!/usr/bin/env python3
"""oTree Lab Launcher.

A small desktop front end for starting an oTree experiment on the lab
machines.  It replaces `set_up_otree_original.bat`, the batch file that lab
staff used to hand-edit before every session.

Nothing is ever written into a batch file in order to launch.  The launcher
builds an environment dictionary (a copy of os.environ plus the values from the
chosen config) and runs `otree resetdb` and `otree prodserver` as child
processes with the working directory set to the chosen oTree project folder.

Standard library only: Python 3 + tkinter/ttk.  No pip installs, no build step.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as _dt
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

# ---------------------------------------------------------------------------
# PUBLIC API INDEX for the two UIs (Tk + web). The database registry and the
# researcher roster (Round 3) are global, passive collections stored in the
# store's `extra` dict alongside lab_presets/pg_admin. Both UIs call the SAME
# functions below; the full signatures live in _ai/CORE_DB_API.md.
#
#   Databases (registry):
#     list_databases(extra)            -> [sqlite builtin, lab builtin, *customs]
#     known_databases_from_store(extra)-> just the custom entries (registry)
#     builtin_databases()              -> the two always-present built-ins
#     find_database(extra, db_id)      -> one entry (builtin or custom) or None
#     database_config_fields(entry)    -> the db_* config overrides to apply
#     register_database(extra, title, researcher, connection=..., ...)
#                                      -> append a custom DB + record researcher
#     edit_database(extra, db_id, ...) -> edit an existing DB (custom OR the
#                                         lab-shared built-in) + persist
#     normalize_database_entry(raw)    -> one normalized custom entry
#     slugify_pg_dbname(name)          -> a Postgres-safe db name from any label
#     switch_to_lab_default(cfg)       -> a cfg switched to the lab shared DB
#                                         (the "Use lab default instead" recovery)
#
#   The lab-shared DEFAULT database (a REFERENCE into the one list above):
#     default_database_id(extra)       -> the chosen default's id (lab built-in fallback)
#     resolve_default_database(extra)  -> the chosen default entry
#     default_database_options(extra)  -> the databases a user may pick as default
#     set_default_database(extra, id)  -> promote a database to the lab-shared default
#     apply_default_database(extra)    -> re-point the live LAB_DB at that default
#
#   Researchers (roster):
#     list_researchers(extra, presets=None) -> the deduped shared roster
#     researchers_from_store(extra)    -> only the explicitly-saved names
#     add_researcher(extra, name)      -> append one name (dedupe)
#
# The registry NEVER connects to Postgres; only create_database() does, and its
# caller then calls register_database() with the confirmed result's `fields`.
# ---------------------------------------------------------------------------

APP_NAME = "oTree Lab Launcher"
APP_AUTHOR = "Julian Tait"
# A semver version string (MAJOR.MINOR.PATCH). The app footer always shows it, and
# the once-a-day update check compares it against the latest GitHub RELEASE tag
# (tag_name, e.g. "v1.2.0") with a small semver compare -- only a strictly greater
# release tag counts as "newer". Bump this whenever a release is cut.
APP_VERSION = "1.6.4"

# ---------------------------------------------------------------------------
# The data folder (schema_version 1, release 1.5.0). data/ is fully user-owned
# and gitignored; nothing in it ships. Three files, three scopes:
#   lab_info.json       LAB level: copied between the PCs of a lab to set one up
#                        (labs, maps, default oTree admin login, lab settings).
#   machine.json        THIS PC only: which lab it is (home_lab), which labs it
#                        shows, the theme, the Postgres admin login and this PC's
#                        databases (with passwords) + its default database.
#   saved_configs.json  THIS PC's saved launch configs (each references a database
#                        by id) + the researcher roster.
# Generated: launch_history.jsonl, seats/, update_check.json, logs, locks/.
# Old (v0) files -- presets.json, lab.local, ui_prefs.json, sessions.jsonl,
# maps/ -- are converted once at startup by migrate_data_folder() and moved to
# data/retired/. See _ai/storage_redesign_plan.md.
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 1
# Back-compat alias (older callers/tests read STORAGE_VERSION).
STORAGE_VERSION = SCHEMA_VERSION
MACHINE_FILENAME = "machine.json"
SAVED_CONFIGS_FILENAME = "saved_configs.json"
LAUNCH_HISTORY_FILENAME = "launch_history.jsonl"
# Every study cloned from GitHub on this computer (and every fresh copy).
CLONE_HISTORY_FILENAME = "clone_history.jsonl"
UPDATE_CHECK_FILENAME = "update_check.json"
LOCKS_DIRNAME = "locks"
RETIRED_DIRNAME = "retired"
ASSETS_DIRNAME = "assets"
# Old (v0) file names, read only by the migration (and its fail-soft fallback).
PRESETS_FILENAME = "presets.json"
SESSIONS_FILENAME = "sessions.jsonl"
LAB_MARKER_FILENAME = "lab.local"
LEGACY_UI_PREFS_FILENAME = "ui_prefs.json"
LEGACY_MAPS_DIRNAME = "maps"


class NewerSchemaError(Exception):
    """A data file was written by a NEWER launcher (its schema_version is higher
    than this app knows). The app refuses to migrate or overwrite it."""

    def __init__(self, path, version):
        self.path = path
        self.version = version
        Exception.__init__(self, (
            "%s was saved by a newer version of the launcher (schema %s; this "
            "version knows up to %s). Update the launcher before using this data "
            "folder." % (os.path.basename(str(path)), version, SCHEMA_VERSION)))

# ---------------------------------------------------------------------------
# Lab-specific data lives in lab_info.json (gitignored), NOT in this source.
# See lab_info.example.json for the schema and the two example rooms. When the
# file is absent the loader returns None and the app runs its first-run setup
# wizard to create it; the safe placeholder defaults below keep the module
# importable in the meantime. Nothing lab-specific is ever hardcoded here.
# ---------------------------------------------------------------------------

LAB_INFO_FILENAME = "lab_info.json"
LAB_INFO_EXAMPLE_FILENAME = "lab_info.example.json"


# ---------------------------------------------------------------------------
# One folder for everything the launcher reads and writes. The code lives in
# app/ but `data/` sits at the REPO ROOT (one level up, beside app/), so it stays
# at the top level and an existing install keeps its data across an update:
# lab.local, lab_info.json, presets.json, seats/, app-written logs, the
# corrupt-store rescue copy, and the shipped lab_info.example.json + maps/ all
# live under it. Updating is "copy the new version over the top, keep your data/
# folder". Only the DEFAULT base lives here; the env overrides (OTREE_LAB_INFO,
# OTREE_LAB_MARKER, OTREE_LAB_LAUNCHER_PRESETS) still win when set.
# ---------------------------------------------------------------------------

DATA_DIRNAME = "data"


def repo_root():
    """The repo root: the parent of app/ (where data/ lives). data/ is anchored
    here, NOT next to __file__, so it stays at the top level."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_dir():
    """The folder holding the app code files (app/)."""
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """The single folder holding everything the launcher reads and writes:
    the repo root's data/ folder (parent of app/). OTREE_LAB_DATA_DIR overrides
    it (the test suite points it at a temp folder so no test touches real data)."""
    override = os.environ.get("OTREE_LAB_DATA_DIR")
    if override:
        return override
    return os.path.join(repo_root(), DATA_DIRNAME)


def assets_dir():
    """Shipped, read-only files (the example lab_info, example maps, the data
    folder README text): app/assets/. Nothing here is ever written."""
    return os.path.join(app_dir(), ASSETS_DIRNAME)


def locks_dir(folder=None):
    """Where every lock file lives: data/locks/ (each is removed after use)."""
    return os.path.join(folder or data_dir(), LOCKS_DIRNAME)


def lock_path(name, folder=None):
    """The lock file for ``name`` inside locks/ (the folder is created)."""
    target = locks_dir(folder)
    os.makedirs(target, exist_ok=True)
    return os.path.join(target, name + ".lock")


def secure_chmod(path):
    """Restrict a secret-bearing file to owner-only (0o600) on POSIX.

    lab_info.json and presets.json hold database and oTree admin passwords, so on
    a shared lab machine another local account must not be able to read them. On
    Windows os.chmod only toggles the read-only bit (a near no-op) and file
    security is really governed by NTFS ACLs, which this does NOT harden -- that
    is a known gap noted here deliberately, not an oversight. Any chmod error is
    swallowed so a permission quirk never breaks a save.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@contextlib.contextmanager
def exclusive_file_lock(lock_path):
    """Best-effort exclusive lock held for the duration of the ``with`` block.

    Uses fcntl.flock on POSIX and msvcrt.locking on Windows, on a dedicated
    sibling lock file. If neither locking primitive is available the lock is a
    no-op; the caller's atomic re-read + os.replace is still correct on its own,
    the lock only serialises concurrent appenders so they cannot each pass the
    "marker not present" check before either writes.

    The lock file is REMOVED after use (1.5.0: data/locks/ used to collect
    hundreds of stale files). After locking, the holder checks the path still
    names the file it locked (another holder may have removed it meanwhile) and
    retries on a fresh file if not, so removing it never lets two holders in.
    """
    folder = os.path.dirname(lock_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    handle, locked, posix = None, False, False
    for _attempt in range(50):
        handle = open(lock_path, "a+")
        locked = posix = False
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = posix = True
        except ImportError:
            try:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            except (ImportError, OSError):
                pass
        if not locked:
            break
        try:
            same = os.path.samestat(os.fstat(handle.fileno()), os.stat(lock_path))
        except OSError:
            same = False
        if same:
            break
        _release_file_lock(handle, posix)
        handle = None
    try:
        yield
    finally:
        if handle is not None:
            if locked and posix:
                # POSIX: unlink while still holding it; a waiter that then gets
                # the lock sees the path is gone and retries on a new file.
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
            _release_file_lock(handle, posix if locked else None)
            if locked and not posix:
                # Windows: an open file cannot be deleted, so remove it after
                # closing; if another process already holds it, it stays for
                # that holder to remove.
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass


def _release_file_lock(handle, posix):
    """Unlock (posix True/False; None = never locked) and close a lock handle."""
    if posix is True:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
    elif posix is False:
        try:
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
    try:
        handle.close()
    except OSError:
        pass


def launch_port(cfg):
    """The validated launch port for this config as an int in 1..65535, or None.

    ONE source of truth for the port, so the ``otree prodserver`` command, the
    port preflight, and the opened URLs all agree. A blank, non-numeric, or
    out-of-range value returns None, meaning "let oTree use its default 8000"
    (the out-of-range case is what the port preflight turns into a clean
    validation failure rather than letting an OverflowError escape from
    ``socket.bind``).
    """
    raw = str(normalize_config(cfg)["port"]).strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


# ---------------------------------------------------------------------------
# JSON helpers shared by the three data files (lab_info.json, machine.json,
# saved_configs.json). Every file carries a top-level schema_version; a missing
# one means the old (v0) layout.
# ---------------------------------------------------------------------------


def schema_version_of(data):
    """The schema_version of a loaded data file (missing or garbled = 0, "v0")."""
    if not isinstance(data, dict):
        return 0
    try:
        return max(0, int(data.get("schema_version", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _read_json(path):
    """``(status, data)`` for a JSON file: status is "missing", "malformed" or "ok"."""
    if not path or not os.path.exists(path):
        return "missing", None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return "ok", json.load(handle)
    except (OSError, ValueError):
        return "malformed", None


def refuse_newer_schema(path):
    """Raise :class:`NewerSchemaError` when the file at ``path`` was written by a
    newer launcher. Absent or unreadable files pass."""
    status, data = _read_json(path)
    if status == "ok" and schema_version_of(data) > SCHEMA_VERSION:
        raise NewerSchemaError(path, schema_version_of(data))


def write_json_atomic(path, data, prefix=".data-"):
    """Write ``data`` to ``path`` atomically and owner-only (0o600).

    A temp file in the same folder is written, fsync'd and os.replace'd into
    place, so an interrupted write never leaves half a file. The data files hold
    passwords, hence 0o600 on POSIX (see :func:`secure_chmod`). Refuses with
    :class:`NewerSchemaError` to overwrite a file a newer launcher wrote.
    """
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    refuse_newer_schema(path)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=folder, prefix=prefix, suffix=".tmp", delete=False)
    tmp_name = handle.name
    try:
        secure_chmod(tmp_name)
        handle.write(text)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    except Exception:
        handle.close()
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    secure_chmod(path)
    return path


# ---------------------------------------------------------------------------
# lab_info.json (LAB level). The ONLY place labs live: the setup wizard and Lab
# Settings write here, and a lab manager copies this one file to every PC of the
# lab. Schema 1:
#   {schema_version, labs: [{id, name, host, seats, geometry, cols, map,
#    default_room, shortcut_label, deleted, suggested_database: {db_name,
#    db_user}}], maps: {name: map}, default_admin: {username, password},
#    github_sync_enabled, github_org, databases_localhost_only}
# No database passwords and nothing machine-specific live here.
# ---------------------------------------------------------------------------


def lab_info_path():
    """Where lab_info.json lives. OTREE_LAB_INFO overrides it (used by tests)."""
    override = os.environ.get("OTREE_LAB_INFO")
    if override:
        return override
    return os.path.join(data_dir(), LAB_INFO_FILENAME)


def lab_info_status(path=None):
    """Distinguish the three states of lab_info.json.

    Returns one of:
      "missing"  -- the file does not exist (genuine first run)
      "malformed" -- the file exists but is unreadable or not a JSON object
                     (a hand-edit typo, or an interrupted write); it is
                     RECOVERABLE, so it is moved aside, never silently overwritten
      "ok"       -- the file exists and parses to a dict
    """
    status, data = _read_json(path or lab_info_path())
    if status == "ok" and not isinstance(data, dict):
        return "malformed"
    return status


def load_lab_info_raw(path=None):
    """lab_info.json exactly as stored (any schema), or None."""
    status, data = _read_json(path or lab_info_path())
    return data if status == "ok" and isinstance(data, dict) else None


def load_lab_info(path=None):
    """lab_info.json as a schema-1 dict, or None when absent or unreadable.

    An old (v0) file is converted IN MEMORY (nothing is written here; the
    startup migration does the writing). For the default location the full
    legacy conversion is used, so Lab Settings edits that an old install kept in
    presets.json still show even if the migration could not run (fail-soft).
    A present-but-unparseable file returns None, like a first run.
    """
    default = path is None
    path = path or lab_info_path()
    data = load_lab_info_raw(path)
    if default and (data is None or schema_version_of(data) == 0):
        legacy = _legacy_view()
        if legacy is not None and legacy.get("lab_info") is not None:
            return copy.deepcopy(legacy["lab_info"])
    if data is None:
        return None
    if schema_version_of(data) == 0:
        return lab_info_from_v0(data, os.path.dirname(os.path.abspath(path)))
    return normalize_lab_info(data)


def preserve_corrupt_lab_info(path=None):
    """Rename a malformed lab_info.json to a timestamped ``.corrupt`` copy.

    Called before lab_info.json is rewritten: when the existing file EXISTS but
    is unreadable/invalid it is recoverable, so it is moved aside
    (``lab_info.json.<stamp>.corrupt``) rather than silently overwritten.
    Returns the recovery path if one was made, else None. Never raises.
    """
    path = path or lab_info_path()
    if lab_info_status(path) != "malformed":
        return None
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    recovery = "%s.%s.corrupt" % (path, stamp)
    try:
        os.replace(path, recovery)
    except OSError:
        return None
    return recovery


def lab_info_present(path=None):
    """True when a readable lab_info.json (any schema) exists."""
    return load_lab_info(path) is not None


def save_lab_info(data, path=None):
    """Write a lab_info dict to lab_info.json (schema 1) atomically, owner-only.

    A v0 dict is converted first. Holds the lab_info lock, refuses to overwrite a
    newer-schema file, and moves a malformed existing file aside first. Returns
    the path."""
    path = path or lab_info_path()
    if schema_version_of(data) == 0 and _looks_v0_lab_info(data):
        data = lab_info_from_v0(data, os.path.dirname(os.path.abspath(path)))
    data = normalize_lab_info(data)
    with exclusive_file_lock(lock_path("lab_info", os.path.dirname(os.path.abspath(path)))):
        preserve_corrupt_lab_info(path)
        write_json_atomic(path, data, prefix=".lab_info-")
    return path


def _looks_v0_lab_info(data):
    """True for an old-layout lab_info dict (labs keyed by id, a database block,
    an admin block or default_lab)."""
    if not isinstance(data, dict):
        return False
    return (isinstance(data.get("labs"), dict) or "database" in data
            or "admin" in data or "default_lab" in data)


def normalize_stored_lab(raw):
    """One lab of lab_info.json in its stored (schema 1) shape."""
    raw = raw or {}
    lab_id = str(raw.get("id", "") or "").strip()
    seats = raw.get("seats", [])
    if isinstance(seats, (list, tuple)):
        seats = [str(s).strip() for s in seats if str(s).strip()]
    else:
        seats = parse_seat_list(seats)
    geo = str(raw.get("geometry", "") or "")
    if geo not in LAB_GEOMETRIES:
        geo = LAB_GEO_GRID
    try:
        cols = max(0, int(raw.get("cols", 0) or 0))
    except (TypeError, ValueError):
        cols = 0
    map_field = raw.get("map")
    entry = {
        "id": lab_id,
        "name": str(raw.get("name", "") or "").strip() or lab_id,
        "host": str(raw.get("host", raw.get("ip", "")) or "").strip(),
        "seats": seats,
        "geometry": geo,
        "cols": cols,
        "map": map_field.strip() if isinstance(map_field, str) else "",
        "default_room": sanitize_room_name(raw.get("default_room")),
        "shortcut_label": str(raw.get("shortcut_label", "") or "").strip(),
        "deleted": bool(raw.get("deleted", False)),
    }
    suggested = raw.get("suggested_database")
    if isinstance(suggested, dict) and str(suggested.get("db_name", "") or "").strip():
        entry["suggested_database"] = {
            "db_name": str(suggested.get("db_name", "")).strip(),
            "db_user": str(suggested.get("db_user", "") or "").strip()}
    return entry


def normalize_lab_info(info):
    """A schema-1 lab_info dict with every field in a known shape.

    Unknown top-level keys are kept (a newer launcher may add some). A lab whose
    ``map`` is an inline object gets that object moved into the ``maps`` table
    under the lab's id."""
    info = dict(info or {})
    maps = info.get("maps")
    maps = {str(k): v for k, v in maps.items() if isinstance(v, dict)} \
        if isinstance(maps, dict) else {}
    labs = []
    seen = set()
    raw_labs = info.get("labs")
    if isinstance(raw_labs, dict):   # tolerate a hand-made dict keyed by id
        raw_labs = [dict(v or {}, id=k) for k, v in raw_labs.items()]
    for raw in (raw_labs if isinstance(raw_labs, list) else []):
        if not isinstance(raw, dict):
            continue
        entry = normalize_stored_lab(raw)
        if not entry["id"] or entry["id"] in seen:
            continue
        if isinstance(raw.get("map"), dict):
            name = _unique_key(entry["id"], maps)
            maps[name] = raw["map"]
            entry["map"] = name
        seen.add(entry["id"])
        labs.append(entry)
    admin = info.get("default_admin")
    if not isinstance(admin, dict):
        admin = info.get("admin") if isinstance(info.get("admin"), dict) else {}
    out = {"schema_version": SCHEMA_VERSION}
    comment = info.get("_comment")
    if comment:
        out["_comment"] = comment
    out["labs"] = labs
    out["maps"] = maps
    out["default_admin"] = {"username": str(admin.get("username", "") or "admin"),
                            "password": str(admin.get("password", "") or "")}
    for key, value in info.items():
        if key in ("schema_version", "_comment", "labs", "maps", "default_admin",
                   "admin", "default_lab", "database"):
            continue
        out[key] = value
    return out


def _unique_key(base, table):
    """``base`` or ``base_2``/``base_3``... so it is not already a key of ``table``."""
    base = str(base or "map")
    if base not in table:
        return base
    n = 2
    while "%s_%d" % (base, n) in table:
        n += 1
    return "%s_%d" % (base, n)


def lab_info_labs(info=None):
    """The stored labs (schema-1 entries, incl. soft-deleted) of a lab_info dict
    (the live one by default)."""
    info = LAB_INFO if info is None else info
    if not isinstance(info, dict):
        return []
    if schema_version_of(info) == 0 and _looks_v0_lab_info(info):
        info = lab_info_from_v0(info, None)
    return normalize_lab_info(info)["labs"]


def lab_info_maps(info=None):
    """The inline maps table (name -> map object) of a lab_info dict."""
    info = LAB_INFO if info is None else info
    maps = (info or {}).get("maps")
    return dict(maps) if isinstance(maps, dict) else {}


def lab_info_example_path():
    """Where the shipped example lab_info.json lives: app/assets/."""
    return os.path.join(assets_dir(), LAB_INFO_EXAMPLE_FILENAME)


def load_example_lab_info():
    """The shipped lab_info.example.json as a schema-1 dict, or None."""
    return load_lab_info(lab_info_example_path())


def example_lab_info_to_save():
    """The example as a lab_info dict ready for "Use example values", or None.
    Drops the ``_comment`` and the GitHub Organisation Sync placeholders."""
    example = load_example_lab_info()
    if example is None:
        return None
    for key in ("_comment", "github_sync_enabled", "github_org"):
        example.pop(key, None)
    return example


def available_maps():
    """Sorted names of the maps a lab may reference: the live lab_info.json
    ``maps`` table plus the shipped example maps (app/assets/maps/)."""
    names = set(lab_info_maps().keys())
    try:
        names.update(f[:-5] for f in os.listdir(os.path.join(assets_dir(), "maps"))
                     if f.endswith(".json"))
    except OSError:
        pass
    return sorted(names)


def validate_wizard_labs(labs):
    """Validate a wizard's list of labs with the SAME rule the Lab Settings path
    uses (:func:`validate_lab_preset_fields`): each lab needs a name, a non-empty
    Host/IP and at least one valid, non-duplicate seat label; and no two labs may
    resolve to the same id. Returns ``(ok, message)``. Both wizard faces call
    this before writing lab_info.json (ONE validator; the wizards do not invent
    their own)."""
    labs = labs or []
    if not labs:
        return False, "Add at least one lab first."
    seen = set()
    for raw in labs:
        name = str(raw.get("name") or "")
        host = str(raw.get("host") or "")
        seats = raw.get("seats") or []
        ok, message, _ = validate_lab_preset_fields(name, host, seats)
        if not ok:
            label = name.strip() or "(unnamed lab)"
            return False, "%s: %s" % (label, message)
        lab_id = str(raw.get("id") or "").strip() or _slugify_lab_id(name)
        if lab_id in seen:
            return False, ("Two labs resolve to the same id (%s). Give them "
                           "distinct names." % lab_id)
        seen.add(lab_id)
    return True, ""


def build_lab_info(labs, database=None, admin=None, default_lab=None, maps=None):
    """Assemble a schema-1 lab_info dict from wizard inputs.

    ``labs`` is a list of {"id"?, "name", "host", "seats": [...], "map": <name or
    "">, "default_room"?, "shortcut_label"?, "suggested_database"?}. A blank id is
    slugified from the name. ``admin`` is the default oTree admin login
    ({username, password}). ``database`` is optional: only its name and user are
    used, as the ``suggested_database`` of ``default_lab`` (or the first lab) when
    that lab has none -- a database password never goes into lab_info.json.
    Each referenced map is copied into the inline ``maps`` table (from ``maps``,
    the live lab_info or the shipped examples).

    A lab that fails :func:`validate_lab_preset_fields` (empty Host/IP, no or
    invalid seats, a duplicate id) is SKIPPED rather than written as a broken
    default; callers gate on :func:`validate_wizard_labs` first.
    """
    admin = admin or {}
    maps = dict(maps or {})
    out_labs = []
    table = {}
    for raw in labs or []:
        name = str(raw.get("name") or "")
        host = str(raw.get("host") or "")
        seats = raw.get("seats") or []
        ok, _message, parsed = validate_lab_preset_fields(name, host, seats)
        if not ok:
            continue
        lab_id = str(raw.get("id") or "").strip() or _slugify_lab_id(name)
        if not lab_id or any(l["id"] == lab_id for l in out_labs):
            continue
        entry = normalize_stored_lab(dict(raw, id=lab_id, name=name.strip() or lab_id,
                                          host=host.strip(), seats=[str(s) for s in parsed]))
        map_name = entry["map"]
        if map_name:
            obj = maps.get(map_name) or load_map_file(map_name)
            if isinstance(obj, dict):
                table[map_name] = obj
            else:
                entry["map"] = ""
        out_labs.append(entry)
    target = str(default_lab or "").strip() or (out_labs[0]["id"] if out_labs else "")
    db_name = str((database or {}).get("db_name", "") or "").strip()
    if db_name:
        for entry in out_labs:
            if entry["id"] == target and "suggested_database" not in entry:
                entry["suggested_database"] = {
                    "db_name": db_name,
                    "db_user": str((database or {}).get("db_user", "") or "").strip()}
    return normalize_lab_info({
        "labs": out_labs,
        "maps": table,
        "default_admin": {"username": str(admin.get("username", "") or "admin"),
                          "password": str(admin.get("password", "") or "")},
    })


# The live, module-level lab and machine state. Filled by reload_lab_info() at the
# END of this module (the loaders need helpers defined further down) and again
# whenever the files change, so every reader sees the current files.
LAB_INFO = None

# Safe placeholders, used only while this PC has no database at all.
_DUMMY_DB = {
    "db_name": "otree", "db_user": "otree", "db_password": "",
    "db_host": "localhost", "db_port": "5432",
}


def lab_db_from_info(info):
    """Back-compat: the ``database`` block of an OLD (v0) lab_info dict, or the
    placeholders. Schema-1 lab_info.json has no database (databases are per PC,
    in machine.json)."""
    out = dict(_DUMMY_DB)
    db = (info or {}).get("database") or {}
    if isinstance(db, dict):
        for key in _DUMMY_DB:
            if db.get(key) is not None:
                out[key] = str(db[key])
    return out


def wizard_lab_db():
    """Back-compat alias: this PC's default database connection (the
    placeholders when it has none)."""
    return dict(LAB_DB)


# LAB_DB holds the connection of THIS PC's default database (machine.json
# default_database), or the placeholders when it has none. It is what a config
# that follows the PC default (db_mode "lab", database_id "") resolves to; every
# downstream reader (normalize_config, build_env, ...) reads this live attribute.
LAB_DB = dict(_DUMMY_DB)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = ""

# db_mode is DERIVED from a config's database_id by normalize_config and is never
# stored (1.5.0): "lab" = follow this PC's default database (only the generated
# Lab default does), "custom" = one database of this PC's list, "none" = SQLite.
DB_MODE_LAB = "lab"
DB_MODE_CUSTOM = "custom"
DB_MODE_NONE = "none"

DB_MODE_LABELS = {
    DB_MODE_LAB: "This computer's default database",
    DB_MODE_CUSTOM: "A database on this computer",
    DB_MODE_NONE: "No lab database (oTree SQLite)",
}
DB_MODE_BY_LABEL = {v: k for k, v in DB_MODE_LABELS.items()}

LAB_CUSTOM = "custom"

# A first-class "Local (this computer)" host for off-lab testing: it resolves to
# localhost (no lab infrastructure needed) and pairs naturally with the oTree
# default (SQLite) database, so a launch runs entirely on the tester's machine.
# It is a pseudo-lab id (like LAB_CUSTOM), not a lab in lab_info.json, so the
# real labs are left untouched.
LAB_LOCAL = "local"
LOCAL_HOST = "localhost"
LOCAL_LAB_LABEL = "Local (this computer)"


def _default_lab_id_from_info(info, machine=None):
    """The lab new configs and the generated Lab default use: this PC's home lab
    when it names a lab, else the first lab that is not deleted, else "lab"."""
    labs = [l for l in lab_info_labs(info or {}) if not l.get("deleted")]
    ids = [l["id"] for l in labs]
    home = str((machine if machine is not None else MACHINE).get("home_lab") or "").strip()
    if home and home in ids:
        return home
    return ids[0] if ids else "lab"


DEFAULT_LAB_ID = "lab"

AUTH_LEVELS = ["STUDY", "DEMO", "none"]

# The room name is static: it must match the room the shortcuts point at.
DEFAULT_ROOM_NAME = "study"


def sanitize_room_name(name, default=DEFAULT_ROOM_NAME):
    """A lab's default room name, coerced to a non-empty single token.

    Whitespace is trimmed and any internal whitespace collapsed to underscores
    (an oTree room name is a single token). A blank/None value falls back to
    ``default`` ("study"). Other characters are left as-is -- this only keeps the
    value a single non-empty token, it does NOT over-restrict, so an existing
    room name a lab already uses is preserved verbatim.
    """
    token = re.sub(r"\s+", "_", str(name or "").strip())
    return token or default


SEAT_DEFAULT = "lab_default"
SEAT_EDIT = "edit"
SEAT_FILE = "file"
SEAT_NONE = "none"

SEAT_MODE_LABELS = {
    SEAT_DEFAULT: "Lab default",
    SEAT_EDIT: "Edit for this run",
    SEAT_FILE: "Use a file from my project",
    SEAT_NONE: "None (open room, no seat board)",
}
SEAT_MODE_BY_LABEL = {v: k for k, v in SEAT_MODE_LABELS.items()}

# oTree validates every label with this exact pattern (otree/common.py).
SEAT_LABEL_RE = re.compile(r"^[a-zA-Z0-9_]+$")

SEAT_ENV_KEYS = ("OTREE_LAB_LABEL_FILE", "OTREE_LAB_ROOM_NAME")

MASK_CHAR = "•"
MASKED_PASSWORD = MASK_CHAR * 8

DEFAULT_CONFIG = {
    "project_path": "",
    # The database a config uses, by REFERENCE: an id from this PC's database
    # list (machine.json), "otree_default" for oTree's own SQLite, or "" = follow
    # this PC's default database (only the generated Lab default does that; a
    # saved config always pins an id). The db_* fields below are DERIVED from it
    # by normalize_config and are never stored.
    "database_id": "",
    "db_mode": DB_MODE_LAB,
    "db_name": LAB_DB["db_name"],
    "db_user": LAB_DB["db_user"],
    "db_password": LAB_DB["db_password"],
    "db_host": LAB_DB["db_host"],
    "db_port": LAB_DB["db_port"],
    "admin_username": DEFAULT_ADMIN_USERNAME,
    "admin_password": DEFAULT_ADMIN_PASSWORD,
    "production": True,
    "auth_level": "STUDY",
    "lab": DEFAULT_LAB_ID,
    "custom_host": "",
    "port": "8000",
    "page": "/rooms",
    "resetdb": True,
    "open_browser": True,
    # Auto-login the admin dashboard after launch (real form-login + one-shot
    # cookie relay). A first-class config field so it round-trips through
    # normalize_config, is saved with the config and reaches
    # open_dashboard_authenticated on every launch path (GUI, web, headless
    # --run). Old stores with no auto_login get True here via normalize_config.
    "auto_login": True,
    "wait_seconds": 5,
    "room_name": DEFAULT_ROOM_NAME,
    "seat_mode": SEAT_DEFAULT,
    "seat_excluded": [],
    "seat_file": "",
}

# The user-editable settings of a config.  Anything outside this list
# (name, created, last_run, plus keys written by a future version) is
# metadata and is never compared or overwritten.
FIELD_KEYS = tuple(DEFAULT_CONFIG.keys())

# Environment variables this launcher owns.  Listed so the log can name them.
DB_ENV_KEYS = ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DATABASE_URL")
OTREE_ENV_KEYS = (
    "OTREE_ADMIN_USERNAME",
    "OTREE_ADMIN_PASSWORD",
    "OTREE_PRODUCTION",
    "OTREE_AUTH_LEVEL",
)
SECRET_ENV_KEYS = ("DB_PASSWORD", "OTREE_ADMIN_PASSWORD", "DATABASE_URL")

CREATE_NEW_CONSOLE = 0x00000010
# CREATE_NO_WINDOW: run a helper subprocess with NO console window at all. Under
# the windowless launcher (pythonw.exe / the .vbs shortcut) a child started
# without this pops a brief console that steals the Windows foreground -- which
# can leave a just-opened modal inactive/greyed -- and, worse, a child that
# INHERITS pythonw's (invalid) std handles can block. Every short prep helper
# below is launched with this flag on Windows and with its std streams
# explicitly redirected, so it neither flashes a window nor inherits a bad
# handle. 0 elsewhere (POSIX ignores it).
CREATE_NO_WINDOW = 0x08000000


def _no_window_flags():
    """CREATE_NO_WINDOW on Windows, 0 elsewhere (for helper subprocesses)."""
    return CREATE_NO_WINDOW if sys.platform.startswith("win") else 0


# ---------------------------------------------------------------------------
# Config helpers (no tkinter here, so the test script can exercise them)
# ---------------------------------------------------------------------------


def now_iso():
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def normalize_config(cfg):
    """Return the field values of `cfg` with defaults filled in.

    Only the keys in FIELD_KEYS are returned; unknown keys stay where they are.
    In lab-default database mode the database fields are forced to the known
    lab default values so that a config can never claim to be the Lab default while
    holding different credentials.
    """
    out = {}
    for key, default in DEFAULT_CONFIG.items():
        value = cfg.get(key, default)
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, list):
            value = [str(item) for item in value] if isinstance(value, (list, tuple)) else []
        elif isinstance(default, int) and not isinstance(default, bool):
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = default
        else:
            value = "" if value is None else str(value)
        out[key] = value

    if out["db_mode"] not in (DB_MODE_LAB, DB_MODE_CUSTOM, DB_MODE_NONE):
        out["db_mode"] = DEFAULT_CONFIG["db_mode"]
    _resolve_config_database(out)
    # `lab` is a lab-preset id (or the two built-in ids, or "custom"). It used
    # to be one of a fixed three; now that new labs are added as presets the id
    # can be any non-empty string, so only a blank value falls back to default.
    # An id that no longer resolves to a preset is handled at resolve time, not
    # silently rewritten here.
    if not str(out["lab"]).strip():
        out["lab"] = DEFAULT_CONFIG["lab"]
    if out["auth_level"] not in AUTH_LEVELS:
        out["auth_level"] = "none"
    if out["wait_seconds"] < 0:
        out["wait_seconds"] = 0
    if out["seat_mode"] not in (SEAT_DEFAULT, SEAT_EDIT, SEAT_FILE, SEAT_NONE):
        out["seat_mode"] = DEFAULT_CONFIG["seat_mode"]
    out["seat_excluded"] = sorted(set(out["seat_excluded"]))
    if not out["room_name"].strip():
        out["room_name"] = DEFAULT_ROOM_NAME
    return out


def _resolve_config_database(out):
    """Fill a normalized config's db_mode + connection from its database_id,
    in place, against THIS PC's live database list (see apply_default_database).

      * "otree_default" (or db_mode none)  -> SQLite, no connection
      * a known id                          -> that database's connection
      * an unknown id                       -> this PC's default database (or
        SQLite when it has none); the id is KEPT so the reference survives, and
        :func:`config_database_note` says what happened
      * ""                                  -> follow this PC's default database
        (db_mode "lab"); a config that carries its own connection with no id
        (an old in-memory dict) keeps it as-is
    """
    db_id = str(out.get("database_id", "") or "").strip()
    out["database_id"] = db_id
    if db_id == DB_BUILTIN_SQLITE or (not db_id and out["db_mode"] == DB_MODE_NONE):
        out["db_mode"] = DB_MODE_NONE
        out["database_id"] = DB_BUILTIN_SQLITE
        return out
    if db_id:
        entry = _DB_REGISTRY.get(db_id)
        if entry is not None:
            out["db_mode"] = DB_MODE_CUSTOM
            for key in DATABASE_CONN_KEYS:
                out[key] = str(entry.get(key, "") or "")
            return out
        default = _DB_REGISTRY.get(_DEFAULT_DB_ID)
        if default is None:
            out["db_mode"] = DB_MODE_NONE
            return out
        out["db_mode"] = DB_MODE_CUSTOM
        for key in DATABASE_CONN_KEYS:
            out[key] = str(default.get(key, "") or "")
        return out
    if out["db_mode"] == DB_MODE_CUSTOM:
        return out   # an old in-memory config carrying its own connection
    if _DEFAULT_DB_ID not in _DB_REGISTRY:
        # This PC has no database set up (setup skipped): oTree's own SQLite.
        out["db_mode"] = DB_MODE_NONE
        out["database_id"] = DB_BUILTIN_SQLITE
        return out
    out["db_mode"] = DB_MODE_LAB
    out.update(LAB_DB)
    return out


def configs_differ(a, b, ignore=()):
    """True when the editable fields of two configs are not the same.

    ``ignore`` names fields to exclude from the comparison. The built-in default
    uses ``ignore=("project_path",)`` so that browsing to a project folder (an
    input to a run, not an edit of the config) does not mark it modified.
    """
    na = normalize_config(a)
    nb = normalize_config(b)
    for key in ignore:
        na.pop(key, None)
        nb.pop(key, None)
    return na != nb


def build_database_url(cfg):
    """The DATABASE_URL for this config, or None when no database is set.

    The user name and password are percent-encoded, so a password that contains
    a URL-special character (``@ : / ? # %`` and friends) cannot break the URL
    or be misparsed by oTree's dj-database-url. A password with none of those
    characters (the Lab default included) is left byte-for-byte unchanged, so
    this is a safety net, not a reformat.
    """
    c = normalize_config(cfg)
    if c["db_mode"] == DB_MODE_NONE:
        return None
    # A BLANK password (a Postgres user with no password: Postgres.app on a Mac,
    # a trust login) leaves the ":password" part out entirely, so the URL is
    # postgres://user@host:port/db -- never a dangling "user:@".
    userinfo = _urlquote(c["db_user"])
    if c["db_password"]:
        userinfo += ":" + _urlquote(c["db_password"])
    return "postgres://{userinfo}@{host}:{port}/{name}".format(
        userinfo=userinfo,
        host=c["db_host"],
        port=c["db_port"],
        name=c["db_name"],
    )


# The grey hint under every Postgres password field (both faces): the password
# may be left blank, and a blank one is used as "no password" end to end.
PG_BLANK_PASSWORD_HINT = ("Leave blank if this Postgres user has no password (e.g. "
                          "Postgres.app on a Mac, or a trust login).")


# The hint under the NEW database user's password (create dialog, wizard
# database cards). A blank password there only works where this Postgres lets
# users in without one (trust, e.g. Postgres.app); a standard Windows install
# (scram-sha-256 in pg_hba.conf) refuses it.
PG_NEW_USER_PASSWORD_HINT = ("Optional. Leave blank only if this Postgres lets users "
                             "in without a password (e.g. Postgres.app on a Mac); on a "
                             "standard Windows install, set one.")
# Shown (as a warning, the database is KEPT) when the login check right after a
# create / register is refused for a user with a blank password.
BLANK_PASSWORD_CREATED_WARNING = ("Created, but this Postgres will not let the new user "
                                  "log in without a password; set a password (Edit "
                                  "database) or use one next time.")
BLANK_PASSWORD_REGISTERED_WARNING = ("Registered, but this Postgres will not let %s log "
                                     "in without a password; set a password (Edit "
                                     "database).")


def pg_password_arg(password):
    """The ``password`` argument for psycopg2.connect: None for a blank password,
    so psycopg2 leaves it out of the connection string altogether (libpq then
    connects with no password, as a trust / peer login expects)."""
    password = "" if password is None else str(password)
    return password or None


def _urlquote(text):
    """Percent-encode one URL userinfo component (nothing is left 'safe')."""
    from urllib.parse import quote
    return quote(str(text), safe="")


_URL_PASSWORD_RE = re.compile(r"^(?P<head>[a-zA-Z][a-zA-Z0-9+.-]*://[^:/@]*:)(?P<pw>[^@]*)(?P<tail>@.*)$")


def mask_database_url(url):
    """Replace the password in a database URL with a fixed run of dots.

    The number of dots is fixed so the length of the real password does not
    leak into the preview or the log.
    """
    if not url:
        return ""
    match = _URL_PASSWORD_RE.match(url)
    if not match:
        return url
    return match.group("head") + MASKED_PASSWORD + match.group("tail")


def credentialed_url(url, username, password):
    """Return ``url`` with HTTP basic-auth userinfo embedded in the host part.

    Best-effort convenience so the admin dashboard can open pre-authenticated
    (no login box): a browser given ``http://user:pass@host/...`` sends the
    credentials itself. The username and password are percent-encoded with
    nothing left "safe", so a password containing ``@ : / # %`` or a space
    cannot break the URL. Any userinfo already in the URL is replaced. Returned
    unchanged when the URL has no network location or both credentials are blank.

    Caveat: the password ends up visible in the URL and the browser history.
    This is a convenience, not a security measure; the copy-the-credentials
    handoff screen is the reliable path.
    """
    from urllib.parse import urlsplit, urlunsplit, quote
    username = "" if username is None else str(username)
    password = "" if password is None else str(password)
    if not username and not password:
        return url
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[-1]   # drop any existing userinfo
    userinfo = quote(username, safe="")
    if password:
        userinfo += ":" + quote(password, safe="")
    return urlunsplit((parts.scheme, userinfo + "@" + host,
                       parts.path, parts.query, parts.fragment))


def mask_credentialed_url(url):
    """A log-safe rendering of a URL that may carry basic-auth userinfo: the
    password (and only the password) is replaced with a fixed run of dots."""
    from urllib.parse import urlsplit, urlunsplit
    if not url:
        return ""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    userinfo, host = parts.netloc.rsplit("@", 1)
    if ":" in userinfo:
        userinfo = userinfo.split(":", 1)[0] + ":" + MASKED_PASSWORD
    return urlunsplit((parts.scheme, userinfo + "@" + host,
                       parts.path, parts.query, parts.fragment))


def _presets_or_default(lab_presets):
    """The passed lab presets, or the labs from lab_info.json when None/empty.

    Host/seat helpers accept ``lab_presets=None`` for convenience; instead of the
    old hardcoded small/large fallback they now resolve against whatever labs
    lab_info.json defines, so no lab data is hardcoded in this module.
    """
    return lab_presets if lab_presets else default_lab_presets()


def resolve_host(cfg, lab_presets=None):
    """The host IP for this config's chosen lab.

    ``lab_presets`` is the list of lab presets (see the lab-preset section
    below). When it is None the two built-in labs are used, so every existing
    caller keeps its old behaviour; when a store's presets are passed the host
    comes from whichever preset the config's ``lab`` id names. ``custom`` still
    means "use the typed custom_host", exactly as before.
    """
    c = normalize_config(cfg)
    lab = c["lab"]
    if lab == LAB_LOCAL:
        return LOCAL_HOST
    if lab == LAB_CUSTOM:
        return c["custom_host"].strip()
    preset = find_lab_preset(lab, _presets_or_default(lab_presets))
    if preset is not None:
        return str(preset.get("ip", "")).strip()
    return c["custom_host"].strip()


# The admin URL path for a specific room's monitor page (the arrival board:
# the room's participant-label grid that flips grey→green as people join).
# Determined EMPIRICALLY against oTree 6.0.15 (see _ai/verify_room_monitor.log):
# the admin /rooms LIST links each room to the "RoomWithoutSession" view, whose
# route is /room_without_session/<room_name> (it 302-redirects to
# /room_with_session/<room_name> once a session is running there). This is the
# page the experimenter actually wants open, so a real chosen room upgrades the
# post-launch auto-open from the /rooms LIST to that room's monitor.
ROOM_MONITOR_PATH = "/room_without_session/%s"

# The launcher's own default auto-open page (the admin rooms LIST). When a config
# still carries this default value we treat the auto-open as "not customised" and
# upgrade it to the chosen room's monitor; any other `page` is a deliberate user
# override and is kept verbatim.
DEFAULT_OPEN_PAGE = DEFAULT_CONFIG["page"]  # "/rooms"


def room_monitor_path(room_name):
    """Admin URL path for a specific room's monitor page (see ROOM_MONITOR_PATH).

    Falls back to the /rooms list when the room name is empty/unresolvable.
    """
    name = str(room_name).strip()
    return (ROOM_MONITOR_PATH % name) if name else DEFAULT_OPEN_PAGE


def _is_default_open_page(page):
    """True when `page` is still the launcher's default (the rooms list), so it
    can be upgraded to the chosen room's monitor rather than treated as a
    deliberate custom override."""
    p = str(page).strip().strip("/").lower()
    return p in ("", "rooms")


def build_url(cfg, lab_presets=None):
    """The page the launcher auto-opens on the EXPERIMENTER PC after the server
    starts.

    By default this now lands on the CHOSEN room's monitor page (the arrival
    board), so selecting a room is meaningful to the experimenter; it falls back
    to the admin /rooms list when no room is resolvable. If the user set an
    explicit `page` other than the default, that page is respected. This does
    NOT change the per-seat participant links the launcher tells staff to open on
    the lab PCs (those stay /room/<name>?participant_label=SEAT&welcome_page_ok=1).
    """
    c = normalize_config(cfg)
    host = resolve_host(c, lab_presets)
    port = c["port"].strip()
    page = c["page"].strip()
    if not host:
        host = "<host>"
    base = "http://" + host
    if port:
        base += ":" + port
    # A page still at the launcher default means "open the rooms area"; upgrade
    # it to the chosen room's monitor. Anything else is a deliberate override.
    if _is_default_open_page(page):
        page = room_monitor_path(c["room_name"])
    if page and not page.startswith("/"):
        page = "/" + page
    return base + page


def build_env(cfg, base_env=None, label_file=None):
    """A copy of the process environment with this config's values applied.

    Variables that the config does not want are removed rather than set to an
    empty or falsy value, so that a stale DATABASE_URL or OTREE_PRODUCTION
    inherited from the machine cannot leak into the run.
    """
    c = normalize_config(cfg)
    env = dict(os.environ if base_env is None else base_env)

    if c["db_mode"] == DB_MODE_NONE:
        for key in DB_ENV_KEYS:
            env.pop(key, None)
    else:
        env["DB_NAME"] = c["db_name"]
        env["DB_USER"] = c["db_user"]
        env["DB_PASSWORD"] = c["db_password"]
        env["DB_HOST"] = c["db_host"]
        env["DB_PORT"] = c["db_port"]
        env["DATABASE_URL"] = build_database_url(c)

    env["OTREE_ADMIN_USERNAME"] = c["admin_username"]
    env["OTREE_ADMIN_PASSWORD"] = c["admin_password"]

    if c["production"]:
        env["OTREE_PRODUCTION"] = "1"
    else:
        env.pop("OTREE_PRODUCTION", None)

    if c["auth_level"] in ("STUDY", "DEMO"):
        env["OTREE_AUTH_LEVEL"] = c["auth_level"]
    else:
        env.pop("OTREE_AUTH_LEVEL", None)

    # A lab launch always opens /room/<room_name>, so always name that room for
    # the block: the block creates it at runtime if the project does not already
    # define it, so the room the launcher opens is guaranteed to exist (even a
    # no-seats lab, which used to 500 with KeyError on a room nothing created).
    # The seat list is exported ONLY when there are seats: with a seat file the
    # block makes it the seat-board room; with none the room is left open (anyone
    # joins). With NO lab environment at all the block stays inert off the lab.
    env["OTREE_LAB_ROOM_NAME"] = c["room_name"]
    if c["seat_mode"] == SEAT_NONE or not label_file:
        env.pop("OTREE_LAB_LABEL_FILE", None)
    else:
        env["OTREE_LAB_LABEL_FILE"] = label_file

    return env


def launcher_env_keys(cfg, label_file=None):
    """The names of the variables this config actually sets, in order."""
    c = normalize_config(cfg)
    keys = []
    if c["db_mode"] != DB_MODE_NONE:
        keys.extend(DB_ENV_KEYS)
    keys.append("OTREE_ADMIN_USERNAME")
    keys.append("OTREE_ADMIN_PASSWORD")
    if c["production"]:
        keys.append("OTREE_PRODUCTION")
    if c["auth_level"] in ("STUDY", "DEMO"):
        keys.append("OTREE_AUTH_LEVEL")
    # A lab launch always names the room it opens; the seat file only when
    # there are seats. Mirrors build_env exactly.
    keys.append("OTREE_LAB_ROOM_NAME")
    if c["seat_mode"] != SEAT_NONE and label_file:
        keys.append("OTREE_LAB_LABEL_FILE")
    return keys


def describe_env_value(key, value):
    """A log-safe rendering of one environment variable."""
    if key == "DATABASE_URL":
        return mask_database_url(value)
    if key in SECRET_ENV_KEYS:
        return MASKED_PASSWORD
    return value


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------


def lab_default_seats(cfg, lab_presets=None):
    """The seat labels for the lab this config points at.

    A custom host has no known seat list, so it returns an empty list and the
    launcher refuses to build one rather than inventing seats. When
    ``lab_presets`` is passed the seats come from the named preset; when it is
    None the labs defined in lab_info.json are used, so existing callers are
    unaffected.
    """
    lab = normalize_config(cfg)["lab"]
    if lab == LAB_CUSTOM:
        return []
    preset = find_lab_preset(lab, _presets_or_default(lab_presets))
    if preset is not None:
        return [str(s) for s in preset.get("seats", [])]
    return []


def resolve_seats(cfg, lab_presets=None):
    """The seat labels this config will hand to oTree, in order.

    Empty for None mode, and for file mode, where the user's own file is used
    as it stands and never re-written.
    """
    c = normalize_config(cfg)
    if c["seat_mode"] in (SEAT_NONE, SEAT_FILE):
        return []
    seats = lab_default_seats(c, lab_presets)
    if c["seat_mode"] == SEAT_EDIT:
        excluded = set(c["seat_excluded"])
        return [seat for seat in seats if seat not in excluded]
    return seats


def effective_seat_mode(cfg, lab_presets=None):
    """The seat mode a launch will REALLY use, after resolving empties to None.

    Seats are never a hard block (Julian): an absent seat file, a file that
    cannot be read or is empty, or a lab-default/edit selection that resolves to
    no seats (e.g. a custom host with no known seat list, or every seat unticked)
    all fall back to SEAT_NONE, a perfectly valid open-room launch with no seat
    board. A chosen file that DOES have labels stays SEAT_FILE (so its labels can
    still be validated), and a lab default with seats stays as chosen.
    """
    c = normalize_config(cfg)
    mode = c["seat_mode"]
    if mode == SEAT_NONE:
        return SEAT_NONE
    if mode == SEAT_FILE:
        path = c["seat_file"].strip()
        if not path or not os.path.isfile(path):
            return SEAT_NONE
        try:
            labels = read_seat_file(path)
        except OSError:
            return SEAT_NONE
        return SEAT_FILE if labels else SEAT_NONE
    # lab_default / edit: none when the resolved list is empty.
    return mode if resolve_seats(c, lab_presets) else SEAT_NONE


def invalid_seats(labels):
    """Labels oTree would reject (otree/common.py: validate_alphanumeric)."""
    return [label for label in labels if not SEAT_LABEL_RE.match(label)]


def seat_file_text(labels):
    """One label per line, with a trailing newline."""
    return "\n".join(labels) + "\n"


def seats_dir():
    return os.path.join(config_dir(), "seats")


def seat_file_path(config_name, room_name):
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", "%s_%s" % (room_name, config_name)).strip("_")
    return os.path.join(seats_dir(), (stem or "seats") + ".txt")


def write_seat_file(labels, path):
    """Write the seat list atomically, so a crash cannot leave half a list."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=folder, prefix=".seats-", suffix=".tmp", delete=False,
        newline="\n")
    tmp_name = handle.name
    try:
        handle.write(seat_file_text(labels))
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    except Exception:
        handle.close()
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def read_seat_file(path):
    """The labels in a seat file, parsed the way oTree parses it (str.split)."""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().split()


def seat_summary(cfg, resolved=None, lab_presets=None):
    """A short line describing the seat list, for the GUI and the log."""
    c = normalize_config(cfg)
    if c["seat_mode"] == SEAT_NONE:
        return "No seat list. The room stays open and shows no per-seat board."
    if c["seat_mode"] == SEAT_FILE:
        path = c["seat_file"].strip()
        if not path:
            return "No file chosen yet."
        if not os.path.isfile(path):
            return "File not found: %s" % path
        try:
            labels = read_seat_file(path)
        except OSError as error:
            return "Could not read %s: %s" % (path, error)
        return "%d seats from %s" % (len(labels), os.path.basename(path))
    labels = resolve_seats(c, lab_presets) if resolved is None else resolved
    if not labels:
        # No seats is not an error: it simply becomes the open (none) room.
        return "No seats: the room opens with no seat board (none)."
    return "%d seats" % len(labels)


def prepare_label_file(cfg, config_name="session", lab_presets=None):
    """Settle the participant label file for one run.

    Returns (path or None, explanation). A list the launcher owns is written to
    its own config directory; a file the user picked in their project is used
    exactly as it stands and is never copied or rewritten.
    """
    c = normalize_config(cfg)
    # Resolve empties (no file, unreadable/empty file, no default seats) to the
    # open (none) room rather than erroring: seats are never a hard block.
    mode = effective_seat_mode(c, lab_presets)
    if mode == SEAT_NONE:
        return None, "No participant list, so OTREE_LAB_LABEL_FILE is not set."
    if mode == SEAT_FILE:
        path = os.path.abspath(c["seat_file"].strip())
        return path, "Using the project's own seat file, unchanged: %s" % path
    labels = resolve_seats(c, lab_presets)
    path = seat_file_path(config_name, c["room_name"])
    write_seat_file(labels, path)
    return path, "Wrote %d seats to %s" % (len(labels), path)


def seat_preview(labels, limit=10):
    if not labels:
        return ""
    head = ", ".join(labels[:limit])
    if len(labels) > limit:
        head += ", ... , " + labels[-1]
    return head


# ---------------------------------------------------------------------------
# The settings.py block
# ---------------------------------------------------------------------------

BLOCK_MARKER = "=== oTree lab support (paste at the END of settings.py) ==="
BLOCK_END_MARKER = "=== end oTree lab support ==="

# The one source of truth for the block. otree_lab_block.py holds the same
# text; test_block_file_matches_the_constant proves they have not drifted.
LAB_BLOCK = '''# === oTree lab support (paste at the END of settings.py) ===
# ---------------------------------------------------------------------------
# OTREE LAB SUPPORT: appended by the oTree lab launcher.
# TO REMOVE: delete everything from this banner line to the END of the file.
# Safe to leave in permanently: it does NOTHING unless the launcher sets
# its environment variables at launch. With no lab environment set, every
# override below is skipped and your settings.py behaves exactly as before.
#
# Because Python binds names last, these assignments live at the END of the
# file, so they win over anything the project hardcoded higher up, but only
# while the launcher's variables are present. Each override is guarded by the
# variable it needs, and its comment says in plain language what it redirects
# and why. Everything here only redirects WHERE your program runs (the lab
# machines, the lab database, the lab room and login); it never changes your
# experiment's logic. Unrelated settings such as SECRET_KEY are left untouched.
# ---------------------------------------------------------------------------
import os as _os

# (a) ROOMS + participant_label_file: a lab launch always names the room it
#     opens, so make sure that room exists here. If the launcher also wrote a
#     seat list, point the room at it so the admin gets the per-seat presence
#     board; with no seat list the room is left OPEN (anyone joins). Adds
#     nothing off the lab, where OTREE_LAB_ROOM_NAME is unset.
if _os.environ.get("OTREE_LAB_ROOM_NAME"):
    # The launcher picked the room it will open for this run.
    _lab_room = _os.environ["OTREE_LAB_ROOM_NAME"]

    # ROOMS may not exist yet in this project.
    try:
        ROOMS
    except NameError:
        ROOMS = []

    # Add the room only if the project does not already define it, so a project
    # that has its own room keeps its own settings.
    if not any(r.get("name") == _lab_room for r in ROOMS):
        ROOMS = list(ROOMS) + [dict(name=_lab_room, display_name="oTree lab session")]

    # Point that room at the seat list ONLY when the launcher wrote one (seats).
    # Mutating in place means any other keys the project set on the room survive.
    # With no seat file the room stays open (no participant_label_file).
    if _os.environ.get("OTREE_LAB_LABEL_FILE"):
        for _room in ROOMS:
            if _room.get("name") == _lab_room:
                _room["participant_label_file"] = _os.environ["OTREE_LAB_LABEL_FILE"]

# (b) DATABASES: redirect the project at the lab's PostgreSQL database, rebuilt
#     from the DB_* variables the launcher set. Because it is assigned here at
#     the end of the file it wins even over a DATABASES block the project
#     hardcoded higher up, so a session cannot run against the wrong database.
#     (oTree 6+ actually chooses its database from the DATABASE_URL environment
#     variable, which the launcher also sets, so on that version the lab
#     database is already in force through the environment; this settings-level
#     DATABASES is the same redirect for Django-based oTree versions that read
#     it, and is simply ignored where it is not.)
if _os.environ.get("DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _os.environ["DB_NAME"],
            "USER": _os.environ.get("DB_USER", ""),
            "PASSWORD": _os.environ.get("DB_PASSWORD", ""),
            "HOST": _os.environ.get("DB_HOST", "localhost"),
            "PORT": _os.environ.get("DB_PORT", "5432"),
        }
    }

# (c) ADMIN_USERNAME: oTree reads the admin password from the environment but
#     hardcodes the admin username, so without this line the launcher's admin
#     username box would do nothing. Only fires when the launcher set
#     OTREE_ADMIN_USERNAME; off the lab the project's own username (or oTree's
#     default) is left exactly as it was.
if "OTREE_ADMIN_USERNAME" in _os.environ:
    ADMIN_USERNAME = _os.environ["OTREE_ADMIN_USERNAME"]

# (d) ADMIN_PASSWORD: take the admin password from the launcher, so a password
#     hardcoded in the project cannot lock the experimenter out of the lab
#     dashboard. Only fires when the launcher set OTREE_ADMIN_PASSWORD.
if "OTREE_ADMIN_PASSWORD" in _os.environ:
    ADMIN_PASSWORD = _os.environ["OTREE_ADMIN_PASSWORD"]

# (e) AUTH_LEVEL: the launcher's access level (STUDY puts the whole site behind
#     the admin login for a real session). Overrides any level the project
#     hardcoded. Only fires when the launcher set OTREE_AUTH_LEVEL.
if "OTREE_AUTH_LEVEL" in _os.environ:
    AUTH_LEVEL = _os.environ["OTREE_AUTH_LEVEL"]

# (f) DEBUG / production: re-derive oTree's own production rule from
#     OTREE_PRODUCTION, so a project that hardcoded DEBUG = True cannot ship
#     debug pages and tracebacks in the lab. Only fires when the launcher set
#     OTREE_PRODUCTION (production mode); off the lab, DEBUG is left as it was.
if "OTREE_PRODUCTION" in _os.environ:
    DEBUG = _os.environ.get("OTREE_PRODUCTION") in (None, "", "0")
# === end oTree lab support ===
'''


def settings_path_for(project_path):
    return os.path.join((project_path or "").strip(), "settings.py")


def _settings_lock_path(settings_path):
    """The launcher-owned lock file for serialising appends to one settings.py.

    PRINCIPLE 1: the launcher writes NO file into a researcher's project. The old
    ``settings.py.lock`` sat next to their settings.py and was never removed, so a
    project made lab-ready gained a stray file researchers would commit. This
    keeps the lock in the launcher's own ``data/locks/`` folder instead, keyed by
    a hash of the ABSOLUTE settings.py path so two clicks / two launchers on the
    same file still serialise, while nothing is written into the project (only the
    intended timestamped ``.bak`` is, on an explicit Add-block click).
    """
    digest = hashlib.sha1(os.path.abspath(settings_path).encode("utf-8", "surrogateescape")).hexdigest()
    folder = os.path.join(data_dir(), "locks")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, digest + ".lock")


def _normalize_block_text(text):
    """The lab block text with per-line trailing whitespace and surrounding blank
    lines dropped, so two copies that differ only in trailing whitespace / a
    trailing newline compare equal (used to spot a STALE, drifted block body)."""
    lines = [line.rstrip() for line in str(text).splitlines()]
    return "\n".join(lines).strip("\n")


def inspect_settings(project_path, lab_room=None):
    """Look for the lab support block in a project's settings.py.

    ``lab_room`` is this lab's default room (defaults to "study"); the room
    comparison (``own_room`` / ``uses_lab_room``) is made against IT, not a
    hardcoded "study", so a lab that runs a different room is judged correctly.

    Returns a dict:
      readable       could settings.py be read at all
      has_block      the start marker is present
      complete       both markers are present
      rooms_after    a top level `ROOMS =` line appears after the block
      rooms_line     the line number of that assignment, or 0
      own_room       the project itself defines a room named like the lab room
      uses_lab_room  the lab room will exist at launch (own room or a live block)
      stale          the block IS present but its body is an OLD version (differs
                     from the current core.LAB_BLOCK, e.g. the pre-no-seats-fix
                     block that guards the room on OTREE_LAB_LABEL_FILE); a
                     refresh_block() would bring it up to date
      needs_refresh  the block is present but outdated/cut-off, so refresh_block()
                     would fix it (stale, or complete markers missing)
      message        one line of plain language for the GUI
    """
    lab_room = sanitize_room_name(lab_room)
    result = {"readable": False, "has_block": False, "complete": False,
              "rooms_after": False, "rooms_line": 0, "path": settings_path_for(project_path),
              "own_room": False, "uses_lab_room": False, "stale": False,
              "needs_refresh": False, "lab_room": lab_room, "message": ""}
    try:
        with open(result["path"], "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        result["message"] = "No settings.py to check yet."
        return result

    result["readable"] = True
    lines = text.splitlines()
    start = end = -1
    for index, line in enumerate(lines):
        if BLOCK_MARKER in line and start < 0:
            start = index
        if BLOCK_END_MARKER in line:
            end = index
    result["has_block"] = start >= 0
    result["complete"] = start >= 0 and end > start

    if start >= 0:
        # A top level rebinding of ROOMS after the block silently throws the
        # room away. Indented ROOMS lines inside the block itself are fine.
        after = end if end > start else start
        for index in range(after + 1, len(lines)):
            if re.match(r"^ROOMS\s*=", lines[index]):
                result["rooms_after"] = True
                result["rooms_line"] = index + 1
                break

    # STALE: the block is complete but its body no longer matches the current
    # core.LAB_BLOCK (a drifted / pre-no-seats-fix copy). Compared with trailing
    # whitespace ignored so cosmetics never trip it. A refresh_block() replaces
    # it in place with the current block.
    if result["complete"]:
        region = "\n".join(lines[start:end + 1])
        result["stale"] = (_normalize_block_text(region)
                           != _normalize_block_text(LAB_BLOCK))
    result["needs_refresh"] = bool(result["has_block"]
                                   and (result["stale"] or not result["complete"]))

    # Does the project already wire up the lab's experimental room? Either the
    # lab support block does it, or the project defines a room named like ours
    # itself. The room name is matched ONLY within the project's own ROOMS
    # assignment region (from a top-level ``ROOMS =`` line down to the next
    # top-level statement), so an unrelated ``name='study'`` elsewhere (e.g. a
    # SESSION_CONFIGS entry) no longer counts as the project defining the room.
    # No top-level ROOMS assignment -> the project defines no rooms of its own.
    own_room = False
    rooms_start = None
    for index, line in enumerate(lines):
        if re.match(r"^ROOMS\s*=", line):
            rooms_start = index
            break
    if rooms_start is not None:
        region = [lines[rooms_start]]
        for line in lines[rooms_start + 1:]:
            # Stop at the next top-level statement (an unindented assignment or
            # keyword); the ROOMS list literal's own lines are indented or start
            # with a bracket, so they stay inside the region.
            if re.match(r"^[A-Za-z_]\w*\s*=", line) or \
                    re.match(r"^(def|class|if|for|while|with|try|import|from)\b", line):
                break
            region.append(line)
        own_room = bool(re.search(
            r"""name\s*=\s*['"]%s['"]""" % re.escape(lab_room), "\n".join(region)))
    result["own_room"] = own_room
    # A STALE block cannot be trusted to define the lab room at launch (the old
    # body guarded the room on the seat-file variable), so it does not count.
    result["uses_lab_room"] = (
        (result["complete"] and not result["rooms_after"] and not result["stale"])
        or own_room)

    if not result["has_block"]:
        result["message"] = ("settings.py does not have the oTree lab support block, so the "
                             "lab room and the seat board will not work.")
    elif result["rooms_after"]:
        result["message"] = ("settings.py assigns ROOMS on line %d, after the oTree lab support "
                             "block. That replaces the lab room. Move the block to the end of "
                             "the file." % result["rooms_line"])
    elif not result["complete"]:
        result["message"] = ("The oTree lab support block in settings.py looks cut off: its end "
                             "marker is missing.")
    elif result["stale"]:
        result["message"] = ("settings.py has an OUTDATED oTree lab support block. Refresh it so "
                             "it picks up the latest lab room and no-seats fixes.")
    else:
        result["message"] = "settings.py has the oTree lab support block."
    return result


class BlockAlreadyPresent(Exception):
    """append_block found the lab support block already in settings.py (checked
    under the lock) and refused to append it a second time."""


def append_block(project_path):
    """Atomically back up settings.py and append the lab support block.

    This is the ONE sanctioned mutation of a researcher's own code, so it is a
    single lock-guarded operation rather than inspect-then-append:

      1. take an exclusive lock on a sibling lock file (serialises two clicks /
         two launchers / an editor race),
      2. RE-READ settings.py and re-check the marker under the lock -- if the
         block is already there raise :class:`BlockAlreadyPresent` (never a
         double append),
      3. copy the current file to a UNIQUE timestamped ``.bak`` (microseconds, so
         two appends can never collide on the one backup name),
      4. write settings + block to a same-dir temp file, fsync it, and
         ``os.replace`` it into place (so a crash mid-write cannot corrupt the
         researcher's settings.py).

    Returns ``(backup, settings_path)`` -- the same shape callers/tests expect.
    Raises :class:`BlockAlreadyPresent` if the block is already present, or
    OSError on a read/write problem.
    """
    path = settings_path_for(project_path)
    folder = os.path.dirname(path) or "."
    with exclusive_file_lock(_settings_lock_path(path)):
        # surrogateescape so a settings.py with a non-UTF-8 byte (a Latin-1
        # comment) round-trips byte-for-byte instead of raising UnicodeDecodeError
        # (which is not OSError, so it used to escape as a generic failure).
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            text = handle.read()
        # Re-check the marker under the lock: refuse a second append even if
        # another appender slipped in between our caller's check and here.
        if BLOCK_MARKER in text:
            raise BlockAlreadyPresent(
                "settings.py already has the oTree lab support block.")
        # A microsecond-stamped backup name, bumped in the unlikely event of a
        # same-microsecond collision, so the first backup is never clobbered.
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = path + "." + stamp + ".bak"
        while os.path.exists(backup):
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = path + "." + stamp + ".bak"
        shutil.copy2(path, backup)
        separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        new_text = text + separator + LAB_BLOCK
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", errors="surrogateescape", dir=folder,
            prefix=".settings-", suffix=".tmp", delete=False
        )
        tmp_name = handle.name
        try:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            # Keep the researcher's original file mode on the replacement.
            try:
                os.chmod(tmp_name, stat.S_IMODE(os.stat(path).st_mode))
            except OSError:
                pass
            os.replace(tmp_name, path)
        except Exception:
            handle.close()
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    return backup, path


def _write_settings_atomically(path, folder, new_text):
    """Fsync ``new_text`` to a same-dir temp file and os.replace it onto ``path``,
    preserving the original file mode. Shared by append_block/refresh_block.
    surrogateescape on write so a settings.py carrying non-UTF-8 bytes round-trips
    unchanged (matches the surrogateescape reads in append_block/refresh_block)."""
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", errors="surrogateescape", dir=folder,
        prefix=".settings-", suffix=".tmp", delete=False
    )
    tmp_name = handle.name
    try:
        handle.write(new_text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        try:
            os.chmod(tmp_name, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass
        os.replace(tmp_name, path)
    except Exception:
        handle.close()
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def refresh_block(project_path):
    """Atomically replace an OUTDATED lab support block with the current one.

    ``append_block`` refuses (``BlockAlreadyPresent``) when a marker is present,
    so it cannot fix a project stuck on an old block (e.g. Micro 1 on the
    pre-no-seats-fix body). This is the sanctioned refresh: under the SAME
    exclusive lock + timestamped ``.bak`` + temp-file/fsync/os.replace machinery
    as :func:`append_block`, it removes the existing block region -- from the
    ``BLOCK_MARKER`` banner line to the ``BLOCK_END_MARKER`` line (or to EOF when
    the end marker is missing, matching the block's own "delete from this banner
    to the END of the file" contract) -- and appends the current
    :data:`LAB_BLOCK`. One atomic op, fully revertible from the backup.

    When no block is present it simply appends the current block (so it is safe to
    call in either state). Returns ``(backup, settings_path)``; raises OSError on a
    read/write problem.
    """
    path = settings_path_for(project_path)
    folder = os.path.dirname(path) or "."
    with exclusive_file_lock(_settings_lock_path(path)):
        # surrogateescape (see append_block): preserve any non-UTF-8 bytes rather
        # than raising UnicodeDecodeError.
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            text = handle.read()
        lines = text.splitlines(keepends=True)
        start = end = -1
        for index, line in enumerate(lines):
            if BLOCK_MARKER in line and start < 0:
                start = index
            if BLOCK_END_MARKER in line:
                end = index
        # A microsecond-stamped, collision-proof backup name (as append_block).
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = path + "." + stamp + ".bak"
        while os.path.exists(backup):
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = path + "." + stamp + ".bak"
        shutil.copy2(path, backup)
        if start >= 0:
            # Remove the old region: banner..end-marker inclusive, or banner..EOF
            # when the end marker is missing (the block's documented removal span).
            last = end if end >= start else len(lines) - 1
            kept = "".join(lines[:start] + lines[last + 1:])
        else:
            kept = text
        kept = kept.rstrip("\n")
        separator = "" if not kept else "\n\n"
        new_text = kept + separator + LAB_BLOCK
        _write_settings_atomically(path, folder, new_text)
    return backup, path


# ---------------------------------------------------------------------------
# Project folder validation
# ---------------------------------------------------------------------------


# The empty project state. With a GitHub organisation set (the clone button is
# shown, ``github_clone_enabled``) it also points at that button.
NO_PROJECT_TEXT = "No project folder chosen yet. Click Browse to pick one."
NO_PROJECT_GITHUB_TEXT = ("No project folder chosen yet. Click Browse to pick one, "
                          "or download one from your GitHub organisation.")


def no_project_message(clone_enabled=None):
    """The empty project state line. ``clone_enabled`` None = read the GitHub
    setting (fail-soft: unreadable = no organisation)."""
    if clone_enabled is None:
        try:
            clone_enabled = github_clone_enabled()
        except Exception:
            clone_enabled = False
    return NO_PROJECT_GITHUB_TEXT if clone_enabled else NO_PROJECT_TEXT


def validate_project(path, clone_enabled=None):
    """Check that a folder looks like an oTree project.

    Returns (level, message) where level is "ok", "warn" or "error".
    A missing folder is an error and blocks launching; anything else only warns.
    ``clone_enabled`` only shapes the empty-path message (see
    ``no_project_message``).
    """
    path = (path or "").strip()
    if not path:
        return "warn", no_project_message(clone_enabled)
    if not os.path.isdir(path):
        return "error", "Folder not found: " + path
    if not os.path.isfile(os.path.join(path, "settings.py")):
        return (
            "warn",
            "No settings.py in this folder, so it may not be an oTree project. "
            "You can still launch.",
        )
    apps = find_app_packages(path)
    if not apps:
        return (
            "warn",
            "settings.py found, but no app package (a folder with __init__.py) "
            "next to it. You can still launch.",
        )
    listed = ", ".join(apps[:4]) + ("..." if len(apps) > 4 else "")
    return "ok", "Looks like an oTree project: settings.py and %d app package%s (%s)." % (
        len(apps),
        "" if len(apps) == 1 else "s",
        listed,
    )


def find_app_packages(path):
    apps = []
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        return apps
    for name in entries:
        if name.startswith(".") or name in ("__pycache__", "_static", "_templates"):
            continue
        folder = os.path.join(path, name)
        if os.path.isdir(folder) and os.path.isfile(os.path.join(folder, "__init__.py")):
            apps.append(name)
    return apps


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def config_dir():
    """The directory where saved_configs.json and seats/ live: the data/ folder."""
    return data_dir()


def saved_configs_path():
    """Where saved_configs.json lives. OTREE_LAB_LAUNCHER_PRESETS overrides it
    (the historical variable name, kept so existing shortcuts/tests work)."""
    override = os.environ.get("OTREE_LAB_LAUNCHER_PRESETS")
    if override:
        return override
    return os.path.join(config_dir(), SAVED_CONFIGS_FILENAME)


# Back-compat alias: both faces call presets_path() for the config store.
presets_path = saved_configs_path


# ---------------------------------------------------------------------------
# machine.json (THIS PC only, never copied between PCs). Schema 1:
#   {schema_version, home_lab, shown_labs: [lab ids], theme,
#    pg_admin: {admin_username, admin_password, admin_host, admin_port},
#    databases: [{id, title, researcher, postgres_user, db_name, db_user,
#                 db_password, db_host, db_port, created, deleted,
#                 created_on: {hostname, ip, home_lab, when}}],
#    default_database, migrations: {...}, notices: [...]}
# Every write is a locked read-modify-write (update_machine) that only touches
# the keys it owns, so the theme, the home lab and the database list can be saved
# from different places without clobbering each other.
# ---------------------------------------------------------------------------


def machine_path():
    """Where machine.json lives. OTREE_LAB_MACHINE overrides it."""
    override = os.environ.get("OTREE_LAB_MACHINE")
    if override:
        return override
    return os.path.join(data_dir(), MACHINE_FILENAME)


def default_machine():
    """An empty machine.json (a PC that has not run the setup wizard yet)."""
    return {"schema_version": SCHEMA_VERSION, "home_lab": "", "shown_labs": None,
            "theme": "dark", "pg_admin": {}, "databases": [], "default_database": "",
            "migrations": {}, "notices": []}


def normalize_machine(raw):
    """A machine dict with every known key present and well-typed. Unknown keys
    (a newer launcher's) are kept."""
    out = default_machine()
    raw = raw if isinstance(raw, dict) else {}
    for key, value in raw.items():
        if key not in out:
            out[key] = value
    out["home_lab"] = str(raw.get("home_lab", "") or "").strip().lower()
    shown = raw.get("shown_labs")
    out["shown_labs"] = ([str(s).strip() for s in shown if str(s).strip()]
                         if isinstance(shown, list) else None)
    out["theme"] = normalize_theme(raw.get("theme"))
    admin = raw.get("pg_admin")
    out["pg_admin"] = ({k: str(admin.get(k, "") or "") for k in PG_ADMIN_KEYS}
                       if isinstance(admin, dict) and admin else {})
    dbs = raw.get("databases")
    out["databases"] = [normalize_database_entry(d) for d in dbs
                        if isinstance(d, dict)] if isinstance(dbs, list) else []
    out["default_database"] = str(raw.get("default_database", "") or "").strip()
    out["migrations"] = dict(raw.get("migrations") or {}) \
        if isinstance(raw.get("migrations"), dict) else {}
    out["notices"] = [n for n in (raw.get("notices") or []) if isinstance(n, dict)] \
        if isinstance(raw.get("notices"), list) else []
    out["schema_version"] = max(SCHEMA_VERSION, schema_version_of(raw))
    return out


def load_machine(path=None):
    """This PC's machine.json as a normalized dict (never None).

    Absent: for the default location with old (v0) files still in place (the
    migration could not run), an in-memory conversion of them (fail-soft);
    otherwise an empty machine (the setup wizard fills it)."""
    default = path is None
    path = path or machine_path()
    status, data = _read_json(path)
    if status != "ok" or not isinstance(data, dict):
        if default:
            legacy = _legacy_view()
            if legacy is not None:
                return normalize_machine(copy.deepcopy(legacy["machine"]))
        return default_machine()
    return normalize_machine(data)


def save_machine(machine, path=None):
    """Write machine.json (schema 1) atomically, owner-only. Refuses a newer
    file. Returns the path."""
    path = path or machine_path()
    data = normalize_machine(machine)
    data["schema_version"] = SCHEMA_VERSION
    write_json_atomic(path, data, prefix=".machine-")
    return path


def update_machine(mutate, path=None):
    """Locked read-modify-write of machine.json: ``mutate(machine)`` changes the
    dict in place (and may return a value, which is returned). The live
    :data:`MACHINE` is refreshed afterwards."""
    global MACHINE
    target = path or machine_path()
    with exclusive_file_lock(lock_path("machine", os.path.dirname(os.path.abspath(target)))):
        machine = load_machine(path)
        result = mutate(machine)
        save_machine(machine, target)
    if path is None or os.path.abspath(target) == os.path.abspath(machine_path()):
        MACHINE = load_machine()
    return result


def current_pc_stamp(home_lab=None, when=None):
    """``{hostname, ip, home_lab, when}`` describing THIS computer now: recorded as
    a database's ``created_on``. The IP is best effort ("" when unknown); nothing
    is sent over the network."""
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    return {"hostname": hostname, "ip": _local_ip(),
            "home_lab": str(home_lab if home_lab is not None else
                            (MACHINE.get("home_lab") or "")),
            "when": when or now_iso()}


def _local_ip():
    """This computer's LAN address, best effort, "" when unknown. A UDP
    "connect" only picks a route; no packet is sent."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))
            ip = probe.getsockname()[0]
        finally:
            probe.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        return "" if ip.startswith("127.") else ip
    except Exception:
        return ""


# --- Theme (machine.json) ----------------------------------------------------
# The light/dark theme is a per-PC preference kept in machine.json (it used to
# be ui_prefs.json, and the web page also kept a copy in localStorage; both are
# gone). Fail-soft: a missing file yields "dark" and a write error is swallowed.


def normalize_theme(theme):
    """Coerce any input to a known theme name. 'light' only when explicitly asked;
    everything else (incl. None/garbage) is the 'dark' default."""
    return "light" if str(theme or "").strip().lower() == "light" else "dark"


def load_ui_theme(path=None):
    """The saved light/dark theme, defaulting to 'dark' when nothing is stored."""
    return normalize_theme(load_machine(path).get("theme"))


def save_ui_theme(theme, path=None):
    """Persist the light/dark theme into machine.json. Returns the normalised
    theme. Never raises (the UI has already updated in place)."""
    value = normalize_theme(theme)
    try:
        update_machine(lambda m: m.__setitem__("theme", value), path)
    except Exception:
        pass
    return value


# --- GitHub Organisation Sync opt-in (a LAB setting, in lab_info.json) ------
# An OPT-IN, DEFAULT-OFF feature that wires the launcher into a lab's read-only
# GitHub organisation git setup so an experimenter can clone/update a study repo
# without touching a terminal. It works ONLY when the lab manager has already set
# up git and a read-only GitHub organisation credential ON THIS lab experimenter
# PC. The launcher never stores or handles any token itself. Two things persist,
# as top-level keys of lab_info.json (so they travel with the lab file): the
# tick-box state and the organisation name (the clone target is <org>/<repo>).
# Both are fail-soft: a missing or corrupt file yields OFF and an empty org. A
# save never CREATES lab_info.json (that would skip the setup wizard); the old
# ui_prefs.json copies are moved into lab_info.json by the 1.5.0 migration.
GITHUB_SYNC_ENABLED_KEY = "github_sync_enabled"
GITHUB_ORG_KEY = "github_org"
_GITHUB_SYNC_KEYS = (GITHUB_SYNC_ENABLED_KEY, GITHUB_ORG_KEY)


def _github_sync_values(source):
    source = source or {}
    return {"enabled": bool(source.get(GITHUB_SYNC_ENABLED_KEY, False)),
            "org": str(source.get(GITHUB_ORG_KEY, "") or "").strip()}


def _set_lab_info_keys(values, path=None):
    """Store top-level keys in lab_info.json (other keys kept) and refresh the
    live LAB_INFO. False when there is no lab_info.json (nothing is created)."""
    target = path or lab_info_path()
    info = load_lab_info(path)
    if info is None:
        return False
    info.update(values)
    save_lab_info(info, target)
    if path is None or os.path.abspath(target) == os.path.abspath(lab_info_path()):
        if isinstance(LAB_INFO, dict):
            LAB_INFO.update(values)
    return True


def load_github_sync(path=None, prefs_path=None):
    """The saved GitHub Organisation Sync settings as ``{"enabled", "org"}``.
    Default OFF with an empty org (fail-soft). ``prefs_path`` is ignored (kept
    for old callers; ui_prefs.json is gone)."""
    return _github_sync_values(load_lab_info(path))


def load_github_sync_enabled(path=None, prefs_path=None):
    """The saved GitHub Organisation Sync opt-in flag. Default OFF (False)."""
    return load_github_sync(path)["enabled"]


def load_github_org(path=None, prefs_path=None):
    """The saved GitHub organisation name, or '' when nothing is stored."""
    return load_github_sync(path)["org"]


def save_github_sync_settings(enabled, org, path=None, prefs_path=None):
    """Persist the GitHub Organisation Sync flag AND organisation name into
    lab_info.json, keeping every other key. With no lab_info.json nothing is
    written. Returns the ``{"enabled", "org"}`` just chosen. Never raises."""
    values = {GITHUB_SYNC_ENABLED_KEY: bool(enabled),
              GITHUB_ORG_KEY: str(org or "").strip()}
    try:
        _set_lab_info_keys(values, path)
    except Exception:
        pass  # fail-soft: the on-screen state already changed
    return _github_sync_values(values)


# --- Databases on this computer only (lab setting, v1.4.15) -----------------
# A lab-level tick box in Lab Settings (Custom databases), stored as a top-level
# key of lab_info.json like github_sync_enabled. DEFAULT ON: a missing key (every
# existing install) or no readable lab_info.json reads as on. When on, a new or
# edited database host is locked to localhost; when off, a database on another
# host can be registered (the 1.4.13 behaviour). Saving never creates
# lab_info.json (that would skip the first-run wizard).
DB_LOCALHOST_ONLY_KEY = "databases_localhost_only"
LOCALHOST_ONLY_LABEL = "Databases on this computer only (localhost)"


def load_databases_localhost_only(path=None):
    """The saved "Databases on this computer only" lab setting. Default ON."""
    info = load_lab_info(path)
    if not isinstance(info, dict) or DB_LOCALHOST_ONLY_KEY not in info:
        return True
    return bool(info.get(DB_LOCALHOST_ONLY_KEY))


def save_databases_localhost_only(enabled, path=None):
    """Store the setting in lab_info.json (other keys kept) and return the value
    now in effect. With no readable lab_info.json nothing is written and the
    default (on) stays in effect. Never raises."""
    try:
        if not _set_lab_info_keys({DB_LOCALHOST_ONLY_KEY: bool(enabled)}, path):
            return True
    except Exception:
        return load_databases_localhost_only(path)
    return bool(enabled)


def database_host_refusal(host, localhost_only=None):
    """Why ``host`` may not be used for a new or edited database, or "".

    Only refuses a non-local host while the localhost-only lab setting is on
    (``localhost_only`` None reads the saved setting)."""
    if localhost_only is None:
        localhost_only = load_databases_localhost_only()
    if not localhost_only or is_local_host(host):
        return ""
    return ("%s is another computer. \"%s\" is on in Lab Settings (Custom "
            "databases), so databases must be on this computer. Untick it to use "
            "a database on another host." % (str(host).strip(), LOCALHOST_ONLY_LABEL))


def new_database_host_defaults(admin, localhost_only=None):
    """The Host/Port a new-database dialog opens with, and whether its pen may
    unlock them: localhost + no pen while localhost-only is on, else the Lab
    Settings admin host/port behind the pen."""
    if localhost_only is None:
        localhost_only = load_databases_localhost_only()
    admin = admin or {}
    host = str(admin.get("admin_host", "") or "").strip() or "localhost"
    port = str(admin.get("admin_port", "") or "").strip() or "5432"
    if localhost_only:
        return {"host": "localhost",
                "port": port if is_local_host(host) else "5432",
                "editable": False}
    return {"host": host, "port": port, "editable": True}


# --- This PC's lab (machine.json home_lab) ----------------------------------
# Which lab this computer is. Chosen in step 1 of the setup wizard (or Lab
# Settings "which lab is this computer"), stored as machine.json ``home_lab``
# (1.5.0; it used to be the one-word lab.local file). It drives the generated Lab
# default (its lab + that lab's default room) and is always shown in the lab
# selector. The function names below are kept from the lab.local days so both
# faces keep calling the same API.


def lab_marker_path():
    """Back-compat: the file that records this PC's lab (machine.json now)."""
    return machine_path()


def read_lab_marker(path=None):
    """This PC's lab id (machine.json ``home_lab``), or None when unset."""
    word = str(load_machine(path).get("home_lab") or "").strip().lower()
    return word or None


def set_lab_marker(lab_id, path=None):
    """Record this PC's lab, OVERWRITING any earlier choice (the Lab Settings
    change path). The lab is added to the shown labs. Returns the id written."""
    lab_id = str(lab_id or "").strip().lower()
    if not lab_id:
        raise ValueError("lab_id must be a non-empty lab id")

    def _apply(machine):
        machine["home_lab"] = lab_id
        shown = machine.get("shown_labs")
        if isinstance(shown, list) and lab_id not in shown:
            shown.append(lab_id)

    update_machine(_apply, path)
    return lab_id


def write_lab_marker(lab, path=None):
    """First-run write: record this PC's lab only if none is set yet. Returns
    True if written, False if refused (use :func:`set_lab_marker` to change)."""
    lab = str(lab or "").strip().lower()
    if not lab:
        raise ValueError("lab must be a non-empty lab id")
    if read_lab_marker(path) is not None:
        return False
    set_lab_marker(lab, path)
    return True


_MARKER_UNSET = object()


def apply_lab_marker(presets, marker=_MARKER_UNSET, lab_presets=None):
    """Point the generated Lab default's lab at this PC's lab, in place.

    User configs are never touched. A no-op when this PC has no lab yet. When
    ``lab_presets`` is given the default's ``room_name`` is re-derived from that
    lab's ``default_room`` (so a room edited in Lab Settings updates the Lab
    default at once)."""
    if marker is _MARKER_UNSET:
        marker = read_lab_marker()
    if not marker:
        return presets
    room = lab_default_room(lab_presets, marker) if lab_presets is not None else None
    for preset in presets:
        if is_builtin(preset):
            preset["lab"] = marker
            if room:
                preset["room_name"] = room
    return presets


def apply_lab_identity(lab_presets, lab_id):
    """Make ``lab_id`` this PC's single shown lab: show only it, hide the rest.

    Only the per-PC display flags change (they are saved as machine.json
    ``shown_labs``, never into the shared lab list). Returns (ok, message,
    new_list); refuses when no lab carries that id."""
    presets = [normalize_lab_preset(p) for p in (lab_presets or [])]
    if find_lab_preset(lab_id, presets) is None:
        return False, "No lab preset with that id.", presets
    for preset in presets:
        preset["display"] = (preset["id"] == str(lab_id))
    return True, "", presets


def default_preset():
    """The built-in "Lab default" config, GENERATED (1.5.0: never stored).

    Built from the code defaults + lab_info.json (this PC's lab's default room,
    the lab's default oTree admin login) + machine.json (this PC's lab, its
    default database). It follows the PC default database (``database_id`` "")
    when this PC has one, else oTree's own SQLite."""
    preset = dict(DEFAULT_CONFIG)
    preset["seat_excluded"] = []
    preset["name"] = "Lab default"
    preset["created"] = now_iso()
    preset["last_run"] = None
    # author/builtin are metadata (like name/created/last_run), NOT config fields
    # in FIELD_KEYS, so they never enter the config-equality comparison.
    preset["author"] = "builtin"
    preset["builtin"] = True
    preset["project_path"] = ""
    preset["database_id"] = "" if _DEFAULT_DB_ID in _DB_REGISTRY else DB_BUILTIN_SQLITE
    preset["db_mode"] = DB_MODE_LAB if _DEFAULT_DB_ID in _DB_REGISTRY else DB_MODE_NONE
    preset["admin_username"] = DEFAULT_ADMIN_USERNAME
    preset["admin_password"] = DEFAULT_ADMIN_PASSWORD
    marker = read_lab_marker()
    lab = marker or DEFAULT_LAB_ID
    preset["lab"] = lab
    preset["room_name"] = lab_default_room(default_lab_presets(), lab)
    return preset


def clear_builtin_last_run(presets):
    """Force the built-in Lab default's ``last_run`` to None, in place (Job 2).

    The built-in "Lab default" is a launch TEMPLATE you launch FROM, never a
    saved config, so it must NEVER record a run. This clears any ``last_run`` a
    previous version stamped onto it (Pass 9 over-corrected and stamped it) so a
    stored stamp is wiped on load. Idempotent; user configs are untouched.
    Returns ``presets`` for chaining.
    """
    for preset in presets:
        if is_builtin(preset):
            preset["last_run"] = None
    return presets


def clear_builtin_project_path(presets):
    """Force the built-in Lab default's ``project_path`` to "" , in place.

    The built-in "Lab default" is a launch TEMPLATE, not a saved study, so it
    must ALWAYS open Browse-first with no project folder set: the launcher opens
    selected on it every start (see :func:`select_on_open`) and lab staff Browse
    to the study of the day from there. This wipes any project folder a previous
    version (or a stray edit) may have stamped onto the built-in so a stored path
    can never survive across a restart. Idempotent; user configs are untouched.
    Returns ``presets`` for chaining.
    """
    for preset in presets:
        if is_builtin(preset):
            preset["project_path"] = ""
    return presets


#
# THE STORE FACADE. Both faces work on ``(presets, extra)``: ``presets`` is the
# config list (the generated Lab default first) and ``extra`` a dict of the
# global collections. Behind it sit three files:
#   saved_configs.json  the configs + researchers + last_author (+ unknown keys)
#   machine.json        pg_admin, databases, default_database, shown_labs
#   lab_info.json       the labs (``extra["lab_presets"]``, a composite view)
# load_store composes ``extra``; save_store splits it back and writes each file
# only when its part changed, merging into what is on disk.
_MACHINE_EXTRA_KEYS = ("pg_admin", "databases", "default_database")
_LAB_EXTRA_KEY = "lab_presets"
_NOT_SAVED_KEYS = ("configs", "presets", "version", "schema_version", "last_project",
                   _LAB_EXTRA_KEY) + _MACHINE_EXTRA_KEYS
# Config fields DERIVED from database_id (never stored).
CONFIG_DB_FIELDS = ("db_mode", "db_name", "db_user", "db_password", "db_host", "db_port")


def config_for_storage(preset):
    """A config as saved_configs.json stores it: the database is a REFERENCE
    (``database_id``), the derived db_* fields are dropped, everything else --
    its own oTree admin login included -- is kept."""
    preset = dict(preset or {})
    n = normalize_config(preset)
    out = {k: v for k, v in preset.items() if k not in CONFIG_DB_FIELDS}
    db_id = str(preset.get("database_id", "") or "").strip()
    if db_id:
        out["database_id"] = db_id
    elif n["db_mode"] == DB_MODE_NONE:
        out["database_id"] = DB_BUILTIN_SQLITE
    elif n["db_mode"] == DB_MODE_LAB:
        out["database_id"] = _DEFAULT_DB_ID if _DEFAULT_DB_ID in _DB_REGISTRY \
            else DB_BUILTIN_SQLITE
    else:
        match = find_database_by_connection(n)
        if match is not None:
            out["database_id"] = match["id"]
        else:
            # A connection no database on this PC matches (an old in-memory
            # config): keep it inline rather than lose it.
            out["database_id"] = ""
            for key in CONFIG_DB_FIELDS:
                out[key] = n[key]
    return out


def load_store(path=None, default_factory=None):
    """Read the config store. Returns ``(presets, extra)``.

    ``presets`` = the generated Lab default (``default_factory``, default
    :func:`default_preset`) followed by the saved configs exactly as stored
    (unknown keys kept; the database is an id, resolved by normalize_config).
    ``extra`` = saved_configs.json's other keys + this PC's ``pg_admin``,
    ``databases``, ``default_database`` (machine.json) + ``lab_presets`` (the labs
    of lab_info.json with this PC's shown flags). A file that cannot be parsed is
    copied aside (``.broken-<stamp>``) rather than overwritten. An old-layout file
    (presets.json content) is read too."""
    make_default = default_factory or default_preset
    default = path is None
    path = path or saved_configs_path()
    status, data = _read_json(path)
    if status == "missing" and default:
        legacy = _legacy_view()
        if legacy is not None:
            data, status = copy.deepcopy(legacy["saved"]), "ok"
    elif status == "malformed":
        backup = path + ".broken-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        data = None
    saved_extra = {}
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("configs")
        if not isinstance(raw, list):
            raw = data.get("presets", [])
        saved_extra = {k: v for k, v in data.items() if k not in _NOT_SAVED_KEYS}
    else:
        raw = []
    configs = [dict(item) for item in (raw if isinstance(raw, list) else [])
               if isinstance(item, dict) and not is_builtin(item)]
    # A record without a usable name still belongs to somebody, so keep it
    # rather than dropping it silently.
    for index, item in enumerate(configs):
        if not str(item.get("name", "")).strip():
            item["name"] = "Unnamed config %d" % (index + 1)
    extra = dict(saved_extra)
    machine = load_machine()
    extra["pg_admin"] = dict(machine.get("pg_admin") or {})
    extra["databases"] = copy.deepcopy(machine.get("databases") or [])
    extra["default_database"] = machine.get("default_database", "")
    extra[_LAB_EXTRA_KEY] = default_lab_presets()
    return [make_default()] + configs, extra


def save_store(presets, extra=None, path=None):
    """Write the store atomically, split over its three files.

    saved_configs.json always (the configs, never a built-in, each with its
    database as an id); machine.json and lab_info.json only when the part of
    ``extra`` they own changed. Each write is atomic (temp file + os.replace),
    owner-only, refuses a newer-schema file and holds a cross-process lock in
    data/locks/ (removed after use). Afterwards the live lab/machine state is
    refreshed from disk."""
    extra = dict(extra or {})
    path = path or saved_configs_path()
    folder = os.path.dirname(os.path.abspath(path))
    payload = {"schema_version": SCHEMA_VERSION,
               "configs": [config_for_storage(p) for p in (presets or [])
                           if not is_builtin(p)]}
    for key, value in extra.items():
        if key not in _NOT_SAVED_KEYS:
            payload[key] = value
    with exclusive_file_lock(lock_path("saved_configs", folder)):
        write_json_atomic(path, payload, prefix=".saved_configs-")
    _save_machine_part(extra)
    _save_lab_part(extra)
    _refresh_live_state()
    return path


def _save_machine_part(extra):
    """Write pg_admin / databases / default_database / shown_labs to machine.json
    when they differ from what is on disk."""
    updates = {}
    for key in _MACHINE_EXTRA_KEYS:
        if key in extra:
            updates[key] = copy.deepcopy(extra[key])
    lab_presets = extra.get(_LAB_EXTRA_KEY)
    if isinstance(lab_presets, list) and lab_presets:
        updates["shown_labs"] = [p["id"] for p in (normalize_lab_preset(x) for x in lab_presets)
                                 if p["display"] and not p["deleted"] and p["id"]]
    if not updates:
        return
    current = load_machine()
    candidate = normalize_machine(dict(current, **updates))
    if "shown_labs" in updates and current.get("shown_labs") is None:
        visible = [l["id"] for l in lab_info_labs() if not l.get("deleted")]
        if sorted(candidate["shown_labs"] or []) == sorted(visible):
            candidate["shown_labs"] = None   # still "all shown": keep it open-ended
    if all(candidate.get(k) == current.get(k) for k in updates):
        return

    def _apply(machine):
        for key in updates:
            machine[key] = candidate[key]

    update_machine(_apply)


def _save_lab_part(extra):
    """Write the labs of ``extra["lab_presets"]`` into lab_info.json when they
    differ from what is on disk (never creates lab_info.json for no labs)."""
    lab_presets = extra.get(_LAB_EXTRA_KEY)
    if not isinstance(lab_presets, list) or not lab_presets:
        return
    info = load_lab_info()
    labs, maps = stored_labs_from_presets(lab_presets, info)
    if info is not None and labs == lab_info_labs(info) and maps == lab_info_maps(info):
        return
    info = dict(info or {})
    info["labs"] = labs
    info["maps"] = maps
    save_lab_info(info)


def stored_labs_from_presets(lab_presets, info=None):
    """``(labs, maps)`` for lab_info.json from a composite lab-preset list.

    Every lab keeps its map NAME (``map_name``); a lab that only carries a map
    object gets it added to the maps table under its id. Labs present in
    ``info`` but missing from ``lab_presets`` (another process added them) are
    kept, so nothing is lost; removing a lab is a soft delete (``deleted``)."""
    existing = {l["id"]: l for l in lab_info_labs(info or {})}
    maps = lab_info_maps(info or {})
    out = []
    seen = set()
    for raw in lab_presets or []:
        p = normalize_lab_preset(raw)
        if not p["id"] or p["id"] in seen:
            continue
        base = existing.get(p["id"], {})
        map_name = str(p.get("map_name") or "").strip() or base.get("map", "")
        if isinstance(p.get("map"), dict):
            if not map_name:
                map_name = _unique_key(p["id"], maps)
            if not isinstance(maps.get(map_name), dict):
                maps[map_name] = p["map"]
        entry = {"id": p["id"], "name": p["name"], "host": p["ip"], "seats": p["seats"],
                 "geometry": p["geometry"], "cols": p["cols"], "map": map_name,
                 "default_room": p["default_room"], "shortcut_label": p["shortcut_label"],
                 "deleted": p["deleted"]}
        suggested = p.get("suggested_database") or base.get("suggested_database")
        if suggested:
            entry["suggested_database"] = suggested
        out.append(normalize_stored_lab(entry))
        seen.add(p["id"])
    for lab_id, entry in existing.items():
        if lab_id not in seen:
            out.append(entry)
    return out, maps


def _refresh_live_state():
    """Re-read lab_info.json + machine.json into the live module state."""
    reload_lab_info()


def merge_store_from_disk(mem_presets, mem_extra, disk_presets, disk_extra):
    """Merge a freshly re-read on-disk store into the in-memory one for the
    cross-process lost-update guard (both faces' ``_mutate_store``).

    When ANOTHER process (e.g. the headless one-click shortcut stamping
    ``last_run`` while the GUI is open) has written presets.json since this
    process last saved, each face re-loads the store and calls this before
    re-applying its own pending mutation, so the other process's change is not
    blindly overwritten by this process's stale in-memory snapshot.

    The IDENTITY of existing in-memory preset dicts (matched by name) is
    preserved -- their contents are replaced in place with the disk version -- so
    a mutation callback that captured a specific preset object (a ``last_run``
    stamp, a delete-by-identity) still targets a live object in the returned list.
    In-memory presets not on disk (e.g. one just appended but not yet saved) are
    kept. Disk wins for ``extra`` keys; the caller's pending mutation runs
    afterwards and still has the last word on whatever it touches. Returns
    ``(presets, extra)``.
    """
    by_name = {}
    for preset in mem_presets:
        by_name.setdefault(str(preset.get("name", "")), preset)
    out = []
    seen = set()
    for disk in disk_presets:
        name = str(disk.get("name", ""))
        if name in seen:
            out.append(disk)
            continue
        obj = by_name.get(name)
        if obj is not None:
            obj.clear()
            obj.update(disk)
            out.append(obj)
        else:
            out.append(disk)
        seen.add(name)
    for preset in mem_presets:
        if str(preset.get("name", "")) not in seen:
            out.append(preset)
            seen.add(str(preset.get("name", "")))
    merged_extra = dict(mem_extra or {})
    merged_extra.update(disk_extra or {})
    return out, merged_extra


def is_builtin(item):
    """True for the app-owned Lab default (builtin flag, or author == "builtin").

    These configs are pinned to the top and cannot be deleted.
    """
    return bool(item.get("builtin")) or str(item.get("author", "")).strip().casefold() == "builtin"


def lab_suffix(lab):
    """The derived, never-stored name suffix for a config's lab.

    Uses the lab preset's display name from lab_info.json, so the suffix works
    for any lab, not just the two that used to be hardcoded.
    """
    lab = str(lab or "").strip()
    if not lab or lab == LAB_CUSTOM:
        return ""
    preset = find_lab_preset(lab, default_lab_presets())
    if preset is not None and preset.get("name"):
        return " (%s)" % preset["name"]
    return ""


def display_name(preset):
    """The config name as shown to the user.

    The derived lab suffix is appended ONLY to the built-in Lab default;
    researcher-saved configs still store their own lab but show no suffix.
    (Canonical rule shared with the Tk launcher; the web UI mirrors it in JS.)
    """
    name = str(preset.get("name", ""))
    if is_builtin(preset):
        return name + lab_suffix(normalize_config(preset)["lab"])
    return name


def _stamp_of(item):
    """A config's last_run as a plain ISO string ("" when never launched).

    ISO timestamps sort lexicographically in chronological order, so a plain
    string compare (max / _invert) is a correct recency compare.
    """
    stamp = item.get("last_run")
    return stamp if isinstance(stamp, str) and stamp else ""


def order_presets_for_display(presets):
    """The order the config list is shown in, shared by BOTH faces (Job 2).

    The app-owned built-in Lab default is pinned to the very TOP regardless of
    when it last ran; below it the saved configs are ordered MOST-RECENTLY-
    LAUNCHED FIRST (by ``last_run`` descending). Configs that have never been
    launched (``last_run`` None) come after every launched config, ordered by
    name (case-insensitive) for a stable, predictable list.

    This is a DISPLAY/SELECTION concern only: it returns a new sorted list and
    never mutates or persists the stored order (see save_store) -- the ordering
    is re-derived from ``last_run`` every time it is shown.
    """

    def key(item):
        stamp = _stamp_of(item)
        return (0 if is_builtin(item) else 1,
                0 if stamp else 1, _invert(stamp), str(item.get("name", "")).casefold())

    return sorted(presets, key=key)


# Back-compat alias: the two faces (and the existing tests) call sort_presets;
# it is now just the shared display ordering above so both faces agree.
sort_presets = order_presets_for_display


def select_on_open(presets):
    """The config the app should open SELECTED (Job 2).

    ALWAYS the built-in "Lab default" (the first built-in). The launcher opens
    on the pinned default template every time it starts -- a fresh, Browse-first
    state -- rather than restoring whatever config was last launched. This is a
    deliberate change (2026-09-24): the built-in is a launch TEMPLATE, and lab
    staff should always begin from it and Browse to the study of the day.

    DO NOT "fix" this by remembering the last project folder (suggested again in
    the 2026-10-01 UX review as #35 and DECLINED by Julian): the app keeps
    opening with no project and the lab default. Someone who has already run an
    experiment relaunches it from its saved config or its one-click shortcut;
    a remembered folder would only make the default look like yesterday's
    study. (1.5.0 removed ``last_project`` for the same reason.) Falls
    back to the first config when there is no built-in, and to None only for an
    empty list. Shared by both faces so they open identically.
    """
    if not presets:
        return None
    for p in presets:
        if is_builtin(p):
            return p
    return presets[0]


def _invert(stamp):
    """Sort ISO timestamps descending inside an otherwise ascending sort."""
    return tuple(-ord(ch) for ch in stamp)


def unique_name(name, presets):
    """True when `name` is not already taken (comparison ignores case)."""
    taken = {str(p.get("name", "")).strip().casefold() for p in presets}
    return name.strip().casefold() not in taken


# A saved config whose room WAS the lab's default room when it was saved carries
# this flag: it then simply FOLLOWS the lab's current default room (Julian,
# 2026-10-01), so a lab that later renames its room does not leave such configs
# behind and they never warn about the room. A config saved with any other room
# keeps that room, and warns when it differs from the lab default. Configs saved
# before this flag existed have no key and keep their literal room.
ROOM_FOLLOWS_LAB_KEY = "room_follows_lab"


def follow_lab_room(preset, lab_presets=None):
    """``preset`` as it will LAUNCH: a copy whose room is the lab's CURRENT
    default room when the config was saved with the lab default
    (:data:`ROOM_FOLLOWS_LAB_KEY`); otherwise the preset unchanged. Both faces
    use it wherever a saved config is loaded or compared, and the one-click
    shortcut launches it."""
    if not isinstance(preset, dict) or not preset.get(ROOM_FOLLOWS_LAB_KEY):
        return preset
    resolved = _presets_or_default(lab_presets)
    lab = str(preset.get("lab") or "").strip()
    if find_lab_preset(lab, resolved) is None:
        return preset
    room = lab_default_room(resolved, lab)
    if str(preset.get("room_name") or "").strip() == room:
        return preset
    followed = dict(preset)
    followed["room_name"] = room
    return followed


def preset_from_fields(name, fields, created=None, author=None, lab_presets=None):
    preset = normalize_config(fields)
    # Saved with the lab's default room -> it follows that lab's default room.
    _resolved_labs = _presets_or_default(lab_presets)
    if (find_lab_preset(preset["lab"], _resolved_labs) is not None
            and preset["room_name"].strip() == lab_default_room(_resolved_labs,
                                                                preset["lab"])):
        preset[ROOM_FOLLOWS_LAB_KEY] = True
    # A saved config always PINS its database by id (1.5.0): "follow this PC's
    # default" is only for the generated Lab default.
    if not preset["database_id"] and preset["db_mode"] == DB_MODE_LAB:
        preset["database_id"] = (_DEFAULT_DB_ID if _DEFAULT_DB_ID in _DB_REGISTRY
                                 else DB_BUILTIN_SQLITE)
        preset = normalize_config(preset)
    preset["name"] = name.strip()
    preset["created"] = created or now_iso()
    preset["last_run"] = None
    author = (author or "").strip()
    if not author:
        try:
            author = getpass.getuser()
        except Exception:
            author = ""
    preset["author"] = author
    # A user-made config is NEVER built in: only default_preset() sets that, so
    # Save As can never mint an undeletable, top-pinned config.
    preset["builtin"] = False
    return preset


def format_last_run(stamp):
    if not stamp:
        return "Never run"
    try:
        when = _dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return str(stamp)
    today = _dt.date.today()
    if when.date() == today:
        return "Last run today at " + when.strftime("%H:%M")
    if (today - when.date()).days == 1:
        return "Last run yesterday at " + when.strftime("%H:%M")
    return "Last run " + when.strftime("%d %b %Y at %H:%M")


# ---------------------------------------------------------------------------
# Session history log (fable review F). ONE JSON line per launch appended to
# data/sessions.jsonl, so a lab manager can answer "who reset the shared DB at
# 14:03" or "which project ran in the small lab last Tuesday". This is a passive
# LOG, not a data platform: the launcher writes it and offers a read-only viewer;
# it never manages sessions or touches oTree's own data (see mission_and_review).
# Everything here is FAIL-SOFT -- logging must never break or delay a launch.
# ---------------------------------------------------------------------------


def launch_history_path():
    """Where the launch history log lives (data/launch_history.jsonl; it was
    sessions.jsonl before 1.5.0). OTREE_LAB_SESSIONS overrides it (tests)."""
    override = os.environ.get("OTREE_LAB_SESSIONS")
    if override:
        return override
    return os.path.join(data_dir(), LAUNCH_HISTORY_FILENAME)


# Back-compat alias: callers and tests use sessions_path().
sessions_path = launch_history_path


def _session_database_label(cfg):
    """A short, human database label for one history line (no secrets): the
    database's nickname on this PC."""
    c = normalize_config(cfg)
    if c["db_mode"] == DB_MODE_NONE:
        return "SQLite (no lab DB)"
    return database_summary_label(c)


def build_session_entry(cfg, config_name="", author="", outcome="ok",
                        server_ready_seconds=None, resetdb=None,
                        lab_presets=None, when=None):
    """One session-history record (a plain dict) for a single launch.

    Pure and shared by BOTH faces so the JSONL lines are identical in shape. It
    carries no secrets -- only the database LABEL, never the URL or password.
    ``outcome`` is normalised to "ok"/"fail". ``server_ready_seconds`` is how long
    the server took to answer the readiness poll (None when not measured).
    """
    c = normalize_config(cfg)
    author = str(author or "").strip()
    if not author:
        try:
            author = getpass.getuser()
        except Exception:
            author = ""
    seconds = None
    if server_ready_seconds is not None:
        try:
            seconds = round(float(server_ready_seconds), 1)
        except (TypeError, ValueError):
            seconds = None
    return {
        "timestamp": when or now_iso(),
        "config": str(config_name or "").strip(),
        "author": author,
        "project": c["project_path"],
        "lab": c["lab"],
        "room": c["room_name"],
        "database": _session_database_label(c),
        "seats": len(resolve_seats(c, lab_presets)),
        "resetdb": bool(c["resetdb"] if resetdb is None else resetdb),
        "outcome": "ok" if outcome == "ok" else "fail",
        "server_ready_seconds": seconds,
    }


def record_session(cfg, config_name="", author="", outcome="ok",
                   server_ready_seconds=None, resetdb=None, lab_presets=None,
                   path=None):
    """Append ONE JSON line to data/launch_history.jsonl for a launch.

    FAIL-SOFT by contract: every error (a bad path, a full disk, a serialisation
    quirk) is swallowed so logging can NEVER break or delay a launch. Returns the
    entry dict that was written, or None if nothing could be written.

    The append uses O_APPEND ("a" mode), whose single-line writes are atomic on
    the platforms the launcher runs on, so a GUI and the headless one-click
    shortcut can both log without a separate lock file cluttering data/.
    """
    try:
        entry = build_session_entry(
            cfg, config_name=config_name, author=author, outcome=outcome,
            server_ready_seconds=server_ready_seconds, resetdb=resetdb,
            lab_presets=lab_presets)
        # The version of the study that ran (a git repository only): the commit
        # ties a dataset to the code that produced it.
        version = study_version(entry.get("project"))
        if version:
            entry["study_version"] = version
        target = path or sessions_path()
        folder = os.path.dirname(target) or "."
        os.makedirs(folder, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return entry
    except Exception:
        return None


def read_sessions(limit=50, path=None):
    """The most recent launches from data/launch_history.jsonl, NEWEST FIRST.

    Read-only and fail-soft: an absent or unreadable file returns []; malformed
    lines are skipped. ``limit`` None returns every entry. The file is written in
    append (oldest-first) order, so this reverses it for the viewer.
    """
    target = path or sessions_path()
    entries = []
    try:
        with open(target, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        return []
    entries.reverse()
    if limit is not None and limit >= 0:
        return entries[:limit]
    return entries


# ---------------------------------------------------------------------------
# Clone history (2026-10-01): one JSON line per study cloned from GitHub on THIS
# computer (and per "Get a fresh copy"), so Settings > GitHub can list which
# studies are here, who got them, and where. Like the launch history: append
# only, fail-soft both ways (writing can never break a clone; a missing or
# partly corrupt file never breaks the app, bad lines are skipped). Never a
# token: only the LABEL of the token the clone used.
# ---------------------------------------------------------------------------

CLONE_RESEARCHER_MISSING = "Enter or pick a researcher: who is getting this study."
CLONE_HISTORY_LINK_LABEL = "Studies from %s on this computer"
CLONE_HISTORY_EMPTY_TEXT = "No study has been cloned from GitHub on this computer yet."
CLONE_FOLDER_MISSING_TEXT = "folder no longer exists"


def clone_history_path():
    """data/clone_history.jsonl; OTREE_LAB_CLONE_HISTORY overrides it (tests)."""
    override = os.environ.get("OTREE_LAB_CLONE_HISTORY")
    if override:
        return override
    return os.path.join(data_dir(), CLONE_HISTORY_FILENAME)


def record_clone(org, repo, researcher, folder, token_label="", fresh_copy=False,
                 path=None, when=None):
    """Append ONE line for a successful clone / fresh copy: when, organisation,
    repository, researcher, the folder (the one the faces select), the LABEL of
    the token used (never a token) and the fresh-copy flag. Fail-soft: returns the
    entry, or None when nothing could be written."""
    try:
        entry = {"timestamp": when or now_iso(),
                 "org": str(org or "").strip(),
                 "repo": str(repo or "").strip(),
                 "researcher": str(researcher or "").strip(),
                 "folder": str(folder or "").strip(),
                 "token_label": str(token_label or "").strip(),
                 "fresh_copy": bool(fresh_copy)}
        target = path or clone_history_path()
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry
    except Exception:
        return None


def read_clone_history(limit=None, path=None):
    """The clone log, NEWEST FIRST. Read-only and fail-soft: a missing or
    unreadable file gives []; lines that are not a JSON object are skipped."""
    entries = []
    try:
        with open(path or clone_history_path(), "r", encoding="utf-8",
                  errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        return []
    entries.reverse()
    return entries if limit is None else entries[:max(0, int(limit))]


def clone_history_rows(limit=None, path=None):
    """The clone log as the faces show it, newest first::

        {"when", "date", "org", "repo", "name", "researcher", "folder",
         "exists", "fresh_copy", "status"}

    ``name`` is "org/repo"; ``exists`` False marks a folder that is gone
    (``status`` then says so); ``date`` is "1 Oct 2026, 14:05"."""
    rows = []
    for entry in read_clone_history(limit=limit, path=path):
        folder = str(entry.get("folder") or "")
        org = str(entry.get("org") or "")
        repo = str(entry.get("repo") or "")
        when = str(entry.get("timestamp") or "")
        try:
            moment = _dt.datetime.fromisoformat(when)
            date = "%d %s" % (moment.day, moment.strftime("%b %Y, %H:%M"))
        except (TypeError, ValueError):
            date = when
        exists = bool(folder) and os.path.isdir(folder)
        rows.append({"when": when, "date": date, "org": org, "repo": repo,
                     "name": "%s/%s" % (org, repo) if org else repo,
                     "researcher": str(entry.get("researcher") or ""),
                     "folder": folder, "exists": exists,
                     "fresh_copy": bool(entry.get("fresh_copy")),
                     "status": "" if exists else CLONE_FOLDER_MISSING_TEXT})
    return rows


def open_folder(folder, opener=None):
    """Show ``folder`` in the computer's file browser (Explorer / Finder / the
    desktop's). Returns ``{"ok", "message"}``; a folder that is gone is refused
    with a plain message. ``opener(argv_or_path)`` replaces the real call in
    tests."""
    folder = str(folder or "").strip()
    if not folder or not os.path.isdir(folder):
        return {"ok": False, "message": "This folder no longer exists: %s" % folder}
    try:
        if opener is not None:
            opener(folder)
        elif sys.platform.startswith("win"):
            os.startfile(folder)                       # noqa: an Explorer window
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception as error:
        return {"ok": False, "message": "Could not open the folder: %s" % error}
    return {"ok": True, "message": "Opened %s." % folder}


def clone_researcher_for(folder, path=None):
    """Who got the study in ``folder`` (or the repository it was cloned into),
    from the clone log: "" when it is not in the log."""
    def norm(value):
        return os.path.normcase(os.path.realpath(str(value or ""))) if value else ""
    wanted = norm(folder)
    top = _git_text(folder, "rev-parse", "--show-toplevel", timeout=10) if folder else None
    tops = {wanted, norm(top) if top else ""} - {""}
    for entry in read_clone_history(path=path):
        if norm(entry.get("folder")) in tops:
            return str(entry.get("researcher") or "")
    return ""


# ---------------------------------------------------------------------------
# Version + once-a-day update check (fable review I). A quiet, FAIL-SOFT nudge:
# ask GitHub for the LATEST RELEASE at most once a day (cached in data/), read its
# tag_name (e.g. "v1.2.0") and compare it to APP_VERSION with a small semver
# compare. Only when the release tag is STRICTLY GREATER does the app footer show
# "new version available" (a clickable link to the repo). No network, no
# releases, or ANY error means silence: the check never disrupts the launcher.
# ---------------------------------------------------------------------------

GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/juliantait/otree-lab-launcher/releases/latest")
REPO_URL = "https://github.com/juliantait/otree-lab-launcher"
UPDATE_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
# The short footer label shown when a newer release exists, and the explanatory
# text shown on hover (both faces) next to the clickable repo link.
UPDATE_LABEL = "new version available"
# The link that runs the check by hand (Settings footer).
UPDATE_CHECK_LABEL = "Check for updates"
UPDATE_BUTTON_LABEL = "Update"
UPDATE_DISCARD_LABEL = "Discard these changes and update"
UPDATE_DONE_TITLE = "Update downloaded"
UPDATE_DONE_ACTION = "Restart to activate the update"
UPDATE_DONE_NOTE = ("Quit the launcher and open it again from the usual shortcut. It "
                    "starts on the new version.")
UPDATE_TOOLTIP = ("Download the app folder from GitHub and replace the app "
                  "folder — keep your data")


def update_cache_path():
    """Where the last update-check result is cached. OTREE_LAB_UPDATE_CACHE
    overrides it (used by tests). Lives in data/, so it survives an update."""
    override = os.environ.get("OTREE_LAB_UPDATE_CACHE")
    if override:
        return override
    return os.path.join(data_dir(), UPDATE_CHECK_FILENAME)


def parse_github_release_tag(raw):
    """The latest release's ``tag_name`` (e.g. "v1.2.0") from a GitHub releases
    API response, or None if it cannot be read.

    Accepts bytes, a JSON string, or an already-parsed dict/list. ``releases/latest``
    returns a single release object; a ``releases`` listing returns a list, so
    both shapes are handled. Never raises.
    """
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        data = json.loads(raw) if isinstance(raw, str) else raw
        release = data[0] if isinstance(data, list) else data
        tag = str(release.get("tag_name", "") or "").strip()
        return tag or None
    except Exception:
        return None


def parse_semver(text):
    """A (major, minor, patch) int tuple from a version string, or None.

    Strips a leading "v"/"V" and reads up to three dot-separated numeric parts,
    padding missing parts with 0 ("v1.2" -> (1, 2, 0)). Any non-numeric leading
    part makes it None, so a garbage tag can never read as a version.
    """
    if text is None:
        return None
    s = str(text).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    if not s:
        return None
    nums = []
    for part in s.split(".")[:3]:
        match = re.match(r"\d+", part.strip())
        if not match:
            return None
        nums.append(int(match.group()))
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def version_is_newer(remote, current):
    """True only when ``remote`` is a STRICTLY GREATER semver than ``current``.

    Both are parsed with :func:`parse_semver`; if either cannot be parsed the
    answer is False (never a phantom "newer"), so a missing or garbage tag is
    silent.
    """
    remote_v = parse_semver(remote)
    current_v = parse_semver(current)
    if remote_v is None or current_v is None:
        return False
    return remote_v > current_v


def _https_ssl_context():
    """An SSL context whose trust store is certifi's bundled CA set.

    This exists because the python.org macOS FRAMEWORK build ships with NO
    system CA bundle (``ssl.get_default_verify_paths().cafile`` is None), so
    Python's default context cannot verify GitHub's certificate and every
    HTTPS request raises CERTIFICATE_VERIFY_FAILED -- which the update check
    then swallows and reports as "Could not reach GitHub". Pointing the context
    at ``certifi.where()`` gives Python a real CA bundle on every platform.

    Used on ALL platforms (not gated to macOS): certifi ships the standard CA
    roots that GitHub chains to, so it is a no-op improvement on Windows/Linux
    where the system store already worked. If certifi cannot be imported for any
    reason we return None so the caller falls back to Python's default context
    (which keeps working where the system trust store is present). Never raises.
    """
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _fetch_release_payload(timeout=4.0):
    """GET the latest-release JSON from GitHub (raw bytes). Short timeout so a
    stalled network never hangs the check; the caller swallows any error
    (including the 404 GitHub returns when there are no releases yet).

    The HTTPS request verifies GitHub's certificate against certifi's bundled
    CA set (see :func:`_https_ssl_context`); passing ``context=None`` when
    certifi is unavailable is equivalent to using Python's default context."""
    import urllib.request
    request = urllib.request.Request(
        GITHUB_RELEASES_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "otree-lab-launcher"})
    context = _https_ssl_context()
    with urllib.request.urlopen(
            request, timeout=timeout, context=context) as response:
        return response.read()


def _write_update_cache(path, data):
    try:
        folder = os.path.dirname(path) or "."
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError:
        pass


def check_for_update(force=False, fetcher=None, path=None, now=None):
    """Once-a-day, FAIL-SOFT release update check. Returns a dict:

        {"update_available": bool,      # latest release tag > APP_VERSION (semver)
         "current": APP_VERSION,        # what this build is
         "remote_version": "v1.2.0",    # latest known release tag ("" if unknown)
         "checked": bool,               # True only if a release tag was fetched OK
         "check_failed": bool,          # True if a fetch was TRIED this call but failed
         "repo_url": REPO_URL,          # where the footer link points
         "label": str,                  # UPDATE_LABEL when an update exists, else ""
         "tooltip": UPDATE_TOOLTIP}     # the hover text next to the link

    When the cached check is fresh (< a day old) and not ``force``, it returns the
    cached verdict with NO network. Any network error, a 404 (no releases), or a
    parse error is swallowed and the last known cache (or a silent "no update") is
    returned -- the check never raises and never blocks longer than the fetch
    timeout. ``fetcher`` (returns a payload for :func:`parse_github_release_tag`)
    is injectable for tests.

    ``check_failed`` is the honesty flag that fixes the "silently says up to date"
    bug: it is True only when this call actually ATTEMPTED a network fetch (forced,
    or the daily cache was stale) and that fetch failed -- offline, rate-limited
    (GitHub allows 60 unauthenticated req/hour per IP), an SSL/proxy error, or a
    response with no parseable release tag. A forced manual check can then report
    "could not check" instead of a false "you are up to date", which is exactly the
    verdict a failed fetch used to masquerade as. It stays False when a fresh cache
    is served without any fetch, so the passive daily path never cries wolf.
    """
    path = path or update_cache_path()
    now = now or _dt.datetime.now()

    cache = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            cache = loaded
    except (OSError, ValueError):
        cache = {}

    remote_version = str(cache.get("remote_version", "") or "")
    last_checked = cache.get("last_checked", "")
    # Freshness is computed INDEPENDENTLY of ``force`` so the decision below is
    # unambiguous: the automatic (non-force) check reuses a still-fresh daily cache,
    # but a FORCED check always fetches regardless of freshness.
    fresh = False
    if last_checked:
        try:
            when = _dt.datetime.fromisoformat(str(last_checked))
            fresh = (now - when).total_seconds() < UPDATE_CHECK_INTERVAL_SECONDS
        except (TypeError, ValueError):
            fresh = False

    # A FORCED check (the on-demand Update button) genuinely BYPASSES the cache: it
    # hits the network NOW and OVERWRITES the cache, even if the cached result is
    # still fresh -- so a release that landed since the last daily check (e.g. 1.3.1
    # > 1.3.0) is reported immediately instead of the stale cached latest (item 10).
    # It is never short-circuited by a fresh-enough cache.
    checked = False
    check_failed = False
    if force or not fresh:
        fetch = fetcher or _fetch_release_payload
        try:
            tag = parse_github_release_tag(fetch())
            if tag:
                remote_version = tag
                checked = True
                _write_update_cache(path, {
                    "remote_version": remote_version,
                    "last_checked": now.isoformat(timespec="seconds")})
            else:
                # We reached something but it had no parseable release tag (a 404
                # body, a rate-limit message, garbage): the fetch did not succeed.
                check_failed = True
        except Exception:
            # No network / no releases (404) / rate limit / SSL / parse error. Keep
            # whatever cache we had, but REMEMBER the fetch failed so a forced check
            # can say "could not check" instead of a false "up to date".
            check_failed = True

    update_available = version_is_newer(remote_version, APP_VERSION)
    return {"update_available": update_available,
            "current": APP_VERSION,
            "remote_version": remote_version,
            "checked": checked,
            "check_failed": check_failed,
            "repo_url": REPO_URL,
            "label": UPDATE_LABEL if update_available else "",
            "tooltip": UPDATE_TOOLTIP}


# --- git-aware in-place update ---------------------------------------------
#
# When an update is available the launcher offers ONE of two update paths,
# depending on how this copy was installed:
#   * a GIT working tree  -> a one-click "git pull" on the install directory,
#   * a plain download    -> a link to the GitHub page to re-download.
# Both act ONLY on the launcher's own install directory (repo_root(), the parent
# of app/). Neither ever touches an oTree/experiment process. Both are fail-soft.
GIT_PULL_TOOLTIP = "This will run git pull to update the project."


def is_git_install(repo=None):
    """True when the launcher's install directory is a git working tree.

    Runs ``git -C <repo> status``; a nonzero exit, git not being on PATH, or a
    timeout all read as NOT a git install (so the caller falls back to the
    GitHub-download path). ``repo`` defaults to :func:`repo_root` (the parent of
    app/, which the launcher already anchors DATA_DIR to). Never raises.
    """
    repo = repo if repo is not None else repo_root()
    try:
        result = _timed_run(
            subprocess.run, ["git", "-C", str(repo), "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=10, creationflags=_no_window_flags())
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# App-owned template files (repo-relative, forward slashes). The in-app Update
# may put these back to the shipped version when local edits to them are the ONLY
# thing blocking the pull. Up to 1.4.x they shipped in data/ (a lab data folder
# copied over data/ marked them changed; the 1.5.0 pull deletes them, which a
# local edit also blocks); since 1.5.0 they live in app/assets/. Deliberately
# explicit: never lab_info.json, machine.json or anything else the lab writes
# (data/ is gitignored as a whole).
SHIPPED_TEMPLATE_FILES = (
    "data/README.md",
    "data/lab_info.example.json",
    "data/maps/README.md",
    "data/maps/example_large.json",
    "data/maps/example_small.json",
    "app/assets/data_folder_README.md",
    "app/assets/lab_info.example.json",
    "app/assets/maps/README.md",
    "app/assets/maps/example_large.json",
    "app/assets/maps/example_small.json",
)


def _pull_blockers(repo, output):
    """The local files a failed ``git pull`` says it would overwrite, or None
    when the failure is not about local changes. Falls back to the tracked
    files with local edits when git gives no list (e.g. a rebase pull's
    "You have unstaged changes")."""
    low = str(output or "").lower()
    if "untracked working tree files" in low:
        return None
    classified = classify_git_error(output)
    if classified["code"] == "local_changes" and classified["files"]:
        return [f.replace("\\", "/") for f in classified["files"]]
    if classified["code"] == "local_changes" or "unstaged changes" in low \
            or "uncommitted changes" in low:
        porcelain = _git_text(str(repo), "status", "--porcelain",
                              "--untracked-files=no") or ""
        files = [line[3:].strip().strip('"') for line in porcelain.splitlines()
                 if len(line) > 3]
        return files or None
    return None


def _run_pull(repo):
    code, output = _run_git(["-C", str(repo), "pull"], timeout=120)
    return code == 0, output.strip()


def update_discard_confirm(files):
    """The confirm text for "Discard these changes and update": it names the
    files. Everything a lab owns is in data/ and local/, which git ignores, so a
    changed tracked file in the launcher folder is never lab data."""
    return ("Discard the local changes to %s and update? The launcher's own files "
            "are put back as shipped; a copy of the changed files is kept in "
            "data/retired/. Your labs, configs and databases are not touched."
            % _short_file_list(files, limit=8))


def _backup_discarded(repo, files):
    """Copy the files about to be put back into data/retired/<stamp>-update-
    discarded/ (same relative paths). Returns the folder ("" when nothing could
    be copied). Fail-soft."""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = os.path.join(data_dir(), RETIRED_DIRNAME, "%s-update-discarded" % stamp)
    copied = 0
    for name in files:
        source = os.path.join(str(repo), name)
        if not os.path.isfile(source):
            continue
        target = os.path.join(folder, name)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        except OSError:
            pass
    return folder if copied else ""


def git_pull(repo=None, discard=None):
    """Run ``git -C <repo> pull`` on the install directory and capture its output.

    ``discard`` (the "Discard these changes and update" button) is the list of
    changed launcher files the user confirmed may be put back: each must really
    be a tracked file with local edits; they are copied to data/retired/ first,
    restored with ``git checkout HEAD --`` and the pull runs. A failed pull that
    is blocked only by tracked local edits carries ``can_discard`` True.

    Returns ``{"ok", "output", "message", "restored", "restored_note"}``: ``ok``
    is True only when the pull succeeded, ``output`` is git's combined text and
    ``message`` the confirmation ("Update installed. Restart the launcher to
    apply.") or a plain failure reason.

    Safe update: when the pull is blocked ONLY by local edits to
    :data:`SHIPPED_TEMPLATE_FILES`, those files are restored to the committed
    version (``git checkout HEAD -- <files>``) and the pull runs once more;
    ``restored`` lists them and ``restored_note`` says so. When any other file
    blocks it, nothing is restored and the result carries ``reason_code``
    "local_changes" plus ``files``. Acts ONLY on the launcher's own install
    directory; never touches any oTree/experiment process. Fail-soft: git
    missing / not a repo / a timeout returns ``ok`` False rather than raising.
    """
    repo = repo if repo is not None else repo_root()
    result = {"ok": False, "output": "", "message": "", "restored": [],
              "restored_note": "", "can_discard": False, "discarded": [],
              "discard_backup": ""}
    if discard:
        wanted = [str(f).replace("\\", "/") for f in discard if str(f).strip()]
        changed = set(_tracked_changes(repo, timeout=30) or [])
        if not wanted or any(f not in changed for f in wanted):
            result["message"] = ("Nothing was discarded: the list of changed files is "
                                 "no longer the same. Try Update again.")
            result["reason_code"] = "discard_mismatch"
            return result
        result["discard_backup"] = _backup_discarded(repo, wanted)
        try:
            code, restore_out = _run_git(["-C", str(repo), "checkout", "HEAD", "--"]
                                         + wanted, timeout=30)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            result["message"] = "Could not put the files back: %s" % error
            return result
        if code != 0:
            result["output"] = restore_out.strip()
            result["message"] = "Could not put the files back. Nothing was updated."
            return result
        result["discarded"] = wanted
    try:
        ok, output = _run_pull(repo)
        last = output
        if not ok:
            blockers = _pull_blockers(repo, output)
            if blockers and all(f in SHIPPED_TEMPLATE_FILES for f in blockers):
                code, restore_out = _run_git(
                    ["-C", str(repo), "checkout", "HEAD", "--"] + blockers,
                    timeout=30)
                if code == 0:
                    result["restored"] = list(blockers)
                    ok, last = _run_pull(repo)
                    output = (output + "\n\n" + last).strip()
                else:
                    output = (output + "\n\n" + restore_out).strip()
    except FileNotFoundError:
        result["message"] = "git is not installed on this machine."
        return result
    except subprocess.TimeoutExpired:
        result["message"] = "git pull timed out."
        return result
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["message"] = "Could not run git pull: %s" % error
        return result
    result["ok"] = ok
    result["output"] = output
    if result["restored"]:
        count = len(result["restored"])
        result["restored_note"] = (
            "Restored %s that had been changed locally: %s." % (
                _plural(count, "shipped template file"),
                _short_file_list(result["restored"], limit=len(result["restored"]))))
    if ok:
        result["message"] = "Update installed. Restart the launcher to apply."
        if result["restored_note"]:
            result["message"] += " " + result["restored_note"]
        return result
    classified = classify_git_error(last)
    result["reason_code"] = classified["code"]
    files = _pull_blockers(repo, last) or []
    if files:
        result["reason_code"] = "local_changes"
        result["files"] = files
        result["can_discard"] = True
        result["message"] = (
            "Update blocked: this computer has local changes to %s that the "
            "update would overwrite. Nothing was changed. Ask the lab manager to "
            "undo those edits, then update again." % _short_file_list(files, limit=8))
    else:
        result["files"] = classified["files"]
        result["message"] = "git pull failed: %s" % classified["reason"]
    return result


# --- Per-study Git update + GitHub Organisation clone -----------------------
#
# These extend the v1.2.0 launcher-self-update git plumbing (is_git_install /
# git_pull, above) to the EXPERIMENT study folder, for the opt-in GitHub
# Organisation Sync feature. They ALWAYS act on the selected STUDY folder that is
# passed in, NEVER on the launcher's own app/ folder, and they NEVER touch any
# oTree/experiment process -- they only run git on the folder's files. All are
# fail-soft: git missing / a timeout / a bad path returns a well-formed result
# rather than raising.
GITHUB_URL_TEMPLATE = "https://github.com/%s/%s"


def is_git_repo(folder):
    """True when ``folder`` is inside a git working tree.

    Detected with ``git -C <folder> rev-parse --is-inside-work-tree`` per spec: a
    nonzero / fatal exit (not a repo, no such folder), git not being on PATH, or a
    timeout all read as NOT a git repo. Never raises. Distinct from
    :func:`is_git_install`, which checks the launcher's OWN install directory; this
    one is pointed at an arbitrary study folder.
    """
    folder = str(folder or "").strip()
    if not folder:
        return False
    try:
        result = _timed_run(
            subprocess.run, ["git", "-C", folder, "rev-parse", "--is-inside-work-tree"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=10, creationflags=_no_window_flags())
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    output = result.stdout or b""
    if isinstance(output, (bytes, bytearray)):
        output = output.decode("utf-8", "replace")
    return output.strip().lower() == "true"


# ---------------------------------------------------------------------------
# Every git command the launcher runs goes to the activity log (2026-10-01):
# what ran, how long it took, how it ended. So a slow case (Julian's first clone
# on a Windows lab PC: a minute on "Checking ...") shows where the time went.
# Never a secret: URL credentials (user:password@ / the token label) are taken
# out of the URL (a label is named separately: it is not a secret), anything
# shaped like a GitHub token is masked, and git credential's stdin is never
# shown. The faces subscribe with add_git_command_listener(fn(line, level)).
# ---------------------------------------------------------------------------

_GIT_LISTENERS = []
_GIT_LISTENERS_LOCK = threading.Lock()
_URL_USERINFO_RE = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@")
_TOKEN_SHAPED_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})")


def add_git_command_listener(listener):
    """Call ``listener(line, level)`` after every git command (``level`` is
    "info" when it worked, "warn" when it failed or was stopped). A bound method
    is held WEAKLY, so a face that goes away stops listening by itself."""
    import weakref
    ref = weakref.WeakMethod(listener) if hasattr(listener, "__self__") else \
        (lambda fn=listener: fn)
    with _GIT_LISTENERS_LOCK:
        _GIT_LISTENERS.append(ref)
    return ref


def remove_git_command_listener(ref):
    with _GIT_LISTENERS_LOCK:
        if ref in _GIT_LISTENERS:
            _GIT_LISTENERS.remove(ref)


def git_command_text(args):
    """``git <args>`` as safe text: credentials out of every URL (a token label
    is named once at the end), token-shaped strings masked."""
    labels = []

    def strip(match):
        label = match.group(2).split(":", 1)[0]
        if label and label not in labels:
            labels.append(label)
        return match.group(1)
    parts = []
    for arg in args:
        text = _TOKEN_SHAPED_RE.sub("********", _URL_USERINFO_RE.sub(strip, str(arg)))
        parts.append(text if text and " " not in text else '"%s"' % text)
    line = "git " + " ".join(parts)
    if labels:
        line += " (token %s)" % ", ".join(labels)
    return line


def git_command_line(args, seconds, code=None, timed_out=False, error=None):
    """One activity-log line: "git ls-remote https://github.com/o/r HEAD: ok,
    1.4 s" / "failed (exit 128), 3.0 s" / "stopped after 20.0 s (too slow)"."""
    if timed_out:
        end = "stopped after %.1f s (too slow)" % seconds
    elif error is not None:
        end = "could not run (%s), %.1f s" % (type(error).__name__, seconds)
    elif code == 0:
        end = "ok, %.1f s" % seconds
    else:
        end = "failed (exit %s), %.1f s" % (code, seconds)
    return "%s: %s" % (git_command_text(args), end)


def _note_git(args, started, code=None, timed_out=False, error=None):
    """Tell the listeners how one git command went. Never raises."""
    with _GIT_LISTENERS_LOCK:
        refs = list(_GIT_LISTENERS)
    if not refs:
        return
    try:
        line = git_command_line(args, time.monotonic() - started, code=code,
                                timed_out=timed_out, error=error)
    except Exception:
        return
    level = "info" if (code == 0 and not timed_out and error is None) else "warn"
    for ref in refs:
        listener = ref()
        if listener is None:
            remove_git_command_listener(ref)
            continue
        try:
            listener(line, level)
        except Exception:
            pass


def _timed_run(run, argv, **kwargs):
    """``run(argv, **kwargs)`` (subprocess.run or a test runner), reported to the
    git listeners with its duration. Re-raises whatever ``run`` raises."""
    started = time.monotonic()
    try:
        result = run(argv, **kwargs)
    except subprocess.TimeoutExpired:
        _note_git(argv[1:], started, timed_out=True)
        raise
    except Exception as error:
        _note_git(argv[1:], started, error=error)
        raise
    _note_git(argv[1:], started, code=getattr(result, "returncode", None))
    return result


def _no_prompt_env():
    """The environment for a git command that must NEVER ask for a login: terminal
    prompts off, Git Credential Manager's own window off, no askpass helper, ssh
    in batch mode. Every login then goes through the launcher's ONE GitHub login
    dialog, the same way on Windows and macOS."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    env["GIT_ASKPASS"] = ""
    env.pop("SSH_ASKPASS", None)
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _run_git(args, runner=None, timeout=60, no_prompt=False):
    """Run ``git <args>`` and return ``(returncode, output)`` with stdout+stderr
    merged and decoded. Raises what subprocess raises (FileNotFoundError when git
    is missing, TimeoutExpired, OSError); callers turn those into results.
    Runs with CREATE_NO_WINDOW on Windows so Git pull / Clone never flash a
    console window from the windowless launcher. ``no_prompt`` runs it with
    :func:`_no_prompt_env` (clone, pull, fetch: git can never ask for a login)."""
    run = runner if runner is not None else subprocess.run
    extra = {"env": _no_prompt_env()} if no_prompt else {}
    result = _timed_run(run, ["git"] + list(args),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=timeout, creationflags=_no_window_flags(), **extra)
    output = getattr(result, "stdout", b"") or b""
    if isinstance(output, (bytes, bytearray)):
        output = output.decode("utf-8", "replace")
    return getattr(result, "returncode", 1), output


def _git_text(folder, *args, timeout=30):
    """Stripped output of a read-only ``git -C <folder> <args>``, or None when it
    fails for any reason (never raises)."""
    try:
        code, output = _run_git(["-C", folder] + list(args), timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return output.strip() if code == 0 else None


def _is_launcher_folder(folder):
    """True when running git in ``folder`` would act on the LAUNCHER's own
    install (repo_root()): the folder is the install directory itself, or sits
    inside it without being a separate git repo of its own. Git pull must never
    touch the launcher (that is the in-app updater's job)."""
    def norm(path):
        return os.path.normcase(os.path.realpath(path))
    root = norm(repo_root())
    real = norm(folder)
    if real == root:
        return True
    if not real.startswith(root.rstrip(os.sep) + os.sep):
        return False
    top = _git_text(folder, "rev-parse", "--show-toplevel")
    return not top or norm(top) == root


def _plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _short_file_list(files, limit=5):
    """'a.py', 'a.py and b.py' or 'a.py, b.py, c.py and 2 more' for messages."""
    files = list(files)
    if len(files) > limit:
        shown = files[:limit - 1]
        return "%s and %d more" % (", ".join(shown), len(files) - len(shown))
    if len(files) <= 1:
        return "".join(files)
    return "%s and %s" % (", ".join(files[:-1]), files[-1])


def _indented_files_after(lines, marker):
    """The tab-indented file names git prints under a line containing
    ``marker`` (e.g. "would be overwritten by merge:")."""
    files = []
    for index, line in enumerate(lines):
        if marker in line:
            for follow in lines[index + 1:]:
                if not follow.startswith(("\t", "    ")) or not follow.strip():
                    break
                files.append(follow.strip())
    return files


def classify_git_error(output):
    """Turn raw git error text into a plain-language reason for the UI.

    Returns ``{"code": str, "reason": str, "files": [str]}`` where ``code`` is
    one of ``local_changes``, ``untracked``, ``conflict``, ``diverged``,
    ``no_upstream``, ``not_found``, ``auth``, ``network``, ``ownership``,
    ``not_repo`` or ``unknown``, and ``files`` names the files involved when git
    lists them (local changes / conflicts). Pure text matching; shared by the
    Git pull result and the clone dialog so both faces word failures alike.
    """
    text = str(output or "")
    low = text.lower()
    lines = text.splitlines()
    if "would be overwritten by" in low and "untracked working tree files" in low:
        files = _indented_files_after(lines, "untracked working tree files")
        return {"code": "untracked", "files": files,
                "reason": ("New files on this computer would be overwritten by the "
                           "pull: %s. Nothing was changed. Move or delete them, "
                           "then pull again." % (_short_file_list(files) or
                                                 "see the git output"))}
    if "would be overwritten by" in low:
        files = _indented_files_after(lines, "would be overwritten by")
        return {"code": "local_changes", "files": files,
                "reason": ("This computer has local changes to %s that the pull "
                           "would overwrite. Nothing was changed. Get a fresh copy, "
                           "or ask the study owner to put those edits into the "
                           "study." % (_short_file_list(files) or
                                       "some files"))}
    if "conflict" in low and ("merge conflict" in low
                              or "automatic merge failed" in low):
        files = re.findall(r"Merge conflict in (.+)", text)
        return {"code": "conflict", "files": [f.strip() for f in files],
                "reason": ("The pulled changes clash with changes made on this "
                           "computer in %s (a merge conflict). The folder now "
                           "needs a manual fix in git." %
                           (_short_file_list(f.strip() for f in files) or
                            "some files"))}
    if ("divergent branches" in low or "not possible to fast-forward" in low
            or "need to specify how to reconcile" in low):
        return {"code": "diverged", "files": [],
                "reason": ("This folder has changes of its own that are not in the "
                           "online copy, so it cannot simply be updated. Nothing "
                           "was changed.")}
    if ("no tracking information" in low or "no such ref was fetched" in low
            or "couldn't find remote ref" in low):
        return {"code": "no_upstream", "files": [],
                "reason": ("This folder is not linked to a branch on GitHub, so "
                           "git does not know what to pull.")}
    if ("repository not found" in low
            or "does not appear to be a git repository" in low
            or re.search(r"repository '[^']*' not found", low)):
        return {"code": "not_found", "files": [],
                "reason": ("GitHub has no such repository, or this computer's "
                           "GitHub login cannot see it.")}
    if any(key in low for key in (
            "authentication failed", "could not read username",
            "could not read password", "terminal prompts disabled",
            "invalid username or password", "returned error: 403",
            "returned error: 401", "permission denied (publickey)")):
        return {"code": "auth", "files": [],
                "reason": ("GitHub did not accept this computer's login, or no "
                           "GitHub login is set up on it. Ask the lab manager to "
                           "check it.")}
    if any(key in low for key in (
            "could not resolve host", "failed to connect", "connection timed out",
            "connection refused", "network is unreachable", "operation timed out",
            "unable to access", "could not resolve proxy", "ssl")):
        return {"code": "network", "files": [],
                "reason": ("Could not reach GitHub. Check that this computer is "
                           "online, then try again.")}
    if "dubious ownership" in low:
        return {"code": "ownership", "files": [],
                "reason": ("Git refuses to work in this folder because it belongs "
                           "to a different user account on this computer.")}
    if "not a git repository" in low:
        return {"code": "not_repo", "files": [],
                "reason": "This folder is not a git repo."}
    return {"code": "unknown", "files": [],
            "reason": "Git reported an error (see the git output below)."}


def _format_commit_date(iso):
    """'28 Sep 2026, 16:05' in this computer's local time, from git's strict ISO
    date; falls back to the raw text when it does not parse."""
    try:
        when = _dt.datetime.fromisoformat(iso.strip())
        return when.astimezone().strftime("%d %b %Y, %H:%M").lstrip("0")
    except (ValueError, TypeError, OSError):
        return str(iso or "").strip()


_CHANGE_NAMES = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed",
                 "C": "copied", "T": "modified"}


def git_changed_files(folder, old, new):
    """The files that differ between commits ``old`` and ``new`` in ``folder``.

    Returns ``[{"path", "change", "added", "removed", "old_path"}]`` where
    ``change`` is added / modified / deleted / renamed / copied and ``added`` /
    ``removed`` are line counts (None for binary files). Uses ``git diff -z
    --name-status`` + ``--numstat`` (cheap, read-only). ``old`` may be "" (an
    unborn branch), meaning "everything in ``new``". Never raises; [] on error.
    """
    base = old or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"   # git's empty tree
    status_raw = _git_text(folder, "diff", "-z", "-M", "--name-status", base, new)
    numstat_raw = _git_text(folder, "diff", "-z", "-M", "--numstat", base, new)
    if status_raw is None:
        return []
    files, order = {}, []
    parts = status_raw.split("\0")
    i = 0
    while i < len(parts):
        code = parts[i].strip()
        if not code:
            i += 1
            continue
        kind = code[0]
        if kind in "RC" and i + 2 < len(parts):
            old_path, path = parts[i + 1], parts[i + 2]
            i += 3
        else:
            old_path, path = "", parts[i + 1] if i + 1 < len(parts) else ""
            i += 2
        if not path:
            continue
        files[path] = {"path": path, "change": _CHANGE_NAMES.get(kind, "modified"),
                       "added": None, "removed": None, "old_path": old_path}
        order.append(path)
    parts = (numstat_raw or "").split("\0")
    i = 0
    while i < len(parts):
        head = parts[i]
        if not head.strip():
            i += 1
            continue
        fields = head.split("\t")
        if len(fields) >= 3 and fields[2] == "":
            # A rename: "added\tremoved\t" then old\0new\0.
            path = parts[i + 2] if i + 2 < len(parts) else ""
            i += 3
        else:
            path = fields[2] if len(fields) >= 3 else ""
            i += 1
        entry = files.get(path)
        if entry is not None and len(fields) >= 2:
            entry["added"] = int(fields[0]) if fields[0].isdigit() else None
            entry["removed"] = int(fields[1]) if fields[1].isdigit() else None
    return [files[p] for p in order]


_STATUS_WORDS = {"D": "deleted", "A": "added", "R": "renamed", "C": "copied",
                 "U": "in conflict"}


def local_changes_note(porcelain, limit=3):
    """The grey hint shown under "Nothing new" when the study folder has local
    edits, built from ``git status --porcelain`` text; "" when there are none.

    Only tracked changes count (modified / deleted / added / renamed); untracked
    ("??") and ignored ("!!") files are skipped, so a stray db.sqlite3 or
    __pycache__ never triggers it. Names up to ``limit`` files, then "and N more":
    'This folder has local changes (README.md deleted). Git pull does not undo
    local edits.'"""
    items = []
    for line in (porcelain or "").splitlines():
        if len(line) < 4 or line[:2] in ("??", "!!"):
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        word = next((_STATUS_WORDS[c] for c in code if c in _STATUS_WORDS),
                    "modified")
        items.append("%s %s" % (path, word))
    if not items:
        return ""
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown += " and %d more" % (len(items) - limit)
    return ("This folder has local changes (%s). Git pull does not undo local "
            "edits." % shown)


def _local_changes_hint(folder):
    """local_changes_note for ``folder``, or "" when git status fails (never
    raises). Not via _git_text: its strip() would eat the first status column."""
    try:
        code, output = _run_git(["-C", folder, "status", "--porcelain",
                                 "--untracked-files=no"], timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return local_changes_note(output) if code == 0 else ""


def git_update_study(folder):
    """Run ``git pull`` in a selected STUDY folder and report what happened.

    Returns a dict both faces render the same way::

        {"ok", "status", "message", "detail", "reason", "reason_code", "output",
         "files", "file_count", "commit_count", "commit", "conflict_files",
         "old_head", "new_head", "note"}

    ``status`` is one of:
      * ``"current"``  -> nothing new (HEAD did not move). ``message`` is
                          "Nothing new: already up to date."
                          ``note`` is a grey hint naming local edits
                          (local_changes_note) when the folder has any, else "".
      * ``"updated"``  -> HEAD moved. ``message`` is "Pulled N changed files.",
                          ``files`` lists each changed file (added / modified /
                          deleted, +/- lines), ``commit`` holds the date and
                          subject of the latest pulled commit and ``detail`` says
                          it in one line.
      * ``"not_repo"`` -> the folder is not a git repo (nothing run).
      * ``"error"``    -> the pull failed; ``reason`` is the plain-language cause
                          (classify_git_error), ``conflict_files`` names the files
                          when git lists them, and ``output`` keeps git's raw text
                          for a collapsed detail block.

    The outcome is decided by comparing HEAD before and after the pull, not by
    parsing git's wording. NEVER runs in the launcher's own folder
    (``reason_code`` "launcher_folder") and never touches any oTree process.
    Bounded by timeouts; never raises.
    """
    folder = str(folder or "").strip()
    result = {"ok": False, "status": "error", "message": "", "detail": "",
              "reason": "", "reason_code": "", "output": "", "files": [],
              "file_count": 0, "commit_count": 0, "commit": {},
              "conflict_files": [], "old_head": "", "new_head": "", "note": "",
              "login_action": False, "fresh_copy": False}

    def failed(code, reason, output=""):
        result.update(reason_code=code, reason=reason, output=output.strip(),
                      message="Git pull failed: " + reason)
        return result

    if folder and _is_launcher_folder(folder):
        return failed("launcher_folder",
                      "This is the launcher's own folder, not a study folder. "
                      "Choose your study folder first.")
    if not is_git_repo(folder):
        result.update(status="not_repo", reason_code="not_repo",
                      reason="This folder is not a git repo.",
                      message="Git pull failed: this folder is not a git repo.")
        return result
    old = _git_text(folder, "rev-parse", "HEAD") or ""
    try:
        # FAST-FORWARD ONLY: the launcher only ever moves a folder forward to
        # what is online. It never merges (a plain pull could leave conflict
        # markers in the study code, or make a merge commit) and never asks for
        # a login (the GitHub login dialog is the one place for that).
        code, output = _run_git(["-C", folder, "pull", "--ff-only"], timeout=120,
                                no_prompt=True)
        if code != 0:
            # GitHub refused the token the folder's URL names (deleted, or an
            # older clone with none): try this computer's other tokens for this
            # one pull. Nothing in the folder's settings changes.
            retry, again = _retry_with_other_tokens(
                _upstream_remote(folder)[2], lambda extra: _run_git(
                    extra + ["-C", folder, "pull", "--ff-only"], timeout=120,
                    no_prompt=True), output)
            if retry is not None:
                code, output = retry, again
    except FileNotFoundError:
        return failed("no_git", "git is not installed on this computer.")
    except subprocess.TimeoutExpired:
        return failed("timeout", "git pull took too long and was stopped. Check "
                                 "the internet connection, then try again.")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return failed("error", "Could not run git pull (%s)." % error)
    output = output.strip()
    result["output"] = output
    if code != 0:
        why = classify_git_error(output)
        result["conflict_files"] = why["files"]
        failed(why["code"], why["reason"], output)
        # What the face offers under the reason: the GitHub login dialog for a
        # login problem; a fresh copy when this folder cannot be moved forward.
        result["login_action"] = (why["code"] in ("auth", "not_found")
                                  and _remote_is_github(folder))
        result["fresh_copy"] = why["code"] in ("local_changes", "untracked",
                                               "diverged", "conflict")
        return result
    new = _git_text(folder, "rev-parse", "HEAD") or ""
    result.update(ok=True, old_head=old, new_head=new)
    if new == old:
        result.update(status="current", message="Nothing new: already up to date.",
                      note=_local_changes_hint(folder))
        return result
    files = git_changed_files(folder, old, new)
    count_raw = _git_text(folder, "rev-list", "--count",
                          ("%s..%s" % (old, new)) if old else new)
    commits = int(count_raw) if count_raw and count_raw.isdigit() else 0
    log = _git_text(folder, "log", "-1", "--format=%cI%x1f%s%x1f%h", new) or ""
    bits = (log.split("\x1f") + ["", "", ""])[:3]
    commit = {"iso": bits[0], "date": _format_commit_date(bits[0]),
              "subject": bits[1], "short": bits[2]}
    if files:
        message = "Pulled %s." % _plural(len(files), "changed file")
    else:
        message = "Pulled %s (no file changes)." % _plural(commits, "new commit")
    detail = "Latest commit, %s: %s" % (commit["date"], commit["subject"])
    if commits > 1:
        detail += " (%s pulled)" % _plural(commits, "commit")
    result.update(status="updated", message=message, detail=detail, files=files,
                  file_count=len(files), commit_count=commits, commit=commit)
    return result


# ---------------------------------------------------------------------------
# Automatic update check for a study folder (2026-10-01).
#
# When a project / config is selected, the faces ask (in the background, once per
# selection) whether the study folder is behind its GitHub copy. The check runs
# ``git fetch``: fetch only DOWNLOADS what is new on GitHub into git's own
# storage; it does NOT change the experiment's files (a pull is fetch + applying
# it, and only the user's click on Git pull does that). Then HEAD is compared to
# its upstream.
#
# It is QUIET by design: not a repo, no upstream, offline or a timeout all show
# NOTHING -- there is no "a check ran" message, and it can never block selecting
# a project or launching. It also can never ask for a login (prompts are switched
# off). A REJECTED login is the one failure that gets a quiet line (an expired
# lab token would otherwise silently stop every lab PC from seeing updates).
#
# It runs for ANY git repository with an upstream, whatever the GitHub
# Organisation Sync setting (Julian, 2026-10-01): "do not run a session on an old
# version" does not depend on an organisation. That setting only decides whether
# the GitHub (clone) button is shown, and for which organisation.
# ---------------------------------------------------------------------------

# How long the background fetch may take before the check gives up silently.
GIT_UPDATE_CHECK_TIMEOUT = 8
# A check older than this is repeated when Launch is pressed (never blocking).
GIT_UPDATE_RECHECK_SECONDS = 600

UPDATE_STATE_NONE = "none"                    # show nothing
UPDATE_STATE_CURRENT = "current"              # quiet "Experiment up to date · checked HH:MM"
UPDATE_STATE_BEHIND = "behind"                # banner + Update (the existing Git pull)
UPDATE_STATE_LOCAL_CHANGES = "local_changes"  # cannot be moved forward: Get a fresh copy
UPDATE_STATE_NO_ACCESS = "no_access"          # the login was rejected: one grey line

# Since 2026-10-07 the wording names GitHub and git pull, to match the README
# and docs (the check still runs for any git repository with an upstream).
GIT_UPDATE_AVAILABLE_TEXT = "A newer version of this study is available on GitHub."
GIT_UP_TO_DATE_TEXT = "Experiment up to date"
GIT_LOCAL_CHANGES_TEXT = ("This folder has changes of its own that are not in the "
                          "online copy, so it cannot be updated here.")
GIT_PRELAUNCH_UPDATE_WARNING = ("A newer version of this study is available on GitHub. "
                                "Git pull before launch.")
GIT_NO_ACCESS_TEXT = "Could not check for a newer version: the login was not accepted."
# The banner / pre-launch button and the standing button in the status box run
# the same git pull, so they carry the same label.
GIT_UPDATE_ACTION_LABEL = "Git pull"
GIT_PULL_BUTTON_LABEL = "Git pull"
GIT_FRESH_COPY_LABEL = "Get a fresh copy"
# The one-click shortcut's line for the same state.
HEADLESS_UPDATE_PROBLEM = ("A newer version of this study is available on GitHub "
                           "(not downloaded yet).")


def _run_git_no_prompt(args, timeout):
    """``git <args>`` that can NEVER ask for a login: terminal prompts off, Git
    Credential Manager's window off, ssh in batch mode. Used by the background
    update check, where a login window popping up unasked would be worse than no
    answer. Returns ``(returncode, output)``; raises what subprocess raises."""
    env = _no_prompt_env()
    result = _timed_run(subprocess.run, ["git"] + list(args), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, timeout=timeout, creationflags=_no_window_flags())
    output = result.stdout or b""
    if isinstance(output, (bytes, bytearray)):
        output = output.decode("utf-8", "replace")
    return result.returncode, output


def _upstream_remote(folder):
    """``(remote, branch, url)`` of the branch this folder follows, or
    ``("", "", "")`` when it follows none. Never raises."""
    upstream = _git_text(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                         "@{u}", timeout=10) or ""
    if "/" not in upstream:
        return "", "", ""
    remote, branch = upstream.split("/", 1)
    url = _git_text(folder, "remote", "get-url", remote, timeout=10) or ""
    return remote, branch, url


def git_remote_host(url):
    """The host name of a git remote URL ("github.com"), "" for a local path."""
    url = str(url or "").strip()
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)", url)
    if match:
        return match.group(1).lower()
    match = re.match(r"^(?:[^@/]+@)?([A-Za-z0-9.-]+\.[A-Za-z]{2,}):", url)   # git@host:owner/repo
    return match.group(1).lower() if match else ""


def _remote_is_github(folder):
    """True when the folder's upstream (else its origin) is on github.com: only
    then is the GitHub login dialog the right thing to offer."""
    try:
        url = _upstream_remote(folder)[2] or \
            (_git_text(folder, "remote", "get-url", "origin", timeout=10) or "")
    except Exception:
        return False
    return git_remote_host(url) == GITHUB_CREDENTIAL_HOST


def _tracked_changes(folder, timeout=10):
    """The tracked files with local edits in ``folder`` (``git status
    --porcelain``, untracked left out), or None when git cannot say. Uses the
    RAW output: a stripped one loses the first line's status column."""
    try:
        code, porcelain = _run_git(["-C", str(folder), "status", "--porcelain",
                                    "--untracked-files=no"], timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return _porcelain_paths(porcelain) if code == 0 else None


def _porcelain_paths(porcelain):
    """The paths named by ``git status --porcelain`` text (both sides of a
    rename), with untracked ("??") and ignored ("!!") lines skipped."""
    paths = []
    for line in (porcelain or "").splitlines():
        if len(line) < 4 or line[:2] in ("??", "!!"):
            continue
        for part in line[3:].split(" -> "):
            part = part.strip().strip('"')
            if part:
                paths.append(part)
    return paths


def _pull_overlap(folder):
    """The local files a fast-forward to the upstream would run into, or None
    when that cannot be worked out (the caller then stays careful).

    Two kinds: a tracked file edited here that also changed online, and a new
    file online whose name already exists here as an untracked file. Local edits
    to OTHER files do not stop a pull, so they are not listed: this is what lets
    "Get ready for the lab" (which edits settings.py) keep updates working."""
    changed = _tracked_changes(folder)
    if changed is None:
        return None
    top = _git_text(folder, "rev-parse", "--show-toplevel", timeout=10)
    incoming = git_changed_files(folder, "HEAD", "@{u}")
    if not top or not incoming:
        return None
    dirty = set(changed)
    overlap = []
    for entry in incoming:
        names = [entry["path"]] + ([entry["old_path"]] if entry.get("old_path") else [])
        if any(name in dirty for name in names):
            overlap.append(entry["path"])
        elif entry["change"] == "added" and os.path.lexists(os.path.join(top, entry["path"])):
            overlap.append(entry["path"])
    return overlap


def _local_changes_message(files):
    """'settings.py was changed on this computer and in the newer version.'"""
    files = list(files)
    return "%s %s changed on this computer and in the newer version." % (
        _short_file_list(files, limit=3), "was" if len(files) == 1 else "were")


def project_update_status(folder, timeout=GIT_UPDATE_CHECK_TIMEOUT, now=None):
    """Is this study folder behind its online copy? For the background check both
    faces run when a project / config is selected (any git repository, whatever
    the GitHub Organisation Sync setting). Returns::

        {"state", "is_repo", "message", "action", "action_label", "behind",
         "ahead", "dirty", "checked", "checked_at", "files", "reason"}

    ``state`` is one of:

      * ``"none"``          -> show NOTHING: not a git repo, the launcher's own
                               folder, no upstream branch, git missing, offline,
                               or the fetch timed out.
      * ``"current"``       -> nothing to pull. ``message`` is "Experiment up to
                               date · checked HH:MM".
      * ``"behind"``        -> the online copy has newer commits and a pull will
                               work: ``message`` is the banner question and
                               ``action`` is "git_pull" (the existing Git pull).
                               Local edits to files the newer version does not
                               touch do NOT stop this (e.g. the lab block that
                               "Get ready for the lab" appended).
      * ``"local_changes"`` -> newer commits online, but this folder cannot be
                               moved forward: a file edited here also changed
                               online (``reason`` "edited", ``files`` names
                               them), or the folder has commits of its own
                               (``reason`` "own_commits"). ``action`` is
                               "fresh_copy": :func:`git_fresh_copy`.
      * ``"no_access"``     -> the remote rejected this computer's login (an
                               expired token, for example). One quiet line;
                               ``action`` is "github_login" for github.com.

    ``is_repo`` is True for any git working tree (even when the remote cannot be
    reached), so the faces can keep the Git pull button for a repo and hide it for
    a plain folder. The fetch never changes the working files and never prompts.
    Bounded by ``timeout``; never raises."""
    folder = str(folder or "").strip()
    out = {"state": UPDATE_STATE_NONE, "is_repo": False, "message": "", "action": "",
           "action_label": "", "behind": 0, "ahead": 0, "dirty": False, "checked": "",
           "checked_at": 0.0, "files": [], "reason": ""}
    if not folder or not os.path.isdir(folder):
        return out
    try:
        if _is_launcher_folder(folder) or not is_git_repo(folder):
            return out
    except Exception:
        return out
    out["is_repo"] = True
    _remote, _branch, url = _upstream_remote(folder)
    if not _remote:
        return out                       # no upstream branch: nothing to compare with
    try:
        code, output = _run_git_no_prompt(["-C", folder, "fetch", "--quiet"], timeout)
        if code != 0:   # another token may open it (see git_update_study)
            retry, again = _retry_with_other_tokens(url, lambda extra: _run_git_no_prompt(
                extra + ["-C", folder, "fetch", "--quiet"], timeout), output)
            if retry is not None:
                code, output = retry, again
    except (OSError, ValueError, subprocess.SubprocessError):
        return out                       # git missing / timeout: say nothing
    moment = now or _dt.datetime.now()
    stamp = moment.strftime("%H:%M")
    if code != 0:
        # Offline, a gone remote, ...: say nothing. A REJECTED LOGIN is the one
        # failure worth a line: a lab token is made to expire, and on that day
        # every lab PC would otherwise silently stop seeing updates.
        host = git_remote_host(url)
        why = classify_git_error(output)["code"]
        if host and (why == "auth" or why == "not_found"):
            github = host == GITHUB_CREDENTIAL_HOST
            out.update(state=UPDATE_STATE_NO_ACCESS, message=GIT_NO_ACCESS_TEXT,
                       action="github_login" if github else "",
                       action_label=GITHUB_LOGIN_ACTION_LABEL if github else "",
                       checked=stamp, checked_at=time.time())
        return out
    counts = _git_text(folder, "rev-list", "--left-right", "--count", "HEAD...@{u}",
                       timeout=10)
    try:
        ahead, behind = [int(x) for x in (counts or "").split()]
    except ValueError:
        return out
    porcelain = _git_text(folder, "status", "--porcelain", "--untracked-files=no",
                          timeout=10)
    dirty = bool((porcelain or "").strip())
    out.update(behind=behind, ahead=ahead, dirty=dirty, checked=stamp,
               checked_at=time.time())
    if behind <= 0:
        out.update(state=UPDATE_STATE_CURRENT,
                   message="%s · checked %s" % (GIT_UP_TO_DATE_TEXT, stamp))
        return out
    if ahead > 0:
        out.update(state=UPDATE_STATE_LOCAL_CHANGES, message=GIT_LOCAL_CHANGES_TEXT,
                   reason="own_commits", action="fresh_copy",
                   action_label=GIT_FRESH_COPY_LABEL)
        return out
    overlap = _pull_overlap(folder)
    if overlap is None and dirty:
        overlap = _tracked_changes(folder) or ["this folder"]   # could not tell: stay careful
    if overlap:
        out.update(state=UPDATE_STATE_LOCAL_CHANGES, files=list(overlap),
                   message=_local_changes_message(overlap), reason="edited",
                   action="fresh_copy", action_label=GIT_FRESH_COPY_LABEL)
    else:
        out.update(state=UPDATE_STATE_BEHIND, message=GIT_UPDATE_AVAILABLE_TEXT,
                   action="git_pull", action_label=GIT_UPDATE_ACTION_LABEL)
    return out


def update_status_after_pull(pull_result, now=None):
    """The update status to show after the Git pull action ran: a successful pull
    turns the banner into "Experiment up to date · checked HH:MM"; a failed one
    returns None (the face keeps its Git pull result, which says why)."""
    if not (pull_result or {}).get("ok"):
        return None
    stamp = (now or _dt.datetime.now()).strftime("%H:%M")
    return {"state": UPDATE_STATE_CURRENT, "is_repo": True, "action": "",
            "action_label": "", "files": [], "reason": "",
            "message": "%s · checked %s" % (GIT_UP_TO_DATE_TEXT, stamp),
            "behind": 0, "ahead": 0, "dirty": False, "checked": stamp,
            "checked_at": time.time()}


def update_status_is_stale(status, now=None, max_age=GIT_UPDATE_RECHECK_SECONDS):
    """True when a check should be repeated before a launch: there is no answer
    yet, or it is older than ``max_age`` seconds (a config selected in the
    morning and launched in the afternoon). Used when Launch is pressed; the
    repeat runs in the background and never blocks the launch screen."""
    stamp = 0.0
    try:
        stamp = float((status or {}).get("checked_at") or 0.0)
    except (TypeError, ValueError):
        stamp = 0.0
    if not stamp:
        return True
    return ((now if now is not None else time.time()) - stamp) > max_age


def prelaunch_update_warning(status):
    """The AMBER pre-launch item when a newer version is known to be waiting
    online and a pull will work, else "". It warns; it never blocks a launch.
    (The local-changes state adds no item: Julian's decision, 2026-10-01.)"""
    if (status or {}).get("state") == UPDATE_STATE_BEHIND:
        return GIT_PRELAUNCH_UPDATE_WARNING
    return ""


# ---------------------------------------------------------------------------
# "Get a fresh copy" (2026-10-01). When a study folder cannot be moved forward
# (a file edited here also changed online, or the folder has commits of its
# own), the launcher does NOT commit, push, merge, stash or discard anything in
# it. The one thing it offers is what the guide tells a lab to do by hand: clone
# the same repository again into a NEW folder next to the old one, and select
# that. The old folder is left exactly as it is.
# ---------------------------------------------------------------------------

def fresh_copy_plan(folder, today=None):
    """Where a fresh copy of ``folder``'s repository would go. Returns
    ``{"ok", "url", "branch", "top", "rel", "target", "path", "message"}``:
    ``target`` is the new clone folder (next to the repository's top folder,
    never an existing path) and ``path`` the study folder inside it (the same
    sub-folder as now when the study is not the repository's top folder)."""
    folder = str(folder or "").strip()
    plan = {"ok": False, "url": "", "branch": "", "top": "", "rel": "", "target": "",
            "path": "", "message": ""}
    if not folder or not os.path.isdir(folder) or not is_git_repo(folder):
        plan["message"] = "This folder is not a git repository."
        return plan
    if _is_launcher_folder(folder):
        plan["message"] = "This is the launcher's own folder, not a study folder."
        return plan
    remote, branch, url = _upstream_remote(folder)
    if not url:
        url = _git_text(folder, "remote", "get-url", "origin", timeout=10) or ""
        branch = ""
    top = _git_text(folder, "rev-parse", "--show-toplevel", timeout=10) or ""
    if not url or not top:
        plan["message"] = ("This folder is not linked to an online copy, so there is "
                           "nothing to copy from.")
        return plan
    top = os.path.normpath(top)
    rel = os.path.relpath(os.path.realpath(folder), os.path.realpath(top))
    rel = "" if rel in (".", "") else rel
    day = (today or _dt.date.today()).strftime("%Y%m%d")
    base = "%s_fresh_%s" % (os.path.basename(top.rstrip("/\\")) or "study", day)
    parent = os.path.dirname(top)
    target = os.path.join(parent, base)
    number = 2
    while os.path.lexists(target):
        target = os.path.join(parent, "%s_%d" % (base, number))
        number += 1
    plan.update(ok=True, url=url, branch=branch, top=top, rel=rel, target=target,
                path=os.path.join(target, rel) if rel else target)
    return plan


def git_fresh_copy(folder, runner=None, on_phase=None, today=None, researcher=None):
    """Clone ``folder``'s repository again into a NEW folder next to it.

    Returns the same shape as :func:`git_clone_org_repo`
    (``{"ok", "status", "message", "action", "hint", "output", "path"}``):
    on success ``path`` is the study folder inside the new clone, for the face
    to select. NOTHING in the old folder is read for content, changed or
    removed. Never asks for a login; bounded by timeouts; never raises.

    A fresh copy is written to the clone log (fresh_copy True) with
    ``researcher``, or, when that is None, whoever got the old folder (the log),
    else "". The result carries ``token_label``."""
    def outcome(status, message, hint="", output="", path="", action=""):
        return {"ok": status == "ok", "status": status, "message": message,
                "action": action, "hint": hint, "output": (output or "").strip(),
                "path": path, "old_path": str(folder or "").strip()}

    plan = fresh_copy_plan(folder, today=today)
    if not plan["ok"]:
        return outcome("no_source", plan["message"])
    if on_phase is not None:
        try:
            on_phase("cloning")
        except Exception:
            pass
    def clone_args(url):
        args = ["clone"]
        if plan["branch"]:
            args += ["--branch", plan["branch"]]
        return args + [url, plan["target"]]
    try:
        code, output = _run_git(clone_args(plan["url"]), runner=runner, timeout=600,
                                no_prompt=True)
        if code != 0 and classify_git_error(output)["code"] in ("auth", "not_found"):
            # The token in the old folder's URL may be gone: the fresh copy is a
            # NEW clone, so it takes the first token that opens it, in its URL.
            current = github_url_user(plan["url"])
            for url in github_clone_candidates(github_url_with_label(plan["url"], "")
                                               or plan["url"]):
                if url == plan["url"] or (current and github_url_user(url) == current):
                    continue
                shutil.rmtree(plan["target"], ignore_errors=True)
                code, again = _run_git(clone_args(url), runner=runner, timeout=600,
                                       no_prompt=True)
                if code == 0:
                    output = again
                    break
    except FileNotFoundError:
        return outcome("no_git", "git is not installed on this computer.",
                       "Ask the lab manager to install git.")
    except subprocess.TimeoutExpired:
        shutil.rmtree(plan["target"], ignore_errors=True)   # drop a half-made clone
        return outcome("timeout", "Getting a fresh copy took too long and was stopped.",
                       "Check the internet connection, then try again.")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        shutil.rmtree(plan["target"], ignore_errors=True)
        return outcome("error", "Could not run git clone.", str(error))
    if code != 0:
        why = classify_git_error(output)
        status = why["code"] if why["code"] in ("auth", "not_found", "network") else "error"
        return outcome(status, "Could not get a fresh copy.", why["reason"], output)
    if not os.path.isdir(plan["path"]):
        return outcome("error", "The fresh copy does not contain this study folder.",
                       "", output)
    used = _git_text(plan["target"], "remote", "get-url", "origin", timeout=10) or plan["url"]
    label = github_url_user(used) or ""
    owner, name = _repo_owner_and_name(used)
    who = researcher if researcher is not None else clone_researcher_for(folder)
    record_clone(owner, name, who, plan["path"], token_label=label, fresh_copy=True)
    result = outcome("ok", "Fresh copy saved in %s. The old folder was not changed."
                     % plan["target"], output=output, path=plan["path"])
    result["token_label"] = label
    return result


def _repo_owner_and_name(url):
    """``(owner, name)`` from a remote URL (https / ssh / a local path: owner ""
    when there is none)."""
    text = str(url or "").strip().rstrip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    parts = [p for p in re.split(r"[/:]", text) if p]
    name = parts[-1] if parts else ""
    owner = parts[-2] if len(parts) >= 2 and git_remote_host(url) else ""
    return owner, name


def study_version(folder):
    """``{"commit", "date"}`` (short id + commit date) of the study folder's
    current version, or None when it is not a git repository. Recorded with each
    launch so a dataset can be tied to the code that produced it. Local and
    quick (no network); never raises."""
    folder = str(folder or "").strip()
    try:
        if not folder or not os.path.isdir(folder) or not is_git_repo(folder):
            return None
        line = _git_text(folder, "log", "-1", "--format=%h%x1f%cI", timeout=5)
    except Exception:
        return None
    if not line:
        return None
    bits = (line.split("\x1f") + [""])[:2]
    version = {"commit": bits[0], "date": bits[1]}
    try:
        porcelain = _git_text(folder, "status", "--porcelain", "--untracked-files=no",
                              timeout=5)
        if (porcelain or "").strip():
            version["local_edits"] = True
    except Exception:
        pass
    return version


def study_version_label(version):
    """'a1b2c3d · 28 Sep 2026' (+ ' + local edits') for the launch history, ""
    when there is no version."""
    if not version or not version.get("commit"):
        return ""
    date = _format_commit_date(version.get("date") or "")
    text = version["commit"] + (" · " + date.split(",")[0] if date else "")
    if version.get("local_edits"):
        text += " + local edits"
    return text


def git_file_change_line(entry):
    """One plain line for a changed file, for text renderers:
    'modified  app/pages.py  (+2 -1)'."""
    counts = ""
    if entry.get("added") is not None or entry.get("removed") is not None:
        counts = "  (+%s -%s)" % (entry.get("added") or 0, entry.get("removed") or 0)
    path = entry.get("path", "")
    if entry.get("old_path"):
        path = "%s -> %s" % (entry["old_path"], path)
    return "%-9s %s%s" % (entry.get("change", "modified"), path, counts)


# The gitignored scratch folder at the repo root (``local/``). The clone folder
# pickers OPEN here, instead of wherever the process happens to run (the launch
# scripts cd to the repo root, so a quick "Choose" used to drop a clone straight
# into the launcher checkout). Anything put in local/ is never committed or pushed.
SCRATCH_DIRNAME = "local"


def scratch_dir():
    """The repo-root scratch folder (gitignored) for test studies and clones."""
    return os.path.join(repo_root(), SCRATCH_DIRNAME)


def default_clone_parent():
    """The folder the clone destination picker opens in: :func:`scratch_dir`,
    created on demand. Returns "" if it cannot be created (the picker then keeps
    its own default). Only the picker's STARTING folder; the user still chooses."""
    path = scratch_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return ""
    return path


CLONE_PARENT_KEY = "clone_parent"


def clone_parent_default():
    """The folder a clone is saved into unless the user changes it: the folder
    used LAST time on this computer (machine.json) when it still exists, else
    the launcher's git-ignored ``local/`` scratch folder. So a clone is: type
    the name, Enter. "" only when neither is available."""
    try:
        last = str(load_machine().get(CLONE_PARENT_KEY) or "").strip()
    except Exception:
        last = ""
    if last and os.path.isdir(last):
        return last
    return default_clone_parent()


def remember_clone_parent(path):
    """Remember the parent folder of a successful clone for next time (this
    computer only). Fail-soft."""
    path = str(path or "").strip()
    if not path:
        return
    try:
        update_machine(lambda m: m.__setitem__(CLONE_PARENT_KEY, path))
    except Exception:
        pass


_GITHUB_LINK_RE = re.compile(
    r"^(?:https?://(?:[^@/]*@)?(?:www\.)?github\.com/|git@github\.com:|github\.com/)"
    r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?(?:[?#].*)?$", re.IGNORECASE)


def clone_target(org, repo):
    """``(owner, name)`` to clone for what the user typed. A plain name (or
    ``something/name``) is looked up in the configured organisation, as before.
    A FULL github.com link (``https://github.com/owner/name``,
    ``git@github.com:owner/name.git``) is used as it is, so a pasted link to a
    repository in another organisation is not silently looked up in the wrong
    place."""
    text = str(repo or "").strip()
    match = _GITHUB_LINK_RE.match(text)
    if match:
        return match.group(1), clone_repo_name(match.group(2))
    return str(org or "").strip().strip("/"), clone_repo_name(text)


def github_clone_url(org, repo, url_template=None):
    """The HTTPS clone URL for ``<org>/<repo>``. The repo name is stripped of a
    trailing ``.git`` and any surrounding whitespace so a user can type either
    form. Raises ValueError when the org or repo is empty. ``url_template``
    (default GITHUB_URL_TEMPLATE) lets tests point at a local folder of bare
    repos instead of github.com."""
    org = str(org or "").strip().strip("/")
    repo = clone_repo_name(repo)
    if not org:
        raise ValueError("Set the GitHub organisation name in Settings > GitHub "
                         "Organisation Sync first.")
    if not repo:
        raise ValueError("Enter the experiment repository name.")
    return (url_template or GITHUB_URL_TEMPLATE) % (org, repo)


def clone_repo_name(repo):
    """The bare repo (and new folder) name from what the user typed: surrounding
    whitespace, slashes and a trailing ``.git`` are dropped, and only the last
    path segment is kept."""
    repo = str(repo or "").strip().strip("/")
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return repo.rsplit("/", 1)[-1].strip()


# ---------------------------------------------------------------------------
# GitHub login / token, handed to the SYSTEM credential store
# ---------------------------------------------------------------------------
# The launcher never keeps a GitHub token: git's own credential helper (Git
# Credential Manager -> Windows Credential Manager, osxkeychain -> the macOS
# Keychain) stores it. These helpers only feed that helper through
# ``git credential approve`` / ``reject`` on stdin. The token never goes on a
# command line, into a launcher file, the activity log or a returned message.

GITHUB_CREDENTIAL_HOST = "github.com"
# The username stored with a token when GitHub could not tell us the account
# (offline / check switched off). GitHub ignores the username for token auth
# over HTTPS, so any name works; this is the one GitHub itself documents.
GITHUB_TOKEN_USERNAME = "x-access-token"
# ONE name for the login dialog wherever it is offered (Settings, a failed clone,
# a failed pull, the "could not check" line). It also fits the first time, when
# there is no login yet to be "different" from.
GITHUB_LOGIN_ACTION_LABEL = "GitHub login…"
GITHUB_LOGIN_DIALOG_TITLE = "Add a GitHub token"
GITHUB_LOGIN_SAVE_RETRY_LABEL = "Save and retry"
# Settings > GitHub: the token list (several per computer, 2026-10-01).
GITHUB_ADD_TOKEN_LABEL = "Add token"
GITHUB_DELETE_TOKEN_LABEL = "Delete"
GITHUB_TOKENS_ROW_LABEL = "Tokens"
GITHUB_NO_TOKENS_TEXT = "No token on this computer"
GITHUB_WHO_LABEL = "Added by"
GITHUB_TOKEN_WHO_MISSING = "Enter or pick who is adding this token."
GITHUB_TOKEN_LINK_LABEL = "Create the token on GitHub"
GITHUB_NO_LOGIN_HINT = ("If the name is right, add a login with \"%s\"."
                        % GITHUB_LOGIN_ACTION_LABEL)
# The login dialog's explanation (both faces show it behind the info tip).
GITHUB_LOGIN_DIALOG_NOTE = (
    "Saved in this computer's own credential store (Windows Credential Manager or "
    "the macOS Keychain), never in a launcher file. A computer can hold several "
    "tokens (the lab's read-only token, a researcher's own token); a clone uses "
    "the first one that can open the study. Make the lab token with the lab "
    "account, a member of the organisation.")
# The dialog's one visible line (bold: the action) and the empty-field message.
GITHUB_LOGIN_DIALOG_PROMPT = "Say who is adding the token, paste it, then Save."
GITHUB_LOGIN_MISSING_MESSAGE = "Paste the token."
GITHUB_MAC_NOTE = (
    "The launcher's git never asks for a login by itself: with none stored, a "
    "clone fails with a login error. Add one here (or run gh auth login in "
    "Terminal).")
GITHUB_WINDOWS_NOTE = (
    "The launcher's git never opens a sign-in window by itself: with no login "
    "stored, a clone fails with a login error. Add one here. A login that Git "
    "Credential Manager already holds (a sign-in made in Git Bash, say) is used "
    "as it is.")


def github_platform_note(platform=None):
    """The one platform-specific sentence for the login dialog ("" on Linux)."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return GITHUB_MAC_NOTE
    if platform.startswith("win"):
        return GITHUB_WINDOWS_NOTE
    return ""


def _credential_payload(fields):
    """``key=value`` lines + the blank line that ends a git credential request."""
    return "".join("%s=%s\n" % (key, value) for key, value in fields) + "\n"


def _run_git_credential(action, fields, runner=None, timeout=30):
    """Run ``git credential <action>`` with ``fields`` on stdin.

    Returns ``(returncode, output)``. Prompts are switched off
    (:func:`_no_prompt_env`) and no console window opens on Windows. Raises what
    subprocess raises; the callers turn that into a result."""
    run = runner if runner is not None else subprocess.run
    env = _no_prompt_env()
    result = _timed_run(run, ["git", "credential", action],
                 input=_credential_payload(fields).encode("utf-8"),
                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                 env=env, timeout=timeout, creationflags=_no_window_flags())
    output = getattr(result, "stdout", b"") or b""
    if isinstance(output, (bytes, bytearray)):
        output = output.decode("utf-8", "replace")
    return getattr(result, "returncode", 1), output


def git_credential_helpers(runner=None):
    """The credential helpers git is configured with (``credential.helper``,
    every config level), or None when git cannot be run."""
    try:
        code, output = _run_git(["config", "--get-all", "credential.helper"],
                                runner=runner, timeout=15)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if code not in (0, 1):   # 1 = the key is simply not set
        return None
    return [line.strip() for line in output.splitlines() if line.strip()]


def _scrub(text, secret):
    """``text`` with every copy of ``secret`` replaced (defence in depth)."""
    text = str(text or "")
    if secret:
        text = text.replace(secret, "********")
    return text


def github_forget_login(runner=None, username=""):
    """Forget a stored github.com login (``git credential reject``). With
    ``username`` only the login stored under that name goes (one token of
    several); without it, whatever git's store hands back for github.com.

    Returns ``{"ok", "status", "message"}``; ``status`` is ok / no_git / error."""
    fields = [("protocol", "https"), ("host", GITHUB_CREDENTIAL_HOST)]
    if str(username or "").strip():
        fields.append(("username", str(username).strip()))
    try:
        code, output = _run_git_credential("reject", fields, runner=runner)
    except FileNotFoundError:
        return {"ok": False, "status": "no_git",
                "message": "git is not installed on this computer."}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"ok": False, "status": "error",
                "message": "Could not run git: %s" % error}
    if code != 0:
        return {"ok": False, "status": "error",
                "message": "git could not remove the GitHub token: %s"
                           % (output.strip() or "exit code %s" % code)}
    return {"ok": True, "status": "ok",
            "message": "Removed the GitHub token from this computer."}


def github_save_login(username, token, runner=None):
    """Hand a GitHub token to the system credential store under ``username``.

    First ``git credential reject`` for that SAME username (replace an older
    token stored under it; the other tokens of this computer are left alone),
    then ``git credential approve`` with protocol=https, host=github.com, the
    username and the token as the password -- all on stdin. GitHub ignores the
    username for token auth over HTTPS, so the launcher uses it as the token's
    LABEL (``otree-lab-<id>``); a blank one is stored as
    :data:`GITHUB_TOKEN_USERNAME`. Nothing is written to any launcher file and
    the token is never part of the returned result.

    Returns ``{"ok", "status", "message"}``; ``status`` is ok / missing /
    bad_input / no_git / no_helper / error. ``no_helper``: git has no
    credential helper configured, so it could not remember a login at all."""
    username = str(username or "").strip() or GITHUB_TOKEN_USERNAME
    token = str(token or "").strip()
    if not token:
        return {"ok": False, "status": "missing",
                "message": GITHUB_LOGIN_MISSING_MESSAGE}
    if any(ch in value for value in (username, token) for ch in "\r\n\0"):
        return {"ok": False, "status": "bad_input",
                "message": "The token contains a line break. Paste it again as one "
                           "line."}
    helpers = git_credential_helpers(runner=runner)
    if helpers is None:
        return {"ok": False, "status": "no_git",
                "message": "git is not installed on this computer (or could not "
                           "be run)."}
    if not helpers:
        return {"ok": False, "status": "no_helper",
                "message": "Not saved: git on this computer has no credential store "
                           "set up, so it cannot remember a token. Install Git for "
                           "Windows (with Git Credential Manager) or use the Mac's "
                           "own git, then try again."}
    forgot = github_forget_login(runner=runner, username=username)
    if not forgot["ok"] and forgot["status"] == "no_git":
        return forgot
    fields = [("protocol", "https"), ("host", GITHUB_CREDENTIAL_HOST),
              ("username", username), ("password", token)]
    try:
        code, output = _run_git_credential("approve", fields, runner=runner)
    except FileNotFoundError:
        return {"ok": False, "status": "no_git",
                "message": "git is not installed on this computer."}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"ok": False, "status": "error",
                "message": _scrub("Could not run git: %s" % error, token)}
    if code != 0:
        return {"ok": False, "status": "error",
                "message": _scrub("git could not store the token: %s"
                                  % (output.strip() or "exit code %s" % code), token)}
    return {"ok": True, "status": "ok",
            "message": "Saved the GitHub token in this computer's credential store."}


# ---------------------------------------------------------------------------
# Several GitHub tokens per computer (2026-10-01)
#
# A computer can hold several tokens: the lab's read-only token plus, say, a
# researcher's own token for a private repository elsewhere. Each token lives in
# the SYSTEM credential store under its own username LABEL ``otree-lab-<id>``
# (git's store keeps one secret per host + username, and GitHub ignores the
# username for token auth over HTTPS). machine.json (this computer only) keeps,
# per token, only: id, label, who added it (from the researcher roster), the
# date, the expiry date and what GitHub said when it was checked. NEVER the
# token; the GitHub account a token belongs to is kept for the record but never
# shown (the faces show "Token, added <date> by <researcher>").
#
# Which token does git use?
#   * clone: each token in turn (``git ls-remote``), then the computer's plain
#     login; the clone is made with the label of the first that works IN its
#     URL (https://otree-lab-<id>@github.com/org/repo), so later pulls and
#     update checks ask git's store for that same token by themselves;
#   * pull / update check: as the folder's URL says; when GitHub refuses (a
#     deleted token, an older clone without a label), each other token is tried
#     for that one command (``git -c url.<labelled>.insteadOf=<url>``): nothing
#     in the study folder is changed;
#   * the clone dialog's repository list: what EVERY token can see, merged.
#
# Saving a token asks GitHub ONCE (with the token just typed) before anything is
# stored: accepted -> stored with its expiry date; rejected -> NOTHING stored;
# unchecked (GitHub unreachable) -> stored, and the message says so.
#
# An install from before (ONE login stored, username = a real GitHub account or
# x-access-token, no list in machine.json) keeps working and shows as one token
# "added before <date> by unknown" until it is deleted or replaced.
# ---------------------------------------------------------------------------

GITHUB_API_URL = "https://api.github.com"
# Where the GitHub API lives; overridable for the test suite and the UI walks
# (a local stand-in server), so no real token ever leaves a test.
GITHUB_API_ENV = "OTREE_LAB_GITHUB_API"
GITHUB_API_TIMEOUT = 6.0
GITHUB_LOGIN_INFO_KEY = "github_login"
# Start warning this many days before the stored token's expiry date.
GITHUB_TOKEN_WARN_DAYS = 14
# Set (to anything) to save a login WITHOUT asking GitHub first: for a computer
# that can reach its git server but not api.github.com, and for the test suite.
GITHUB_NO_TOKEN_CHECK_ENV = "OTREE_LAB_NO_TOKEN_CHECK"
GITHUB_TOKEN_REJECTED_MESSAGE = ("GitHub did not accept this token (mistyped, expired "
                                 "or revoked). Nothing was saved.")


def _github_api_get(path, token=None, timeout=GITHUB_API_TIMEOUT):
    """GET ``https://api.github.com<path>``. Returns ``(status, headers, data)``
    with lower-case header names and the parsed JSON body (None when it is not
    JSON). An HTTP error status is RETURNED, not raised; a network failure
    raises (OSError / URLError). The token only ever travels in the
    Authorization header of this one HTTPS request."""
    import urllib.error
    import urllib.request
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "otree-lab-launcher"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    base = (os.environ.get(GITHUB_API_ENV) or GITHUB_API_URL).rstrip("/")
    request = urllib.request.Request(base + path, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout,
                                          context=_https_ssl_context())
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = getattr(response, "status", None) or response.getcode()
        found = {str(k).lower(): str(v) for k, v in response.headers.items()}
        raw = response.read()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        data = None
    return status, found, data


def _token_expiry_date(headers):
    """The token's expiry as ``YYYY-MM-DD`` from GitHub's
    ``github-authentication-token-expiration`` response header ("" when the
    token has no expiry or the header is absent / unreadable)."""
    raw = str((headers or {}).get("github-authentication-token-expiration") or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    return match.group(1) if match else ""


def github_token_check_enabled():
    return not os.environ.get(GITHUB_NO_TOKEN_CHECK_ENV)


def github_check_token(token, fetcher=None):
    """Ask GitHub whether it accepts ``token``. Returns
    ``{"state", "login", "expires"}``: ``state`` is "accepted" (``login`` is the
    account GitHub knows the token as, ``expires`` its expiry date or ""),
    "rejected" (HTTP 401: mistyped, expired or revoked) or "unchecked" (GitHub
    could not be reached, or answered something unexpected). ``fetcher(path,
    token)`` replaces the HTTPS request in tests. Never raises; the token is in
    no returned value."""
    out = {"state": "unchecked", "login": "", "expires": ""}
    try:
        status, headers, data = (fetcher or _github_api_get)("/user", token)
    except Exception:
        return out
    if status == 401:
        out["state"] = "rejected"
    elif status == 200:
        out.update(state="accepted", expires=_token_expiry_date(headers),
                   login=str((data or {}).get("login") or "") if isinstance(data, dict) else "")
    return out


def format_day(iso_date):
    """'30 Sep 2027' from '2027-09-30' (the text unchanged when unreadable)."""
    try:
        when = _dt.date.fromisoformat(str(iso_date)[:10])
    except (TypeError, ValueError):
        return str(iso_date or "")
    return "%d %s" % (when.day, when.strftime("%b %Y"))


GITHUB_TOKENS_KEY = "github_tokens"
GITHUB_TOKEN_LABEL_PREFIX = "otree-lab-"
GITHUB_TOKEN_UNKNOWN_BY = "unknown"


def _legacy_github_login_info():
    """The pre-multi-token record (machine.json ``github_login``: username +
    expiry date), read ONLY to carry its expiry date over to the adopted token."""
    try:
        info = load_machine().get(GITHUB_LOGIN_INFO_KEY)
    except Exception:
        info = None
    info = info if isinstance(info, dict) else {}
    return {"username": str(info.get("username") or ""),
            "token_expires": str(info.get("token_expires") or "")}


def _normalize_token_entry(raw):
    if not isinstance(raw, dict) or not str(raw.get("id") or "").strip():
        return None
    return {"id": str(raw.get("id")).strip(),
            "label": str(raw.get("label") or "").strip(),
            "added_by": str(raw.get("added_by") or "").strip(),
            "added_on": str(raw.get("added_on") or "").strip(),
            "seen_before": str(raw.get("seen_before") or "").strip(),
            "expires": str(raw.get("expires") or "").strip(),
            "checked": str(raw.get("checked") or "").strip(),
            "github_account": str(raw.get("github_account") or "").strip(),
            "adopted": bool(raw.get("adopted"))}


def _stored_token_entries():
    try:
        raw = load_machine().get(GITHUB_TOKENS_KEY)
    except Exception:
        raw = None
    entries = [_normalize_token_entry(item) for item in (raw if isinstance(raw, list) else [])]
    return [entry for entry in entries if entry]


def _save_token_entries(entries):
    """Write the token list (machine.json). Also drops the old single-login
    record, which the list replaces. Raises what update_machine raises."""
    value = [dict(entry) for entry in entries]

    def mutate(machine):
        machine[GITHUB_TOKENS_KEY] = value
        machine.pop(GITHUB_LOGIN_INFO_KEY, None)
    update_machine(mutate)


def _new_token_id(existing):
    import secrets
    taken = {entry["id"] for entry in existing}
    while True:
        token_id = secrets.token_hex(3)
        if token_id not in taken:
            return token_id


def _adopt_existing_login(runner=None, today=None):
    """An older install (or a sign-in made outside the launcher) has ONE login in
    git's store and no token list: record it as one token "added before <today>
    by unknown", so it shows and can be deleted. Returns the entry or None."""
    username, password = _github_stored_credential(runner=runner)
    if not password:
        return None
    legacy = _legacy_github_login_info()
    entry = _normalize_token_entry({
        "id": "before-" + _new_token_id([]), "label": username,
        "seen_before": (today or _dt.date.today()).isoformat(),
        "expires": legacy["token_expires"], "checked": "adopted", "adopted": True})
    try:
        _save_token_entries([entry])
    except Exception:
        pass
    return entry


def github_tokens(adopt=False, runner=None, today=None):
    """The GitHub tokens of this computer (machine.json metadata, never a
    secret), in the order they were added. With ``adopt`` (Settings), an older
    single login found in git's store while the list is empty is taken over as
    one "added before" token (that asks the credential store: only where the
    old "who is logged in" lookup ran)."""
    entries = _stored_token_entries()
    if not entries and adopt:
        found = _adopt_existing_login(runner=runner, today=today)
        if found:
            entries = [found]
    return entries


def _token_expired(entry, today=None):
    try:
        return _dt.date.fromisoformat(entry.get("expires", "")[:10]) < (today or _dt.date.today())
    except (TypeError, ValueError):
        return False


def github_token_who(entry):
    """'added 1 Oct 2026 by Julian' / 'added before 1 Oct 2026 by unknown'."""
    by = entry.get("added_by") or GITHUB_TOKEN_UNKNOWN_BY
    if entry.get("added_on"):
        return "added %s by %s" % (format_day(entry["added_on"]), by)
    if entry.get("seen_before"):
        return "added before %s by %s" % (format_day(entry["seen_before"]), by)
    return "added by %s" % by


def github_token_text(entry, today=None):
    """One token as the faces show it: "Token, added 1 Oct 2026 by Julian ·
    valid until 30 Sep 2027" (expiry only when known). Never the token, never a
    GitHub account name."""
    text = "Token, " + github_token_who(entry)
    if entry.get("expires"):
        text += " · %s %s" % ("expired" if _token_expired(entry, today) else "valid until",
                              format_day(entry["expires"]))
    return text


def github_token_rows(adopt=True, runner=None, today=None):
    """The token list for Settings: ``[{"id", "text", "expired", "confirm"}]``
    (``confirm`` is the in-app question the Delete button asks)."""
    return [{"id": entry["id"], "text": github_token_text(entry, today),
             "expired": _token_expired(entry, today),
             "confirm": github_delete_confirm_text(entry)}
            for entry in github_tokens(adopt=adopt, runner=runner, today=today)]


def github_delete_confirm_text(entry):
    return ("Delete the GitHub token %s? Studies that only this token can open can "
            "no longer be cloned or updated on this computer." % github_token_who(entry))


def github_add_token(token, added_by, checker=None, runner=None, today=None):
    """What the "Add token" dialog's Save does. ``added_by`` (who is adding it,
    from the researcher roster or typed) is required.

    Asks GitHub about the token first (unless the check is switched off): a
    REJECTED token is not stored (``ok`` False, status "rejected"). Otherwise the
    token goes to the credential store under a new label and its metadata to
    machine.json. A token that is already on this computer is not added twice
    (status "duplicate"). Returns ``{"ok", "status", "message", "token"}`` where
    ``token`` is the new row (:func:`github_token_rows` shape); no secret and no
    GitHub account name in it. ``checker`` replaces the GitHub request in tests."""
    token = str(token or "").strip()
    added_by = str(added_by or "").strip()
    if not token:
        return {"ok": False, "status": "missing", "message": GITHUB_LOGIN_MISSING_MESSAGE}
    if not added_by:
        return {"ok": False, "status": "missing_who", "message": GITHUB_TOKEN_WHO_MISSING}
    existing = github_tokens()
    for entry in existing:
        if entry["label"] and _github_stored_credential(runner=runner,
                                                        username=entry["label"])[1] == token:
            return {"ok": False, "status": "duplicate",
                    "message": "This token is already on this computer (%s). Nothing "
                               "new was saved." % github_token_who(entry)}
    check = {"state": "skipped", "login": "", "expires": ""}
    if checker is not None or github_token_check_enabled():
        check = (checker or github_check_token)(token)
    if check["state"] == "rejected":
        return {"ok": False, "status": "rejected", "message": GITHUB_TOKEN_REJECTED_MESSAGE}
    token_id = _new_token_id(existing)
    label = GITHUB_TOKEN_LABEL_PREFIX + token_id
    stored = github_save_login(label, token, runner=runner)
    if not stored.get("ok"):
        return {"ok": False, "status": stored.get("status", "error"),
                "message": stored.get("message", "")}
    entry = _normalize_token_entry({
        "id": token_id, "label": label, "added_by": added_by,
        "added_on": (today or _dt.date.today()).isoformat(),
        "expires": check.get("expires", ""), "checked": check["state"],
        "github_account": check.get("login", "")})
    try:
        _save_token_entries(existing + [entry])
    except Exception as error:
        github_forget_login(runner=runner, username=label)   # keep store + list in step
        return {"ok": False, "status": "error",
                "message": "Could not record the token on this computer: %s" % error}
    message = "Token saved, added by %s." % added_by
    if check["state"] == "accepted":
        message += " GitHub accepts it."
        if check.get("expires"):
            message += " Valid until %s." % format_day(check["expires"])
    elif check["state"] == "unchecked":
        message += " Not checked: GitHub could not be reached."
    return {"ok": True, "status": "ok", "message": message,
            "token": {"id": entry["id"], "text": github_token_text(entry, today),
                      "expired": _token_expired(entry, today),
                      "confirm": github_delete_confirm_text(entry)}}


def github_delete_token(token_id, runner=None):
    """Delete one token: from git's credential store (``git credential reject``
    for its label) and from this computer's list. Returns ``{"ok", "status",
    "message"}``; status "not_found" when no token has that id."""
    entries = _stored_token_entries()
    entry = next((e for e in entries if e["id"] == str(token_id or "")), None)
    if entry is None:
        return {"ok": False, "status": "not_found",
                "message": "That token is no longer on this computer."}
    result = github_forget_login(runner=runner, username=entry["label"])
    if not result.get("ok") and result.get("status") == "no_git":
        return result
    try:
        _save_token_entries([e for e in entries if e["id"] != entry["id"]])
    except Exception as error:
        return {"ok": False, "status": "error",
                "message": "Could not update this computer's token list: %s" % error}
    return {"ok": True, "status": "ok",
            "message": "Deleted the GitHub token %s." % github_token_who(entry)}


def _github_stored_credential(runner=None, username=""):
    """``(username, password)`` git's credential store holds for github.com (for
    ``username`` when given), or ``("", "")``. Asks ``git credential fill`` with
    every prompt switched off, so nothing can pop up. Callers must not keep or
    show the password."""
    run = runner if runner is not None else subprocess.run
    fields = [("protocol", "https"), ("host", GITHUB_CREDENTIAL_HOST)]
    if str(username or "").strip():
        fields.append(("username", str(username).strip()))
    try:
        result = _timed_run(run, ["git", "-c", "core.askPass=", "credential", "fill"],
                     input=_credential_payload(fields).encode("utf-8"),
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     env=_no_prompt_env(), timeout=15,
                     creationflags=_no_window_flags())
    except (OSError, ValueError, subprocess.SubprocessError):
        return "", ""
    if getattr(result, "returncode", 1) != 0:
        return "", ""
    output = getattr(result, "stdout", b"") or b""
    if isinstance(output, (bytes, bytearray)):
        output = output.decode("utf-8", "replace")
    found = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            found[key.strip()] = value
    if not found.get("password"):
        return "", ""
    return found.get("username", ""), found["password"]


def github_token_expiry_notice(today=None, tokens=None):
    """The amber start-up notice about a token's expiry date: "" until
    :data:`GITHUB_TOKEN_WARN_DAYS` days before the FIRST expiry, then one line
    naming that token (who added it, when) and saying when it expires (or that
    it has) and what to do. A silent lab-wide outage becomes a planned renewal."""
    today = today or _dt.date.today()
    if tokens is None:
        tokens = github_tokens()
        if not tokens:
            legacy = _legacy_github_login_info()   # an install not yet looked at in Settings
            if legacy["token_expires"]:
                tokens = [{"id": "", "added_by": "", "added_on": "", "seen_before": "",
                           "expires": legacy["token_expires"]}]
    soonest = None
    for entry in tokens:
        try:
            when = _dt.date.fromisoformat(str(entry.get("expires") or "")[:10])
        except (TypeError, ValueError):
            continue
        if soonest is None or when < soonest[0]:
            soonest = (when, entry)
    if soonest is None or (soonest[0] - today).days > GITHUB_TOKEN_WARN_DAYS:
        return ""
    when, entry = soonest
    name = ("The GitHub token %s" % github_token_who(entry)
            if entry.get("added_by") or entry.get("added_on") else
            "The GitHub token on this computer")
    where = ("Make a new one on GitHub, add it in Settings > GitHub, then delete the "
             "old one.")
    if when < today:
        return ("%s expired on %s: studies it opens can no longer be cloned or "
                "updated. %s" % (name, format_day(when.isoformat()), where))
    return "%s expires on %s. %s" % (name, format_day(when.isoformat()), where)


def github_clone_enabled(sync=None):
    """True when the GitHub (clone) button is shown: an organisation name is set
    and the setting is on. An EMPTY name is OFF (there is no tick box any more:
    the organisation field is the whole setting)."""
    sync = sync if sync is not None else load_github_sync()
    return bool(sync.get("enabled")) and bool(str(sync.get("org") or "").strip())


def save_github_org(org, path=None):
    """The Settings field: a name switches GitHub on for that organisation, an
    empty field switches it off. Returns ``{"enabled", "org"}``."""
    org = str(org or "").strip().strip("/")
    return save_github_sync_settings(bool(org), org, path)


def github_summary(sync=None, tokens=None, today=None):
    """The collapsed GitHub card in Settings: "Off", "demo-lab · no token on this
    computer", "demo-lab · token added 1 Oct 2026 by Julian · valid until 30 Sep
    2027" or "demo-lab · 2 tokens". Never a GitHub account name."""
    sync = sync if sync is not None else load_github_sync()
    if not github_clone_enabled(sync):
        return "Off"
    org = str(sync.get("org") or "").strip()
    tokens = list(tokens if tokens is not None else github_tokens())
    if not tokens:
        return "%s · no token on this computer" % org
    if len(tokens) == 1:
        text = github_token_text(tokens[0], today)
        return "%s · %s" % (org, text[0].lower() + text[1:])
    expired = sum(1 for entry in tokens if _token_expired(entry, today))
    return "%s · %d tokens%s" % (org, len(tokens),
                                 (" (%d expired)" % expired) if expired else "")


def github_token_create_url(org, days=365):
    """GitHub's own "new fine-grained token" page, PREFILLED for the lab token:
    the organisation as resource owner, Contents: Read-only (GitHub adds
    Metadata: Read-only itself) and the expiry. "All repositories" is not among
    GitHub's documented link parameters, so that stays one click on the page."""
    import urllib.parse
    org = str(org or "").strip().strip("/")
    query = [("name", "oTree lab PCs (read-only)"),
             ("description", "Lets the lab computers clone and update studies. "
                             "Read-only.")]
    if org:
        query.append(("target_name", org))
    query += [("expires_in", str(int(days))), ("contents", "read")]
    return ("https://github.com/settings/personal-access-tokens/new?"
            + urllib.parse.urlencode(query))


def _list_org_repos_with(org, token, fetch, limit):
    """One token's view of the organisation (see github_list_org_repos)."""
    out = {"ok": False, "status": "network", "repos": [], "message": ""}
    repos, page = [], 1
    try:
        while len(repos) < limit:
            status, _headers, data = fetch(
                "/orgs/%s/repos?per_page=100&sort=pushed&page=%d" % (org, page),
                token or None)
            if status == 401:
                out.update(status="rejected",
                           message="GitHub did not accept this computer's login.")
                return out
            if status == 404:
                out.update(status="no_org",
                           message="GitHub has no organisation called '%s'." % org)
                return out
            if status != 200 or not isinstance(data, list):
                return out
            for item in data:
                if isinstance(item, dict) and item.get("name"):
                    repos.append({"name": str(item["name"]),
                                  "private": bool(item.get("private")),
                                  "description": str(item.get("description") or "")})
            if len(data) < 100:
                break
            page += 1
    except Exception:
        return out
    if not repos:
        out.update(ok=True, status="empty",
                   message=("This computer's login sees no repositories in %s." % org
                            if token else
                            "No login on this computer: private repositories in %s "
                            "cannot be listed." % org))
        return out
    out.update(ok=True, status="ok", repos=repos[:limit])
    return out


def github_list_org_repos(org, credential=None, fetcher=None, limit=300):
    """The repositories of ``org`` that this computer's GitHub tokens can see,
    newest work first, for the clone dialog's list (typing the name still works).
    With several tokens, what EVERY token sees, merged (one entry per name).

    Returns ``{"ok", "status", "repos": [{"name", "private", "description",
    "token_label"}], "message"}`` (``token_label``: the label of the first token
    whose list had it, "" for the plain login, None when no token was used). ``status``: "ok"; "empty" (no token sees anything there: no
    access, or the token is for another organisation / not approved yet);
    "rejected" (the login was not accepted); "no_org" (no such organisation);
    "network" (GitHub could not be reached: the dialog just shows no list).
    Each stored token is read into memory for its request only; it is never
    returned, logged or written. ``credential`` / ``fetcher`` are test seams."""
    org = str(org or "").strip().strip("/")
    if not org:
        return {"ok": False, "status": "no_org", "repos": [], "message": ""}
    fetch = fetcher or _github_api_get
    if credential is not None:
        labels = [credential[0] if credential else ""]
        secrets_ = [credential[1] if credential else ""]
    else:
        labels = [entry["label"] for entry in github_tokens() if entry["label"]]
        if labels:
            secrets_ = [_github_stored_credential(username=label)[1] for label in labels]
        else:
            labels = [""]
            secrets_ = [_github_stored_credential()[1]]
    results = [_list_org_repos_with(org, token, fetch, limit) for token in secrets_]
    merged, seen = [], set()
    for label, token, result in zip(labels, secrets_, results):
        for repo in result["repos"]:
            if repo["name"].lower() not in seen:
                seen.add(repo["name"].lower())
                # Which token showed it (a LABEL, never the token; "" = the plain
                # login): a clone of a listed repo skips its check and uses it.
                merged.append(dict(repo, token_label=label if token else None))
    if merged:
        return {"ok": True, "status": "ok", "repos": merged[:limit], "message": ""}
    for status in ("empty", "no_org", "rejected", "network"):
        for result in results:
            if result["status"] == status:
                return result
    return results[0]


# The GitHub URL forms the token logic rewrites: https://[user@]github.com/...
_GITHUB_HTTPS_RE = re.compile(r"^https://(?:([^@/]*)@)?github\.com(?=/|$)", re.IGNORECASE)


def github_url_user(url):
    """The username (token label) in an https github.com URL, "" when it has
    none, None when the URL is not an https github.com URL."""
    match = _GITHUB_HTTPS_RE.match(str(url or "").strip())
    if not match:
        return None
    import urllib.parse
    return urllib.parse.unquote(match.group(1) or "")


def github_url_with_label(url, label):
    """``url`` with ``label`` as its username (https://<label>@github.com/...),
    or None when ``url`` is not an https github.com URL. An empty label gives
    the plain URL."""
    url = str(url or "").strip()
    match = _GITHUB_HTTPS_RE.match(url)
    if not match:
        return None
    import urllib.parse
    rest = url[match.end():]
    label = str(label or "").strip()
    who = (urllib.parse.quote(label, safe="") + "@") if label else ""
    return "https://%sgithub.com%s" % (who, rest)


def github_clone_candidates(url):
    """The URLs a NEW clone tries, in order: each token's labelled URL (as the
    tokens were added), then the plain URL (an older login, or a sign-in made
    outside the launcher). Just ``[url]`` for anything that is not an https
    github.com URL, or when there are no tokens."""
    labels = [entry["label"] for entry in github_tokens() if entry["label"]]
    if github_url_user(url) is None or not labels:
        return [url]
    out = [github_url_with_label(url, label) for label in labels]
    plain = github_url_with_label(url, "")
    if plain not in out:
        out.append(plain)
    return out


def github_retry_configs(url):
    """For a pull / fetch that GitHub refused: one ``["-c", "url.<x>.insteadOf=<y>"]``
    per OTHER token, so that one command runs with that token instead of the one
    the folder's URL names. Nothing in the study folder changes. [] when the
    remote is not https github.com or there is no other token."""
    current = github_url_user(url)
    if current is None:
        return []
    match = _GITHUB_HTTPS_RE.match(str(url).strip())
    prefix = str(url).strip()[:match.end()] + "/"
    out = []
    for entry in github_tokens():
        label = entry["label"]
        if label and label != current:
            out.append(["-c", "url.%s.insteadOf=%s" % (
                github_url_with_label("https://github.com/", label), prefix)])
    return out


def _retry_with_other_tokens(url, run, output):
    """After git refused ``url`` (``output``), run ``run(extra_args)`` once per
    other token until one works. Returns ``(code, output)`` of the first success,
    or ``(None, output)`` (the ORIGINAL failure) when none works or a retry does
    not apply (not a login problem, not GitHub, no other token)."""
    if classify_git_error(output)["code"] not in ("auth", "not_found"):
        return None, output
    for extra in github_retry_configs(url):
        try:
            code, again = run(extra)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        if code == 0:
            return code, again
    return None, output


# The clone's "does it exist?" check (git ls-remote). It used to wait 60 s; on
# a Windows lab PC the first one sat the whole minute (Git Credential Manager
# starting up, or a permission window behind the launcher), so stop sooner and
# say what to do.
GIT_PRECHECK_TIMEOUT = 20
CLONE_PRECHECK_TIMEOUT_ACTION = ("GitHub did not answer in time. If a system window "
                                 "asked for permission (Keychain / Credential "
                                 "Manager), allow it and press Retry.")
CLONE_PRECHECK_TIMEOUT_TIP = (
    "The first GitHub request on a computer can be slow: on Windows, Git "
    "Credential Manager starts up (and may show a window of its own); on a Mac, "
    "the Keychain may ask whether git may use the saved token, in a window that "
    "can sit behind the launcher. Retry is usually quick. The activity log shows "
    "how long each git step took.")


def git_clone_org_repo(org, repo, dest_parent, runner=None, url_template=None,
                       on_phase=None, researcher="", known_label=None):
    """Check, then clone, ``https://github.com/<org>/<repo>`` into a new
    ``<repo>`` subfolder of ``dest_parent`` using the machine's already-stored
    read-only git credential (plain git -- the launcher never handles a token).

    Returns ``{"ok", "status", "message", "hint", "output", "path"}``.
    ``status`` is "ok" on success (``path`` is the cloned folder, for the
    auto-select) or one of: ``no_org``, ``no_repo``, ``no_dest``,
    ``dest_missing``, ``exists`` (all refused before any git runs),
    ``not_found``, ``auth``, ``network``, ``no_git``, ``timeout``, ``error``.
    ``message`` is the one bold line for the dialog, ``hint`` a secondary
    sentence and ``output`` git's raw text for a collapsed detail block.

    It first runs a cheap ``git ls-remote`` so a wrong name fails fast with a
    clear not-found message (GitHub gives the SAME answer for a missing repo and
    a private one this login cannot see, so the message covers both). ``on_phase``
    is called with "checking" then "cloning" so a dialog can show progress.
    ``runner`` lets tests inject a fake spawn; ``url_template`` lets them clone
    from local bare repos. Bounded by timeouts; never raises.

    A successful clone is written to the clone log (:func:`record_clone`) with
    ``researcher`` (who got it) and the label of the token used; the result
    carries ``token_label``.

    ``known_label`` (a token label, "" for the plain login): the repository was
    picked from the list that token just fetched from GitHub, which already
    proves it exists and that token can read it, so the ``ls-remote`` check is
    SKIPPED and the clone runs with that token at once (one git + credential
    helper start fewer: on Windows that is Git Credential Manager starting). If
    that clone is refused anyway, the normal check runs after all.
    """
    def outcome(status, message, hint="", output="", path="", action="", **more):
        # ``action`` is the line the dialog shows in BOLD (what to do);
        # ``message`` says what happened, ``hint`` adds a quieter sentence.
        result = {"ok": status == "ok", "status": status, "message": message,
                  "action": action, "hint": hint, "output": (output or "").strip(),
                  "path": path}
        result.update(more)
        return result

    def phase(name):
        if on_phase is not None:
            try:
                on_phase(name)
            except Exception:
                pass

    # A pasted full github.com link names its own owner (clone_target).
    org, name = clone_target(org, repo)
    if not org:
        return outcome("no_org", "Set the GitHub organisation name in Settings > "
                                 "GitHub Organisation Sync first.")
    if not name:
        return outcome("no_repo", "Enter the experiment repository name.")
    url = github_clone_url(org, name, url_template)
    dest_parent = str(dest_parent or "").strip()
    if not dest_parent:
        return outcome("no_dest", "Choose a destination folder for the clone.")
    if not os.path.isdir(dest_parent):
        return outcome("dest_missing",
                       "Destination folder does not exist: %s" % dest_parent)
    target = os.path.join(dest_parent, name)
    if os.path.exists(target):
        # On a lab PC this is the normal case (the study was cloned before), so
        # the dialog offers the existing folder (``existing_path``) first.
        return outcome("exists",
                       "A folder named '%s' already exists in %s." % (name, dest_parent),
                       "Pick another parent folder, or remove or rename that "
                       "folder, then try again.",
                       action="Use that folder, or choose another folder.",
                       existing_path=target if os.path.isdir(target) else "")

    def git_failure(output):
        why = classify_git_error(output)
        if why["code"] == "not_found":
            # GitHub answers "not found" for a private repo this login cannot
            # see too, so the message names both; the dialog offers
            # GITHUB_LOGIN_ACTION_LABEL next to it.
            return outcome("not_found",
                           "No repository called '%s' found in %s, or this login "
                           "has no access to it." % (name, org), "", output,
                           action="Check the name, or use another GitHub login.")
        if why["code"] == "auth":
            return outcome("auth",
                           "Could not open %s/%s: GitHub asked for a login this "
                           "computer does not have (or did not accept)."
                           % (org, name), GITHUB_NO_LOGIN_HINT, output,
                           action="Add a GitHub login for this computer.")
        if why["code"] == "network":
            return outcome("network", "Could not reach GitHub.",
                           "Check that this computer is online, then try again.",
                           output)
        return outcome("error", "Could not clone %s/%s." % (org, name),
                       why["reason"], output)

    def spawn_errors(action):
        return {
            FileNotFoundError: outcome(
                "no_git", "git is not installed on this computer.",
                "Ask the lab manager to install git."),
            subprocess.TimeoutExpired: outcome(
                "timeout", "%s took too long and was stopped." % action,
                "Check the internet connection, then try again."),
        }

    def clone_into(clone_url):
        """``(code, output)`` of the clone, or an outcome dict when git could not
        be run / was too slow."""
        try:
            return _run_git(["clone", clone_url, target], runner=runner, timeout=600,
                            no_prompt=True)
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            shutil.rmtree(target, ignore_errors=True)   # drop a half-made clone
            return spawn_errors("Cloning")[type(error)]
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            shutil.rmtree(target, ignore_errors=True)
            return outcome("error", "Could not run git clone.", str(error))

    def succeeded(clone_url, output):
        if not os.path.isdir(target):
            return outcome("error", "git clone reported success but the folder is "
                                    "missing.", "", output)
        label = github_url_user(clone_url) or ""
        record_clone(org, name, researcher, target, token_label=label)
        return outcome("ok", "Cloned %s/%s into %s." % (org, name, target),
                       output=output, path=target, token_label=label)

    if known_label is not None:
        # Picked from the list this token fetched: it exists and the token can
        # read it, so no separate check.
        direct = github_url_with_label(url, known_label) or url
        phase("cloning")
        done = clone_into(direct)
        if isinstance(done, dict):
            return done
        code, output = done
        if code == 0:
            return succeeded(direct, output)
        if classify_git_error(output)["code"] not in ("auth", "not_found"):
            return git_failure(output)
        shutil.rmtree(target, ignore_errors=True)       # then check as usual

    phase("checking")
    # Several tokens: try each in turn (then the plain login) and clone with the
    # first that can open the repository, its label IN the URL, so later pulls
    # and update checks use that same token by themselves.
    for candidate in github_clone_candidates(url):
        try:
            code, output = _run_git(["ls-remote", candidate, "HEAD"], runner=runner,
                                    timeout=GIT_PRECHECK_TIMEOUT, no_prompt=True)
        except subprocess.TimeoutExpired:
            return outcome("timeout", "Checking %s/%s took longer than %d s and was "
                           "stopped." % (org, name, GIT_PRECHECK_TIMEOUT),
                           action=CLONE_PRECHECK_TIMEOUT_ACTION,
                           tip=CLONE_PRECHECK_TIMEOUT_TIP)
        except FileNotFoundError as error:
            return spawn_errors("Checking the repository")[type(error)]
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return outcome("error", "Could not run git.", str(error))
        if code == 0:
            url = candidate
            break
        if classify_git_error(output)["code"] not in ("auth", "not_found"):
            break                         # offline etc.: another token will not help
    if code != 0:
        return git_failure(output)

    phase("cloning")
    done = clone_into(url)
    if isinstance(done, dict):
        return done
    code, output = done
    if code != 0:
        return git_failure(output)
    return succeeded(url, output)


# ---------------------------------------------------------------------------
# "See the activity log" -> a button (both faces)
# ---------------------------------------------------------------------------

# Label of the button both faces put next to a message that points at the
# in-app activity log panel (not a file). Clicking it reveals the panel:
# expanded, scrolled to the latest lines, briefly highlighted.
ACTIVITY_LOG_BUTTON_LABEL = "See activity log"

# A TRAILING pointer to the log: "See the activity log.", ": see the activity
# log.", "See the log above.", "See the activity log for both paths." ...
# The web page mirrors this pattern in JS (ACTIVITY_LOG_HINT_RE); a test keeps
# the two in step.
ACTIVITY_LOG_HINT_PATTERN = (r"[\s:;,.\-]*\b(?:see|check) the (?:activity )?log"
                             r"(?: above| below)?(?: for [^.!?]{1,40})?\s*[.!]?\s*$")
_ACTIVITY_LOG_HINT_RE = re.compile(ACTIVITY_LOG_HINT_PATTERN, re.IGNORECASE)


def split_activity_log_hint(message):
    """Split a trailing "See the activity log." off a UI message.

    Returns ``(text, has_hint)``. With a hint, ``text`` is the message without
    it (ending in a full stop), and the face renders a
    :data:`ACTIVITY_LOG_BUTTON_LABEL` button right after it instead of the
    sentence. Without one the message comes back unchanged. The activity log
    itself keeps the full sentence (a button there would point at itself)."""
    text = "" if message is None else str(message)
    match = _ACTIVITY_LOG_HINT_RE.search(text)
    if not match:
        return text, False
    head = text[:match.start()].rstrip(" \t:;,-")
    if head and not head.endswith((".", "!", "?")):
        head += "."
    return head, True


def version_footer_lines():
    """The three identity lines for the WHOLE-APP footer (pinned at the bottom of
    the left config sidebar, centred). Identity ONLY -- no update tag; the update
    nudge lives on the Lab Settings page. Shared by both faces so they read
    identically:

        ["oTree Lab Launcher", "version 1.0.0", "by Julian Tait"]
    """
    return [APP_NAME, "version %s" % APP_VERSION, "by %s" % APP_AUTHOR]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def resetdb_command():
    """`otree resetdb`, exactly as the batch file ran it."""
    return ["otree", "resetdb"]


def _applescript_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _prodserver_argv(cfg):
    """``["otree", "prodserver"]`` plus the validated port when one is set.

    oTree 6's ``prodserver`` takes an optional positional ``addrport`` (a port,
    or ``ipaddr:port``), so the CONFIGURED port reaches the real server on every
    platform instead of oTree silently defaulting to 8000. The port is
    :func:`launch_port` (the same validated 1..65535 int the port preflight and
    the opened URLs use); a blank/invalid port omits the arg and lets oTree use
    its own default, so this is version-safe (the arg is only ever a bare port).
    """
    argv = ["otree", "prodserver"]
    port = launch_port(cfg)
    if port is not None:
        argv.append(str(port))
    return argv


def macos_shell_script(cfg, project_path, env_pairs):
    """The shell line that a new macOS Terminal window will run."""
    parts = ["cd " + shlex.quote(project_path)]
    for key, value in env_pairs:
        parts.append("export %s=%s" % (key, shlex.quote(value)))
    parts.append("echo '%s: oTree prodserver. Press Ctrl-C to stop the server.'" % APP_NAME)
    parts.append(" ".join(shlex.quote(part) for part in _prodserver_argv(cfg)))
    return "; ".join(parts)


def build_server_launch(cfg, project_path, env, platform_name=None):
    """How to start `otree prodserver` in a window the researcher can see.

    Returns a dict with the command to run, the platform branch that produced
    it, and the extra Popen arguments that branch needs.  Kept separate from
    the running of it so both branches can be tested off their own platform.
    The configured port (:func:`launch_port`) is passed to prodserver on every
    platform so the server, the readiness probe and the opened URLs agree.
    """
    platform_name = platform_name or sys.platform
    env_pairs = [(key, env[key]) for key in launcher_env_keys(cfg) if key in env]
    argv = _prodserver_argv(cfg)
    prodserver = " ".join(argv)  # e.g. "otree prodserver 8000"

    if platform_name.startswith("win"):
        # cmd /k keeps the console open after the server stops, so the
        # researcher can still read the traceback that killed it.
        inner = 'title oTree Server ({app}) && {prod}'.format(app=APP_NAME, prod=prodserver)
        return {
            "kind": "windows",
            "cmd": ["cmd", "/k", inner],
            "cwd": project_path,
            "creationflags": CREATE_NEW_CONSOLE,
            "shell": False,
            "description": "new console window: cmd /k %s" % prodserver,
        }

    if platform_name == "darwin":
        script = 'tell application "Terminal"\nactivate\ndo script %s\nend tell' % _applescript_string(
            macos_shell_script(cfg, project_path, env_pairs)
        )
        return {
            "kind": "macos",
            "cmd": ["osascript", "-e", script],
            "cwd": project_path,
            "creationflags": 0,
            "shell": False,
            "description": "new Terminal window via osascript",
        }

    for terminal in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        if shutil.which(terminal):
            return {
                "kind": "linux-terminal",
                "cmd": [terminal, "-e"] + argv,
                "cwd": project_path,
                "creationflags": 0,
                "shell": False,
                "description": "new %s window" % terminal,
            }
    return {
        "kind": "linux-background",
        "cmd": list(argv),
        "cwd": project_path,
        "creationflags": 0,
        "shell": False,
        "description": "background process (no terminal emulator found; output goes to this log)",
    }


# ---------------------------------------------------------------------------
# Batch file export
# ---------------------------------------------------------------------------


def export_bat_text(cfg, name="config"):
    """A standalone .bat with the same effect as launching from the app.

    This is a convenience for people who still want a batch file.  The app
    itself never writes or reads one in order to launch.
    """
    c = normalize_config(cfg)
    url = build_url(c)
    lines = [
        "@echo off",
        "REM Generated by %s on %s" % (APP_NAME, _dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        'REM Config: "%s"' % name,
        "REM Editing this file does not change the saved config in the app.",
        "",
    ]

    if c["db_mode"] == DB_MODE_NONE:
        lines += [
            "REM === Database ===",
            "REM This config sets no database, so oTree falls back to its own default.",
            "set DATABASE_URL=",
            "",
        ]
    else:
        lines += [
            "REM === Database ===",
            "set DB_NAME=%s" % c["db_name"],
            "set DB_USER=%s" % c["db_user"],
            "set DB_PASSWORD=%s" % c["db_password"],
            "set DB_HOST=%s" % c["db_host"],
            "set DB_PORT=%s" % c["db_port"],
            # A blank password: "set DB_PASSWORD=" UNSETS the variable, so
            # %DB_PASSWORD% would stay literal text. Leave it out of the URL.
            ("set DATABASE_URL=postgres://%DB_USER%:%DB_PASSWORD%@%DB_HOST%:%DB_PORT%/%DB_NAME%"
             if c["db_password"] else
             "set DATABASE_URL=postgres://%DB_USER%@%DB_HOST%:%DB_PORT%/%DB_NAME%"),
            "",
        ]

    lines += [
        "REM === oTree variables ===",
        "set OTREE_ADMIN_USERNAME=%s" % c["admin_username"],
        "set OTREE_ADMIN_PASSWORD=%s" % c["admin_password"],
    ]
    if c["production"]:
        lines.append("set OTREE_PRODUCTION=1")
    else:
        lines.append("set OTREE_PRODUCTION=")
    if c["auth_level"] in ("STUDY", "DEMO"):
        lines.append("set OTREE_AUTH_LEVEL=%s" % c["auth_level"])
    else:
        lines.append("set OTREE_AUTH_LEVEL=")
    lines.append("")

    lines += [
        "REM === oTree project folder ===",
        'cd /d "%s"' % c["project_path"],
        "",
    ]

    if c["resetdb"]:
        lines += [
            "REM === Reset the database (the y is answered for you) ===",
            "(echo y) | otree resetdb",
            "",
        ]

    lines += [
        "REM === Start the server in a new terminal ===",
        'start "oTree Server" cmd /k %s' % " ".join(_prodserver_argv(c)),
        "",
    ]

    if c["open_browser"]:
        lines += [
            "REM === Wait for prodserver to boot up, then open the page ===",
            "timeout /t %d >nul" % c["wait_seconds"],
            "start %s" % url,
            "",
        ]

    lines += [
        "echo Server starting. This window will close now.",
        "timeout /t 10 >nul",
        "",
    ]
    return "\r\n".join(lines)


# ---------------------------------------------------------------------------
# Live one-click shortcut (launcher-invoked headless run)
# ---------------------------------------------------------------------------


def sanitize_shortcut_name(name):
    """A filesystem-safe base name for a one-click shortcut file.

    Keeps spaces so "Dictator study" stays readable; only strips characters a
    file name cannot carry. Never empty.
    """
    safe = re.sub(r"[^A-Za-z0-9 _.-]", "_", str(name or "")).strip()
    return safe or "config"


def headless_shortcut(config_name, launcher_path, platform_name=None):
    """A one-click shortcut that launches a SAVED config with no UI.

    Unlike :func:`export_bat_text` (a frozen snapshot that bakes the DB password
    into a loose file), this shortcut calls the launcher HEADLESSLY
    (``otree_lab_launcher.py --run "<name>"``). The launcher reads the named
    config from ``presets.json`` at click time, so the secret stays in
    ``presets.json`` and never lands in this file, and the shortcut always
    reflects the latest saved settings.

    Returns ``{"ext", "filename", "content"}``:
      - Windows: a ``.vbs`` that runs the launcher under ``pythonw`` with NO
        console window (the same zero-window trick as ``Start ... .vbs``),
        invoked by absolute path.
      - macOS / other: a ``.command`` shell script that runs it with ``python3``
        (a Terminal window is fine on the Mac, matching the mac launcher).

    The config name and launcher path are the only things written; no database
    password, DATABASE_URL, or admin password is ever put in the file.
    """
    platform_name = platform_name or sys.platform
    name = str(config_name or "").strip()
    launcher_path = os.path.abspath(launcher_path)
    launcher_dir = os.path.dirname(launcher_path)
    # Suggested file NAME only: prefix with "Launch_" so a config "Auction study"
    # is offered as "Launch_Auction study.vbs" (or .command). This changes only
    # the suggested filename -- the shortcut's contents and behaviour are untouched.
    base = "Launch_" + sanitize_shortcut_name(name)
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    if platform_name.startswith("win"):
        # In a VBScript "..." literal a double quote is written by doubling it;
        # the real command-line quotes come from Chr(34), so the two never clash.
        v_name = name.replace('"', '""')
        v_path = launcher_path.replace('"', '""')
        content = "\r\n".join([
            "' oTree Lab Launcher one-click shortcut.",
            "' Runs the SAVED config \"%s\" headlessly, with no window." % v_name,
            "' It calls the launcher under pythonw (no console); the config is read",
            "' from presets.json at click time, so the DB password is NOT in this file.",
            "' Generated by %s on %s." % (APP_NAME, stamp),
            "Option Explicit",
            "Dim sh",
            'Set sh = CreateObject("WScript.Shell")',
            ('sh.Run "pythonw " & Chr(34) & "%s" & Chr(34) & " --run " '
             '& Chr(34) & "%s" & Chr(34), 0, False') % (v_path, v_name),
            "",
        ])
        return {"ext": ".vbs", "filename": base + ".vbs", "content": content}

    # macOS and Linux: a double-clickable .command that finds python3 itself.
    content = "\n".join([
        "#!/bin/bash",
        "# oTree Lab Launcher one-click shortcut.",
        "# Runs the SAVED config %s headlessly (no launcher UI is built)." % shlex.quote(name),
        "# The config is read from presets.json at click time, so the DB password",
        "# is NOT stored in this file.",
        "# Generated by %s on %s." % (APP_NAME, stamp),
        "cd %s || exit 1" % shlex.quote(launcher_dir),
        'for PY in python3 python; do',
        '  if command -v "$PY" >/dev/null 2>&1; then',
        '    exec "$PY" %s --run %s' % (shlex.quote(launcher_path), shlex.quote(name)),
        "  fi",
        "done",
        'echo "Could not find python3 on this machine."',
        'read -r -p "Press return to close this window. "',
        "",
    ])
    return {"ext": ".command", "filename": base + ".command", "content": content}



def find_candidate_label_files(project_path, limit=60):
    """Text files in a project that could be a participant label file.

    Scans the project folder and its immediate subfolders for ``*.txt`` files,
    so the "Use a file from my project" picker can offer them directly (with a
    Browse fallback for anything elsewhere). Returns absolute paths, project
    root first, each folder's files in name order. Nothing is read or changed.
    """
    project_path = (project_path or "").strip()
    if not project_path or not os.path.isdir(project_path):
        return []
    skip = {"__pycache__", "_static", "_templates", ".git", "node_modules", ".idea"}

    def txts(folder):
        found = []
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            return found
        for name in entries:
            if name.startswith("."):
                continue
            path = os.path.join(folder, name)
            if os.path.isfile(path) and name.lower().endswith(".txt"):
                found.append(os.path.abspath(path))
        return found

    out = list(txts(project_path))
    try:
        subs = sorted(os.listdir(project_path))
    except OSError:
        subs = []
    for name in subs:
        if name in skip or name.startswith("."):
            continue
        sub = os.path.join(project_path, name)
        if os.path.isdir(sub):
            out.extend(txts(sub))
        if len(out) >= limit:
            break
    seen, uniq = set(), []
    for path in out:
        if path not in seen:
            seen.add(path)
            uniq.append(path)
        if len(uniq) >= limit:
            break
    return uniq


# ---------------------------------------------------------------------------
# Lab presets (Feature 4)
# ---------------------------------------------------------------------------
# A lab preset is a named location the launcher can serve: an IP address, a
# seat list, a "display" flag (whether it appears in the lab selector), an
# optional spatial "map" (see below), and a geometry hint. Presets live in the
# same presets.json store as configs, under the top-level "lab_presets" key
# (round-tripped through the store's `extra` dict). The lab selector reads the
# DISPLAYED presets. The BUILT-IN labs are seeded from lab_info.json (not from
# hardcoded constants), so any number of labs with any names is supported.

LAB_GEO_GRID = "grid"           # a plain grid, used for a lab with no map
# Kept for backward compatibility with any presets.json that stored these hints.
LAB_GEO_SMALL = "grid_small"
LAB_GEO_LARGE = "grid_large"
LAB_GEOMETRIES = (LAB_GEO_SMALL, LAB_GEO_LARGE, LAB_GEO_GRID)


# --- Lab MAPS (spatial room layouts) ---------------------------------------
# A map is a small JSON file of pure geometry: grid size and a list of cells,
# each {r, c, kind} where kind is "seat", "exp" (experimenter desk) or "wall".
# Seat cells carry NO label, labels come from the lab's own seat list, filled
# in the order the seat cells appear (see build_seatmap_from_map). Maps live as
# their own files in the committed maps/ folder (maps/<name>.json). A lab
# references one with "map": "<name>"; a full inline map object is also accepted
# as a fallback. Because a map is just geometry, several labs can share one file
# (e.g. a real lab links to maps/example_large.json) and anyone can drop their
# own maps/<name>.json and point a lab at it.

MAPS_DIRNAME = "maps"


def maps_dir():
    """The shipped example maps: app/assets/maps/ (read-only). A lab's own maps
    live inline in lab_info.json ``maps`` (1.5.0; data/maps/ is retired)."""
    return os.path.join(assets_dir(), MAPS_DIRNAME)


def _read_map_file(folder, name):
    """The map dict in <folder>/<name>.json, or None (basename guards escapes)."""
    try:
        with open(os.path.join(folder, os.path.basename(name) + ".json"),
                  "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_map_file(name):
    """The map called ``name``: the live lab_info.json ``maps`` table first, then
    the shipped examples (app/assets/maps/<name>.json). None when unknown."""
    name = str(name or "").strip()
    if not name:
        return None
    table = lab_info_maps()
    if isinstance(table.get(name), dict):
        return table[name]
    return _read_map_file(maps_dir(), name)


def resolve_lab_map(map_field, maps_table=None):
    """A lab's spatial map object, or None.

    ``map_field`` is either a full map dict (inline) or a string naming a map.
    A named map resolves against ``maps_table`` (a lab_info ``maps`` table)
    first, then :func:`load_map_file`."""
    if isinstance(map_field, dict):
        return map_field
    if isinstance(map_field, str) and map_field.strip():
        name = map_field.strip()
        if maps_table and isinstance(maps_table.get(name), dict):
            return maps_table[name]
        return load_map_file(name)
    return None


def default_lab_presets():
    """The labs of lab_info.json as lab presets (the composite the faces use).

    Each carries its resolved ``map`` object (plus ``map_name``), its
    ``suggested_database``, and ``display`` from THIS PC's machine.json
    ``shown_labs`` (all shown when the PC never narrowed them; the home lab is
    always shown). Empty when there is no lab_info.json (first run)."""
    info = LAB_INFO or {}
    maps_table = lab_info_maps(info)
    shown = MACHINE.get("shown_labs")
    home = str(MACHINE.get("home_lab") or "")
    presets = []
    for raw in lab_info_labs(info):
        display = True if shown is None else (raw["id"] in shown)
        if raw["id"] == home:
            display = True
        presets.append(normalize_lab_preset({
            "id": raw["id"],
            "name": raw["name"],
            "ip": raw["host"],
            "seats": list(raw["seats"]),
            "display": display,
            "geometry": raw["geometry"],
            "cols": raw["cols"],
            "map": resolve_lab_map(raw["map"], maps_table),
            "map_name": raw["map"],
            "default_room": raw["default_room"],
            "shortcut_label": raw["shortcut_label"],
            "deleted": raw["deleted"],
            "suggested_database": raw.get("suggested_database"),
            "builtin": False,
        }))
    return presets


def reload_lab_info(path=None):
    """Re-read lab_info.json AND machine.json and refresh the module-level state:
    LAB_INFO, MACHINE, this PC's database list + default (LAB_DB), the default
    oTree admin login, the default lab and the DEFAULT_CONFIG seeds.

    Called at startup, after the setup wizard writes the files and after every
    store save, so the app picks changes up without a restart."""
    global LAB_INFO, MACHINE, DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD, DEFAULT_LAB_ID
    LAB_INFO = load_lab_info(path)
    MACHINE = load_machine()
    admin = (LAB_INFO or {}).get("default_admin") or {}
    DEFAULT_ADMIN_USERNAME = str(admin.get("username") or "admin")
    DEFAULT_ADMIN_PASSWORD = str(admin.get("password") or "")
    DEFAULT_LAB_ID = _default_lab_id_from_info(LAB_INFO, MACHINE)
    _publish_databases(MACHINE.get("databases"), MACHINE.get("default_database"),
                       MACHINE.get("pg_admin"))
    DEFAULT_CONFIG.update({
        "admin_username": DEFAULT_ADMIN_USERNAME,
        "admin_password": DEFAULT_ADMIN_PASSWORD, "lab": DEFAULT_LAB_ID,
    })
    return LAB_INFO


def build_seatmap_from_map(map_obj, seats):
    """(rows, cols, cells) for a lab's map object, or None when there is no map.

    Seat labels are filled from ``seats`` in the order the seat cells appear, so
    a lab that references an example map inherits the shape while showing ITS OWN
    seat names. Non-seat cells (kind "exp"/"wall") consume no seat. This is the
    single geometry->drawing converter; both front-ends render from map data.
    """
    if not isinstance(map_obj, dict):
        return None
    seat_iter = iter([str(s) for s in (seats or [])])
    cells = []
    max_r = max_c = 0
    for cell in (map_obj.get("cells") or []):
        try:
            r = int(cell.get("r"))
            c = int(cell.get("c"))
        except (TypeError, ValueError):
            continue
        kind = cell.get("kind", "seat")
        out = {"r": r, "c": c, "kind": kind}
        if cell.get("colspan"):
            out["colspan"] = cell["colspan"]
        if cell.get("rowspan"):
            out["rowspan"] = cell["rowspan"]
        if kind == "seat":
            out["label"] = next(seat_iter, "")
        cells.append(out)
        max_r = max(max_r, r)
        max_c = max(max_c, c)
    rows = int(map_obj.get("rows") or max_r)
    cols = int(map_obj.get("cols") or max_c)
    return rows, cols, cells


def normalize_lab_preset(preset):
    """A lab preset with every field coerced to a known shape."""
    out = dict(preset or {})
    out["id"] = str(out.get("id", "")).strip()
    out["name"] = str(out.get("name", "")).strip()
    out["ip"] = str(out.get("ip", "")).strip()
    seats = out.get("seats", [])
    if isinstance(seats, (list, tuple)):
        out["seats"] = [str(s).strip() for s in seats if str(s).strip()]
    else:
        out["seats"] = []
    out["display"] = bool(out.get("display", True))
    out["builtin"] = bool(out.get("builtin", False))
    # Soft-delete flag: a deleted preset stays in the store JSON (recoverable by
    # hand-editing) but is hidden from every UI list -- the settings table, the
    # main selector and the per-config tiles. See soft_delete_lab_preset.
    out["deleted"] = bool(out.get("deleted", False))
    geo = str(out.get("geometry", "") or "")
    if geo not in LAB_GEOMETRIES:
        geo = LAB_GEO_GRID
    out["geometry"] = geo
    # A resolved spatial map object (or None). Preserved as-is; a lab with no
    # map is drawn as a plain grid from its seat list.
    out["map"] = out.get("map") if isinstance(out.get("map"), dict) else None
    # Optional column count for the plain-grid seat map of an added lab, so its
    # drawing can match the room's shape. 0 means "auto" (the seat board picks).
    try:
        out["cols"] = max(0, int(out.get("cols", 0)))
    except (TypeError, ValueError):
        out["cols"] = 0
    # Per-lab default room name (drives the built-in Lab default config + new
    # configs). Missing/blank -> "study", so an older lab_info.json / store with
    # no room field keeps working unchanged.
    out["default_room"] = sanitize_room_name(out.get("default_room"))
    # Optional human name of the participant-PC desktop shortcuts (e.g.
    # "Experiment"). Surfaced only in the launch briefing; blank is fine.
    out["shortcut_label"] = str(out.get("shortcut_label", "") or "").strip()
    # The NAME of the lab's map in lab_info.json ``maps`` ("" = none), kept next
    # to the resolved ``map`` object so a save writes the name back.
    out["map_name"] = str(out.get("map_name", "") or "").strip()
    # The database name the setup wizard suggests on a new PC of this lab.
    suggested = out.get("suggested_database")
    out["suggested_database"] = (
        {"db_name": str(suggested.get("db_name", "") or "").strip(),
         "db_user": str(suggested.get("db_user", "") or "").strip()}
        if isinstance(suggested, dict) and str(suggested.get("db_name", "") or "").strip()
        else None)
    return out


def lab_default_room(lab_presets, lab_id):
    """The default room for ``lab_id`` among ``lab_presets`` ("study" fallback).

    Backward compatible: a lab preset with no ``default_room`` (an older store or
    lab_info.json) yields :data:`DEFAULT_ROOM_NAME`, and an unknown lab id does
    too, so nothing that resolves a room can break.
    """
    preset = find_lab_preset(lab_id, lab_presets)
    if preset is None:
        return DEFAULT_ROOM_NAME
    return sanitize_room_name(preset.get("default_room"))


def lab_shortcut_label(lab_presets, lab_id):
    """The participant-PC desktop-shortcut label for ``lab_id`` ("" when unset)."""
    preset = find_lab_preset(lab_id, lab_presets)
    if preset is None:
        return ""
    return str(preset.get("shortcut_label", "") or "").strip()


def lab_presets_from_store(extra):
    """The lab presets for this store, backward-compatible and never empty.

    Reads ``extra["lab_presets"]`` when present; an old store with no such key
    (or an empty/garbled one) gets the two seeded built-ins, so every config
    that named ``small``/``large`` still resolves. If every preset has been
    deleted the built-ins are re-seeded, so the launcher can never end up with
    no lab at all.
    """
    raw = (extra or {}).get("lab_presets")
    if not isinstance(raw, list):
        return default_lab_presets()
    presets = [normalize_lab_preset(p) for p in raw if isinstance(p, dict)]
    presets = [p for p in presets if p["id"]]
    if not presets:
        return default_lab_presets()
    return presets


def displayed_lab_presets(lab_presets):
    """Only the presets the lab selector should show, in stored order.

    A soft-deleted preset (``deleted`` True) is never shown."""
    return [p for p in (lab_presets or [])
            if p.get("display", True) and not p.get("deleted")]


def lab_options_for_config(lab_presets, config_lab):
    """The labs a PER-CONFIG lab selector should offer, in stored order.

    Every DISPLAYED lab, PLUS the config's own saved lab even when that lab is
    hidden (``display`` is False), so opening a config that was saved on a
    now-hidden lab never drops or silently reassigns the lab it was saved with.
    No duplicate; a pseudo-host / custom / unknown ``config_lab`` (one that no
    preset carries) contributes nothing, leaving just the displayed labs.

    This is the seam both faces build their per-config selector from, so the Tk
    tiles and the web tiles agree on which labs a given config may pick.
    """
    presets = lab_presets or []
    cid = str(config_lab).strip() if config_lab not in (None, "") else None
    # A soft-deleted preset is never offered, even as a config's own saved lab.
    return [p for p in presets
            if not p.get("deleted")
            and (p.get("display", True)
                 or (cid is not None and str(p.get("id", "")) == cid))]


def selectable_lab_presets(lab_presets):
    """The labs the main selector offers: the displayed ones.

    The display toggle exists to narrow the main UI down to the actual lab a
    machine is, so an admin can hide every lab but one and a researcher on that
    machine cannot pick the wrong lab. As a defence against a hand-edited store
    that hid every lab, this falls back to showing all of them rather than an
    empty selector (``set_lab_display`` also refuses to hide the last one).
    """
    shown = displayed_lab_presets(lab_presets)
    return shown if shown else [normalize_lab_preset(p) for p in (lab_presets or [])]


def default_selected_lab(lab_presets, current=None):
    """Which lab the main selector should have chosen.

    When exactly one lab is displayed it is forced as the selection, so a
    researcher on a single-lab machine cannot pick the wrong lab. Otherwise the
    caller's current choice is kept when it is still selectable, else the first
    selectable lab.
    """
    shown = selectable_lab_presets(lab_presets)
    if not shown:
        return current
    if len(shown) == 1:
        return shown[0]["id"]
    ids = [p["id"] for p in shown]
    if current in ids:
        return current
    return shown[0]["id"]


def find_lab_preset(lab_id, lab_presets):
    """The preset whose id is ``lab_id``, or None.

    With ``lab_presets`` None (the default for the host/seat helpers) this
    returns None, so those helpers fall back to the two hardcoded built-in labs
    and every existing caller keeps its old behaviour.
    """
    if not lab_presets:
        return None
    for preset in lab_presets:
        if str(preset.get("id", "")) == str(lab_id):
            return preset
    return None


def _slugify_lab_id(name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return slug or "lab"


def new_lab_id(name, existing):
    """A fresh preset id derived from the name, unique within ``existing``."""
    base = _slugify_lab_id(name)
    taken = {str(p.get("id", "")) for p in (existing or [])} | {LAB_CUSTOM}
    if base not in taken:
        return base
    n = 2
    while "%s-%d" % (base, n) in taken:
        n += 1
    return "%s-%d" % (base, n)


def parse_seat_list(text):
    """Split a seat list typed as lines, spaces or commas into labels."""
    if isinstance(text, (list, tuple)):
        items = [str(s).strip() for s in text]
    else:
        items = re.split(r"[\s,]+", str(text or "").strip())
    return [s for s in items if s]


def natural_sort_key(label):
    """A key that sorts seat labels the human way: 2 before 10, A1 before A2
    before B1. Digit runs compare as numbers, letter runs as lowercased text,
    and the (rank, value) pairs never compare a str against an int.
    """
    key = []
    for chunk in re.findall(r"\d+|\D+", str(label)):
        if chunk.isdigit():
            key.append((1, int(chunk)))
        else:
            key.append((0, chunk.lower()))
    return key


def sorted_seats(seats):
    """Seat labels in natural order (see natural_sort_key)."""
    return sorted([str(s) for s in (seats or []) if str(s).strip()], key=natural_sort_key)


def validate_lab_preset_fields(name, ip, seats):
    """Check the fields of a new/edited lab preset.

    Returns (ok, message, parsed_seats). Seats may come in as a list or as a
    free-text block; each label must pass oTree's own label rule, so a lab
    preset can never contain a seat the seat board would reject.
    """
    name = (name or "").strip()
    ip = (ip or "").strip()
    if not name:
        return False, "Give the lab a name.", []
    if not ip:
        return False, "Enter the lab's IP address or host name.", []
    labels = parse_seat_list(seats)
    if not labels:
        return False, "Enter at least one seat label.", []
    bad = invalid_seats(labels)
    if bad:
        return False, ("These seat labels are not valid participant labels "
                       "(letters, digits and underscore only): %s"
                       % ", ".join(bad[:8])), []
    dupes = sorted({s for s in labels if labels.count(s) > 1})
    if dupes:
        return False, "These seat labels are repeated: %s" % ", ".join(dupes[:8]), []
    return True, "", labels


def add_lab_preset(lab_presets, name, ip, seats, display=True, geometry=LAB_GEO_GRID, cols=0,
                   default_room=None, shortcut_label=None):
    """Validate and append a new lab preset. Returns (ok, message, new_list, preset).

    ``cols`` is an optional column count for the plain-grid seat map, so an added
    lab can be drawn to roughly match the room's shape (0 = auto). ``default_room``
    is the per-lab room (blank/None -> "study") and ``shortcut_label`` the optional
    participant-PC desktop-shortcut name.
    """
    ok, message, labels = validate_lab_preset_fields(name, ip, seats)
    if not ok:
        return False, message, list(lab_presets or []), None
    presets = [normalize_lab_preset(p) for p in (lab_presets or [])]
    preset = normalize_lab_preset({
        "id": new_lab_id(name, presets),
        "name": name,
        "ip": ip,
        "seats": labels,
        "display": bool(display),
        "geometry": geometry,
        "cols": cols,
        "default_room": default_room,
        "shortcut_label": shortcut_label or "",
        "builtin": False,
    })
    presets.append(preset)
    return True, "Added the lab %r." % preset["name"], presets, preset


def update_lab_preset(lab_presets, lab_id, name=None, ip=None, seats=None, display=None,
                      cols=None, default_room=None, shortcut_label=None):
    """Edit an existing lab preset in place (by id). Returns (ok, message, new_list, preset).

    ``default_room`` / ``shortcut_label`` are only changed when passed (None leaves
    the stored value); an empty string is a real value and clears the shortcut
    label, while a blank room name coerces back to "study".
    """
    presets = [normalize_lab_preset(p) for p in (lab_presets or [])]
    target = None
    for preset in presets:
        if preset["id"] == str(lab_id):
            target = preset
            break
    if target is None:
        return False, "No lab preset with that id.", presets, None
    next_name = target["name"] if name is None else name
    next_ip = target["ip"] if ip is None else ip
    next_seats = target["seats"] if seats is None else seats
    ok, message, labels = validate_lab_preset_fields(next_name, next_ip, next_seats)
    if not ok:
        return False, message, presets, None
    target["name"] = next_name.strip()
    target["ip"] = next_ip.strip()
    target["seats"] = labels
    if display is not None:
        target["display"] = bool(display)
    if cols is not None:
        try:
            target["cols"] = max(0, int(cols))
        except (TypeError, ValueError):
            target["cols"] = 0
    if default_room is not None:
        target["default_room"] = sanitize_room_name(default_room)
    if shortcut_label is not None:
        target["shortcut_label"] = str(shortcut_label or "").strip()
    return True, "Updated the lab %r." % target["name"], presets, target


def set_lab_display(lab_presets, lab_id, display):
    """Show or hide one preset in the lab selector (a per-PC choice, saved as
    machine.json ``shown_labs``). Guarded so the selector is never left with
    nothing to show, and this PC's own lab always stays shown."""
    presets = [normalize_lab_preset(p) for p in (lab_presets or [])]
    target = find_lab_preset(lab_id, presets)
    if target is None:
        return False, "No lab preset with that id.", presets
    if not display and str(lab_id) == (read_lab_marker() or ""):
        return False, "This computer's own lab is always shown.", presets
    if not display:
        others = [p for p in presets if p["id"] != str(lab_id) and p["display"]]
        if not others:
            return False, ("At least one lab must stay visible in the selector, so this one "
                           "cannot be hidden."), presets
    target["display"] = bool(display)
    return True, "", presets


def delete_lab_preset(lab_presets, lab_id, selected_lab=None):
    """Remove a lab preset. Returns (ok, message, new_list, next_selected).

    Refuses to delete the last remaining displayed lab, so the launcher always
    keeps a lab. If the deleted preset was the selected one, the returned
    ``next_selected`` names another displayed lab for the caller to switch to.
    """
    presets = [normalize_lab_preset(p) for p in (lab_presets or [])]
    target = find_lab_preset(lab_id, presets)
    if target is None:
        return False, "No lab preset with that id.", presets, selected_lab
    remaining = [p for p in presets if p["id"] != str(lab_id)]
    if not displayed_lab_presets(remaining):
        return False, ("This is the last lab the selector can show, so it cannot be deleted. "
                       "Add another lab first."), presets, selected_lab
    next_selected = selected_lab
    if str(selected_lab) == str(lab_id):
        shown = displayed_lab_presets(remaining)
        next_selected = shown[0]["id"] if shown else remaining[0]["id"]
    return True, "Deleted the lab %r." % target["name"], remaining, next_selected


def soft_delete_lab_preset(lab_presets, lab_id, selected_lab=None):
    """Soft-delete a lab preset: hide it from the UI but KEEP it in the store.

    Same contract as :func:`delete_lab_preset` -- returns
    (ok, message, new_list, next_selected) and refuses to remove the last
    remaining displayed lab -- but instead of dropping the preset it flags it
    ``deleted``, so it stays in presets.json (recoverable by hand-editing) while
    every UI list (:func:`displayed_lab_presets`, :func:`lab_options_for_config`,
    the settings table) skips it. The returned list still CONTAINS the deleted
    preset, so persisting it keeps the entry in the store.
    """
    presets = [normalize_lab_preset(p) for p in (lab_presets or [])]
    target = find_lab_preset(lab_id, presets)
    if target is None:
        return False, "No lab preset with that id.", presets, selected_lab
    if target.get("deleted"):
        return False, "That lab is already deleted.", presets, selected_lab
    # What would remain VISIBLE if this preset were hidden.
    remaining = [p for p in presets
                 if p["id"] != str(lab_id) and not p.get("deleted")]
    if not displayed_lab_presets(remaining):
        return False, ("This is the last lab the selector can show, so it cannot be deleted. "
                       "Add another lab first."), presets, selected_lab
    for preset in presets:
        if preset["id"] == str(lab_id):
            preset["deleted"] = True
            break
    next_selected = selected_lab
    if str(selected_lab) == str(lab_id):
        shown = displayed_lab_presets(remaining)
        next_selected = shown[0]["id"] if shown else remaining[0]["id"]
    return True, "Deleted the lab %r." % target["name"], presets, next_selected


# ---------------------------------------------------------------------------
# Postgres admin config (Feature 2/4), used ONLY to create databases, never as
# launch environment variables. Stored, like lab presets, in the store's extra.
# ---------------------------------------------------------------------------

PG_ADMIN_KEYS = ("admin_username", "admin_password", "admin_host", "admin_port")

# Hosts we treat as "this machine". Creating a database (CREATE ROLE / CREATE
# DATABASE) is only supported against a local Postgres for now; a remote host may
# be USED (registered) but not created on. See is_local_host below.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


# The one short note both faces show when the Host of a new database is not
# this computer: the create option is disabled and "already exists" is forced.
REMOTE_DB_NOTE = ("Creating a database only works on this computer; a database on "
                  "another host is registered, not created.")


def is_local_host(host):
    """True when ``host`` names this machine's Postgres (localhost/127.0.0.1/::1).

    Trimmed and lowercased before comparison; a blank host counts as local
    (the launcher defaults an empty host to localhost). Anything else is remote.
    """
    h = str(host or "").strip().lower()
    if not h:
        return True
    return h in LOCAL_HOSTS


def database_host_note(entry):
    """"on HOST:PORT" when a database (a config, or a picker entry) lives on
    another computer, else "". Shown in grey next to the database wherever it is
    picked or shown, so a session on another computer's database is obvious."""
    entry = entry or {}
    if entry.get("db_mode", DB_MODE_CUSTOM) == DB_MODE_NONE:
        return ""
    host = str(entry.get("db_host", "") or "").strip()
    if is_local_host(host):
        return ""
    port = str(entry.get("db_port", "") or "").strip() or "5432"
    return "on %s:%s" % (host, port)


def database_summary_label(cfg):
    """The Database card's one-line name for this config: the database's
    nickname (its title) on this PC. A database on another host is named
    without "on HOST", which :func:`database_host_note` shows next to it."""
    c = normalize_config(cfg)
    if c["db_mode"] == DB_MODE_NONE:
        return DB_BUILTIN_SQLITE_TITLE
    entry = None
    if c["database_id"]:
        entry = _DB_REGISTRY.get(c["database_id"]) or _DB_REGISTRY.get(_DEFAULT_DB_ID)
    elif c["db_mode"] == DB_MODE_LAB:
        entry = _DB_REGISTRY.get(_DEFAULT_DB_ID)
    if entry is None and c["db_mode"] == DB_MODE_CUSTOM:
        entry = find_database_by_connection(c)
    if entry is not None:
        return "%s (Postgres)" % (entry.get("title") or entry.get("db_name") or "database")
    name = c["db_name"].strip() or "custom database"
    host = c["db_host"].strip() or "localhost"
    if not is_local_host(host):
        return "%s (Postgres)" % name
    return "%s on %s (Postgres)" % (name, host)


def pg_admin_from_store(extra):
    """The Postgres admin config (machine.json), with sane host/port defaults."""
    raw = (extra or {}).get("pg_admin") or {}
    return {
        "admin_username": str(raw.get("admin_username", "")),
        "admin_password": str(raw.get("admin_password", "")),
        "admin_host": str(raw.get("admin_host", "") or "localhost"),
        "admin_port": str(raw.get("admin_port", "") or "5432"),
    }


def pg_admin_ready(admin):
    """Which required admin fields are still blank (empty list means ready).
    The password is NOT required: a Postgres user may have none (Postgres.app,
    a trust login), see :data:`PG_BLANK_PASSWORD_HINT`."""
    admin = admin or {}
    missing = []
    labels = {"admin_username": "username", "admin_host": "host", "admin_port": "port"}
    for key in PG_ADMIN_KEYS:
        if key == "admin_password":
            continue
        if not str(admin.get(key, "")).strip():
            missing.append(labels[key])
    return missing


# ---------------------------------------------------------------------------
# THIS PC's database list (machine.json ``databases``) + researcher roster.
# Every database this PC uses is a normal entry with its own connection and
# password; which one is the PC default is a REFERENCE (machine.json
# ``default_database``). Configs reference databases by id. The registry NEVER
# connects to Postgres by itself: only create_database() does, and its caller
# then calls register_database() with the confirmed result. The store facade
# carries the list as ``extra["databases"]`` / ``extra["default_database"]``.
# ---------------------------------------------------------------------------

# The id of the one always-present built-in database: oTree's own SQLite.
DB_BUILTIN_SQLITE = "otree_default"
# RETIRED in 1.5.0: the old "Lab shared database" (the lab_info.json database
# block). The migration turns it into a normal database of this PC; the id is
# kept only so old references can be recognised.
DB_BUILTIN_LAB = "lab_shared"

DEFAULT_DATABASE_KEY = "default_database"

DB_BUILTIN_SQLITE_TITLE = "oTree default (SQLite)"
# Kept for old callers; the picker no longer has a "lab shared" entry.
DB_BUILTIN_LAB_TITLE = "This computer's default database"

# The connection fields a database entry carries (kept separate from the person).
DATABASE_CONN_KEYS = ("db_name", "db_user", "db_password", "db_host", "db_port")

# The live registry normalize_config resolves ``database_id`` against: id ->
# entry (not soft-deleted), plus the id of the PC default. Published by
# _publish_databases (from machine.json at load, or from a store ``extra`` via
# apply_default_database after any change).
_DB_REGISTRY = {}
_DEFAULT_DB_ID = ""


def _publish_databases(databases, default_id, pg_admin=None):
    """Make ``databases`` (a machine.json-style list) + ``default_id`` the live
    registry, and point LAB_DB / the DEFAULT_CONFIG db seeds at the default.
    A ``uses_admin_login`` entry gets this PC's ``pg_admin`` login filled in
    here (in memory only), so a launch always uses the CURRENT admin login."""
    global _DB_REGISTRY, _DEFAULT_DB_ID, LAB_DB
    registry = {}
    for raw in databases or []:
        if isinstance(raw, dict):
            entry = resolve_admin_login(normalize_database_entry(raw), pg_admin)
            if entry["id"] and not entry["deleted"]:
                registry[entry["id"]] = entry
    _DB_REGISTRY = registry
    _DEFAULT_DB_ID = str(default_id or "").strip()
    default = registry.get(_DEFAULT_DB_ID)
    LAB_DB = ({key: str(default.get(key, "") or "") for key in DATABASE_CONN_KEYS}
              if default is not None else dict(_DUMMY_DB))
    DEFAULT_CONFIG.update({
        "db_name": LAB_DB["db_name"], "db_user": LAB_DB["db_user"],
        "db_password": LAB_DB["db_password"], "db_host": LAB_DB["db_host"],
        "db_port": LAB_DB["db_port"],
    })
    return LAB_DB


def _slugify_db_id(title):
    slug = re.sub(r"[^a-z0-9]+", "_", str(title or "").lower()).strip("_")
    return slug or "db"


def _unique_db_id(title, existing_ids):
    """A registry id derived from the title, unique among existing ids and never
    colliding with the reserved built-in ids."""
    base = _slugify_db_id(title)
    reserved = set(existing_ids) | {DB_BUILTIN_SQLITE, DB_BUILTIN_LAB}
    candidate = base
    suffix = 2
    while candidate in reserved:
        candidate = "%s_%d" % (base, suffix)
        suffix += 1
    return candidate


def slugify_pg_dbname(name):
    """A Postgres-identifier-safe database name derived from an arbitrary label.

    Used to prefill the Create-a-database dialog's name from the current
    project folder / config name so staff rarely have to type it. The rules:
    lowercase; turn spaces, hyphens and any character outside the Postgres
    identifier set (letters, digits, underscore, dollar) into underscores;
    collapse runs of underscores; strip leading/trailing underscores. A Postgres
    identifier may not start with a digit and may not be empty, so a result that
    is empty or begins with a digit is prefixed with an underscore. The result is
    a valid, unquoted identifier the user can still edit.

    Examples: ``"My Lab-Study 2" -> "my_lab_study_2"``; ``"2024data" -> "_2024data"``.
    """
    text = str(name or "").lower()
    text = re.sub(r"[^a-z0-9_$]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text or text[0].isdigit():
        text = "_" + text
    return text


def normalize_created_on(raw):
    """A database's ``created_on`` stamp ({hostname, ip, home_lab, when}), or {}."""
    if not isinstance(raw, dict):
        return {}
    out = {key: str(raw.get(key, "") or "").strip()
           for key in ("hostname", "ip", "home_lab", "when")}
    if raw.get("migrated"):
        out["migrated"] = True
    return out if any(out.get(k) for k in ("hostname", "ip", "home_lab", "when")) else {}


def normalize_database_entry(raw):
    """One database of this PC as a normalized dict.

    Every field is a string; the connection keys are always present; the creator
    ``researcher`` (a person) is kept distinct from ``postgres_user`` (the
    credential). ``title`` is the nickname shown everywhere. ``created_on`` says
    which computer it was created/registered on."""
    raw = raw or {}
    entry = {
        "id": str(raw.get("id", "")).strip(),
        "title": str(raw.get("title", "")).strip(),
        "researcher": str(raw.get("researcher", "")).strip(),
        "postgres_user": str(raw.get("postgres_user", "") or raw.get("db_user", "")).strip(),
        "database_url": str(raw.get("database_url", "") or ""),
        "created": str(raw.get("created", "") or ""),
        "builtin": False,
        "db_mode": DB_MODE_CUSTOM,
        # Soft-delete flag: a deleted entry stays in machine.json (recoverable by
        # hand-editing) but is hidden from every app UI list. It NEVER means the
        # underlying Postgres database was touched.
        "deleted": bool(raw.get("deleted", False)),
        "created_on": normalize_created_on(raw.get("created_on")),
    }
    for key in DATABASE_CONN_KEYS:
        entry[key] = str(raw.get(key, "") or "")
    # uses_admin_login: the database connects as THIS PC's Postgres admin login
    # (machine.json pg_admin), by REFERENCE. The stored form never holds a copy
    # of the admin user/password; resolve_admin_login fills them in live.
    entry["uses_admin_login"] = bool(raw.get("uses_admin_login", False))
    if entry["uses_admin_login"]:
        entry["db_user"] = ""
        entry["db_password"] = ""
        entry["postgres_user"] = ""
    if not entry["title"]:
        entry["title"] = entry["db_name"] or "custom database"
    return entry


# A database with no user of its own connects as this computer's Postgres admin.
ADMIN_LOGIN_NOTE = "Uses the Postgres admin login from this computer."
ADMIN_LOGIN_MISSING = ("This database uses the Postgres admin login from this "
                       "computer, but no admin login is stored. Add it in Settings > "
                       "Postgres admin.")


def resolve_admin_login(entry, pg_admin):
    """A copy of a database ``entry`` with its login resolved: for a
    ``uses_admin_login`` entry, ``db_user``/``db_password`` come from
    ``pg_admin`` (the CURRENT machine login, so a changed admin login is
    followed) and ``login_missing`` is True when no admin username is stored
    (the entry is then unusable, see :data:`ADMIN_LOGIN_MISSING`). Other
    entries come back unchanged (as a copy). Never meant to be saved."""
    out = dict(entry or {})
    out["login_missing"] = False
    if not out.get("uses_admin_login"):
        return out
    admin = pg_admin or {}
    user = str(admin.get("admin_username", "") or "").strip()
    out["db_user"] = user
    out["db_password"] = str(admin.get("admin_password", "") or "") if user else ""
    out["postgres_user"] = user
    out["login_missing"] = not user
    return out


def known_databases_from_store(extra):
    """This PC's databases (the store's ``extra["databases"]``), normalized, in
    list order, soft-deleted ones skipped. Never includes the SQLite built-in."""
    raw = (extra or {}).get("databases")
    if not isinstance(raw, list):
        return []
    admin = (extra or {}).get("pg_admin")
    out = []
    for item in raw:
        if isinstance(item, dict):
            entry = resolve_admin_login(normalize_database_entry(item), admin)
            if entry["id"] and not entry["deleted"]:
                out.append(entry)
    return out


def builtin_databases():
    """The always-present built-in database entries: only oTree's own SQLite
    (1.5.0: the "Lab shared database" is a normal database of this PC now)."""
    sqlite = {"id": DB_BUILTIN_SQLITE, "title": DB_BUILTIN_SQLITE_TITLE,
              "researcher": "", "postgres_user": "", "database_url": "",
              "created": "", "builtin": True, "db_mode": DB_MODE_NONE,
              "deleted": False, "created_on": {}}
    for key in DATABASE_CONN_KEYS:
        sqlite[key] = ""
    return [sqlite]


def list_databases(extra):
    """The full database picker list: the SQLite built-in, then this PC's
    databases (the default one carries ``is_default`` True)."""
    default = default_database_id(extra)
    out = builtin_databases()
    for entry in known_databases_from_store(extra):
        entry["is_default"] = (entry["id"] == default)
        out.append(entry)
    return out


def find_database(extra, db_id):
    """A database entry by id (the SQLite built-in or one of this PC's), or None."""
    db_id = str(db_id or "").strip()
    if not db_id:
        return None
    for entry in list_databases(extra):
        if entry["id"] == db_id:
            return entry
    return None


def find_database_by_connection(conn, databases=None):
    """The database (of ``databases``, or the live registry) whose name, host,
    port and user match ``conn``, or None. Passwords are not compared."""
    def key(d):
        return (str(d.get("db_name", "") or "").strip(),
                (str(d.get("db_host", "") or "").strip() or "localhost").lower(),
                str(d.get("db_port", "") or "").strip() or "5432",
                str(d.get("db_user", "") or "").strip())
    wanted = key(conn or {})
    pool = databases if databases is not None else list(_DB_REGISTRY.values())
    for entry in pool:
        if isinstance(entry, dict) and not entry.get("deleted") and key(entry) == wanted:
            return entry
    return None


def database_config_fields(entry):
    """The config field overrides that selecting this database applies: its
    ``database_id`` (the reference that is saved) plus the derived ``db_mode``
    and connection. The SQLite built-in only sets db_mode "none"."""
    entry = entry or {}
    if entry.get("db_mode") == DB_MODE_NONE or entry.get("id") == DB_BUILTIN_SQLITE:
        return {"db_mode": DB_MODE_NONE, "database_id": DB_BUILTIN_SQLITE}
    fields = {"db_mode": DB_MODE_CUSTOM, "database_id": str(entry.get("id", "") or "")}
    for key in DATABASE_CONN_KEYS:
        fields[key] = str(entry.get(key, "") or "")
    return fields


def config_database_login_problem(cfg):
    """:data:`ADMIN_LOGIN_MISSING` when the database this config uses connects
    as the Postgres admin login and this PC has none stored, else ""."""
    entry = _DB_REGISTRY.get(current_database_id(cfg))
    if entry is not None and entry.get("login_missing"):
        return ADMIN_LOGIN_MISSING
    return ""


def current_database_id(cfg):
    """The id of the database a config uses right now (for a picker to tick):
    its own id, the PC default for a config that follows it, the SQLite id, or
    "" when nothing on this PC matches."""
    c = normalize_config(cfg)
    if c["db_mode"] == DB_MODE_NONE and c["database_id"] in ("", DB_BUILTIN_SQLITE):
        return DB_BUILTIN_SQLITE
    if c["database_id"] and c["database_id"] in _DB_REGISTRY:
        return c["database_id"]
    if c["database_id"]:
        return _DEFAULT_DB_ID if _DEFAULT_DB_ID in _DB_REGISTRY else DB_BUILTIN_SQLITE
    if c["db_mode"] == DB_MODE_LAB:
        return _DEFAULT_DB_ID
    match = find_database_by_connection(c)
    return match["id"] if match else ""


def config_database_note(cfg):
    """"" normally; a plain sentence when the config's database is not on this
    PC and the PC default (or SQLite) is used instead. Shown by both faces and
    logged by the headless run."""
    db_id = str((cfg or {}).get("database_id", "") or "").strip()
    if not db_id or db_id == DB_BUILTIN_SQLITE or db_id in _DB_REGISTRY:
        return ""
    default = _DB_REGISTRY.get(_DEFAULT_DB_ID)
    if default is not None:
        return ("This config's database (%s) is not set up on this computer, so it "
                "uses this computer's default database, %s." % (db_id, default["title"]))
    return ("This config's database (%s) is not set up on this computer, and this "
            "computer has no default database, so it uses oTree's own SQLite."
            % db_id)


def database_created_on_line(entry):
    """Where and when a database was created (or registered), e.g. "Created on
    LAB-PC-7 (10.0.0.7), Large lab, 2026-09-23". "" when unknown. Shown ONLY in
    the database's Edit view (1.5.1): lists, the Database card and the launch
    briefing show just :func:`database_host_note` (blank for localhost) and
    :func:`database_location_warning`."""
    stamp = normalize_created_on((entry or {}).get("created_on"))
    if not stamp:
        return ""
    where = stamp.get("hostname") or "an unknown computer"
    if stamp.get("ip"):
        where += " (%s)" % stamp["ip"]
    parts = [("Recorded on %s" if stamp.get("migrated") else "Created on %s") % where]
    if stamp.get("home_lab"):
        lab = find_lab_preset(stamp["home_lab"], default_lab_presets())
        parts.append(lab["name"] if lab else stamp["home_lab"])
    if stamp.get("when"):
        parts.append(stamp["when"][:10])
    return ", ".join(parts)


def database_location_warning(entry, hostname=None):
    """A warning when a LOCALHOST database was created on a different computer
    than this one: "localhost" here is a different Postgres, so a database of the
    same name here is not the same data. "" otherwise."""
    entry = entry or {}
    if entry.get("db_mode") == DB_MODE_NONE or entry.get("id") == DB_BUILTIN_SQLITE:
        return ""
    if not is_local_host(entry.get("db_host", "")):
        return ""
    stamp = normalize_created_on(entry.get("created_on"))
    created_host = stamp.get("hostname", "")
    if hostname is None:
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = ""
    if not created_host or not hostname or created_host.lower() == str(hostname).lower():
        return ""
    return ("%s was created on %s. On this computer (%s), localhost is a different "
            "Postgres: a database with the same name here is NOT the same data."
            % (entry.get("title") or entry.get("db_name") or "This database",
               created_host, hostname))


def database_location_note(entry, hostname=None):
    """The SHORT form of :func:`database_location_warning` for lists, the
    Database card and the picker: "Created on LAB-PC-7, not this computer". ""
    whenever the full warning is "". The full sentence goes behind the info tip
    next to it and stays in the database's Edit view."""
    if not database_location_warning(entry, hostname):
        return ""
    created_host = normalize_created_on((entry or {}).get("created_on")).get("hostname", "")
    return "Created on %s, not this computer" % created_host


def register_database(extra, title, researcher, connection=None,
                      postgres_user="", database_url="", created=None, created_on=None):
    """Append a database to this PC's list (append-only), stamp where it was
    created (``created_on``), record its creator in the researcher roster, and
    make it the PC default when the PC has none yet.

    Mutates ``extra`` in place and returns the new, normalized entry.
    ``connection`` is a dict of the ``db_*`` connection keys, so a
    ``create_database`` result's ``fields`` dict can be passed straight in."""
    if extra is None:
        raise ValueError("register_database needs a store `extra` dict to write into")
    connection = dict(connection or {})
    raw = {
        "title": str(title or "").strip(),
        "researcher": str(researcher or "").strip(),
        "postgres_user": str(postgres_user or connection.get("db_user", "")).strip(),
        "database_url": str(database_url or ""),
        "created": created or now_iso(),
        "created_on": created_on or current_pc_stamp(),
    }
    for key in DATABASE_CONN_KEYS:
        raw[key] = str(connection.get(key, "") or "")
    # A database with no user of its own references the admin login (stored
    # without a copy of it: normalize_database_entry blanks user + password).
    raw["uses_admin_login"] = bool(connection.get("uses_admin_login", False))
    stored = extra.get("databases")
    if not isinstance(stored, list):
        stored = []
    ids = [str(d.get("id", "")) for d in stored if isinstance(d, dict)]
    raw["id"] = _unique_db_id(raw["title"] or raw["db_name"], ids)
    entry = normalize_database_entry(raw)
    stored.append(entry)
    extra["databases"] = stored
    if entry["researcher"]:
        add_researcher(extra, entry["researcher"])
    if find_database(extra, extra.get(DEFAULT_DATABASE_KEY)) is None:
        extra[DEFAULT_DATABASE_KEY] = entry["id"]
    apply_default_database(extra)
    return resolve_admin_login(entry, extra.get("pg_admin"))


def edit_database(extra, db_id, title=None, researcher=None, db_name=None,
                  db_user=None, db_password=None, db_host=None, db_port=None,
                  postgres_user=None):
    """Edit one of this PC's databases in place (by id; its id, and so every
    config referencing it, is kept). Only the fields passed (non-None) change.
    The SQLite built-in has nothing to edit and the retired "lab shared" id no
    longer exists -> ``ValueError``. ``postgres_user`` is an alias for
    ``db_user``. A ``db_user`` of "" means "use this PC's Postgres admin login"
    (``uses_admin_login``, stored without a copy of it); a non-blank one gives
    the database its own user. Mutates ``extra`` in place and returns the
    updated entry (login resolved)."""
    if extra is None:
        raise ValueError("edit_database needs a store `extra` dict to write into")
    db_id = str(db_id or "").strip()
    if db_id == DB_BUILTIN_SQLITE:
        raise ValueError("The oTree default (SQLite) database has nothing to edit.")
    conn_updates = {"db_name": db_name, "db_user": db_user,
                    "db_password": db_password, "db_host": db_host,
                    "db_port": db_port}
    if postgres_user is not None and db_user is None:
        conn_updates["db_user"] = postgres_user
    stored = extra.get("databases")
    if not isinstance(stored, list):
        stored = []
    target_index = None
    for index, item in enumerate(stored):
        if isinstance(item, dict) and str(item.get("id", "")).strip() == db_id:
            target_index = index
            break
    if target_index is None:
        raise ValueError("No database with id %r to edit." % (db_id,))
    entry = normalize_database_entry(stored[target_index])
    _refuse_new_remote_host(entry.get("db_host", ""), db_host)
    if title is not None:
        entry["title"] = str(title).strip() or entry["title"]
    if researcher is not None:
        entry["researcher"] = str(researcher).strip()
    if postgres_user is not None:
        entry["postgres_user"] = str(postgres_user).strip()
    for key, value in conn_updates.items():
        if value is not None:
            entry[key] = str(value)
    if db_user is not None and postgres_user is None:
        entry["postgres_user"] = str(db_user).strip()
    if conn_updates["db_user"] is not None:
        entry["uses_admin_login"] = not str(conn_updates["db_user"]).strip()
    entry = normalize_database_entry(entry)
    stored[target_index] = entry
    extra["databases"] = stored
    if researcher is not None and entry["researcher"]:
        add_researcher(extra, entry["researcher"])
    apply_default_database(extra)
    return resolve_admin_login(entry, extra.get("pg_admin"))


def _refuse_new_remote_host(current, new):
    """ValueError when an edit moves a database to a DIFFERENT non-local host
    while localhost-only is on. Keeping an existing remote host is allowed, so
    older entries stay editable."""
    if new is None:
        return
    if str(new).strip().lower() == str(current or "").strip().lower():
        return
    refusal = database_host_refusal(new)
    if refusal:
        raise ValueError(refusal)


def soft_delete_database(extra, db_id):
    """Soft-delete one of this PC's databases: hide it from the app but KEEP it in
    machine.json (recoverable by hand-editing). It does NOT touch Postgres. The
    SQLite built-in cannot be deleted. If it was the PC default, the next
    remaining database becomes the default (or none). Mutates ``extra`` in place
    and returns (ok, message)."""
    if extra is None:
        raise ValueError("soft_delete_database needs a store `extra` dict to write into")
    db_id = str(db_id or "").strip()
    if db_id in (DB_BUILTIN_SQLITE, DB_BUILTIN_LAB):
        return False, "A built-in database cannot be deleted."
    stored = extra.get("databases")
    if not isinstance(stored, list):
        stored = []
    for item in stored:
        if isinstance(item, dict) and str(item.get("id", "")).strip() == db_id:
            if item.get("deleted"):
                return False, "That database is already deleted."
            title = str(item.get("title", "") or item.get("db_name", "") or db_id)
            item["deleted"] = True
            extra["databases"] = stored
            if str(extra.get(DEFAULT_DATABASE_KEY, "") or "") == db_id:
                remaining = known_databases_from_store(extra)
                extra[DEFAULT_DATABASE_KEY] = remaining[0]["id"] if remaining else ""
            apply_default_database(extra)
            return True, "Hid the database %r." % title
    return False, "No database with that id."


# -- This PC's DEFAULT database (a reference into the one list) --------------
#
# The default is simply WHICH of this PC's databases a config that follows the
# PC default (the generated Lab default) uses, stored as its id in machine.json
# ``default_database``. "" / an id that no longer resolves = no default: such a
# config falls back to oTree's own SQLite, and saying so is the caller's job.


def default_database_id(extra):
    """The id of this PC's default database, or "" when it has none."""
    value = str((extra or {}).get(DEFAULT_DATABASE_KEY, "") or "").strip()
    if value and any(e["id"] == value for e in known_databases_from_store(extra)):
        return value
    return ""


def resolve_default_database(extra):
    """This PC's default database entry, or None."""
    db_id = default_database_id(extra)
    return find_database(extra, db_id) if db_id else None


def lab_shared_db_fields(extra):
    """The connection of this PC's default database (placeholders when none)."""
    entry = resolve_default_database(extra)
    if entry is None:
        return dict(_DUMMY_DB)
    return {key: str(entry.get(key, "") or "") for key in DATABASE_CONN_KEYS}


def apply_default_database(extra):
    """Publish ``extra``'s database list + default as the live registry that
    normalize_config resolves against (and LAB_DB = the default's connection).
    Both faces call this after loading the store and after every database
    change. Returns the default's connection dict."""
    return _publish_databases((extra or {}).get("databases"),
                              default_database_id(extra),
                              (extra or {}).get("pg_admin"))


def default_database_options(extra):
    """The databases selectable as this PC's default: all of its databases
    (never the SQLite built-in)."""
    return [entry for entry in list_databases(extra)
            if entry.get("db_mode") != DB_MODE_NONE]


def set_default_database(extra, db_id):
    """Make one of this PC's databases the default, by reference. Raises
    ``ValueError`` for an unknown id or the SQLite built-in. Mutates ``extra`` in
    place, republishes the live registry and returns the chosen entry."""
    if extra is None:
        raise ValueError("set_default_database needs a store `extra` dict to write into")
    entry = find_database(extra, db_id)
    if entry is None:
        raise ValueError("No database with id %r to make the default." % (db_id,))
    if entry.get("db_mode") == DB_MODE_NONE:
        raise ValueError(
            "The default database must be a Postgres database, not the oTree "
            "default (SQLite).")
    extra[DEFAULT_DATABASE_KEY] = entry["id"]
    apply_default_database(extra)
    return entry


# -- Researcher roster ------------------------------------------------------

def _dedupe_names(names):
    """Case-insensitive dedupe keeping first-seen casing, sorted case-insensitively."""
    seen = {}
    for name in names:
        name = str(name or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key not in seen:
            seen[key] = name
    return sorted(seen.values(), key=lambda s: s.casefold())


def researchers_from_store(extra):
    """Only the researcher names saved explicitly in the store (not derived)."""
    raw = (extra or {}).get("researchers")
    if not isinstance(raw, list):
        return []
    return _dedupe_names(raw)


def list_researchers(extra, presets=None):
    """The shared researcher roster, deduped case-insensitively and sorted.

    Combines three sources so one roster feeds the Save-as-new config author
    field AND the create-database Researcher field: the names saved explicitly
    in the store, every config ``author`` (pass ``presets`` to include them,
    the built-in "builtin" author is skipped), and every database creator.
    """
    names = list(researchers_from_store(extra))
    for preset in (presets or []):
        author = str((preset or {}).get("author", "")).strip()
        if author and author.casefold() != "builtin":
            names.append(author)
    for entry in known_databases_from_store(extra):
        if entry["researcher"]:
            names.append(entry["researcher"])
    return _dedupe_names(names)


def add_researcher(extra, name):
    """Add one researcher name to the saved roster (append-only, case-insensitive
    dedupe). Mutates ``extra`` in place and returns the saved-names list."""
    if extra is None:
        raise ValueError("add_researcher needs a store `extra` dict to write into")
    name = str(name or "").strip()
    stored = extra.get("researchers")
    if not isinstance(stored, list):
        stored = []
    if name and not any(str(n).strip().casefold() == name.casefold() for n in stored):
        stored.append(name)
    extra["researchers"] = stored
    return researchers_from_store(extra)


# ---------------------------------------------------------------------------
# Opening the dashboard (Round 3), a SHARED helper for both launchers. A
# pre-authenticated dashboard URL embeds the admin credentials
# (http://user:pass@host/...). Chrome, Edge, Chromium, Brave and Firefox honor
# an embedded userinfo; Safari STRIPS it, so for a credentialed URL we prefer a
# known-good browser and only fall back to the system default browser
# (webbrowser.open) when none is found. A plain URL always uses the default
# browser. The caller keeps showing the admin username + password so a fallback
# to Safari (or any browser that ignores the creds) still lets the user type
# them.
# ---------------------------------------------------------------------------

# Preference order for a credentialed URL. Each entry carries the macOS .app
# name, the Windows install sub-paths and the executable names to look up on
# PATH (Linux and a Windows PATH fallback).
_PREFERRED_BROWSERS = (
    {"name": "Chrome", "mac_app": "Google Chrome",
     "win": (r"Google\Chrome\Application\chrome.exe",),
     "exe": ("google-chrome", "google-chrome-stable", "chrome")},
    {"name": "Edge", "mac_app": "Microsoft Edge",
     "win": (r"Microsoft\Edge\Application\msedge.exe",),
     "exe": ("microsoft-edge", "microsoft-edge-stable", "msedge")},
    {"name": "Chromium", "mac_app": "Chromium",
     "win": (r"Chromium\Application\chrome.exe",),
     "exe": ("chromium", "chromium-browser")},
    {"name": "Brave", "mac_app": "Brave Browser",
     "win": (r"BraveSoftware\Brave-Browser\Application\brave.exe",),
     "exe": ("brave-browser", "brave")},
    {"name": "Firefox", "mac_app": "Firefox",
     "win": (r"Mozilla Firefox\firefox.exe",),
     "exe": ("firefox",)},
)


def url_has_credentials(url):
    """True when the URL embeds a userinfo component (``scheme://user:pass@host``)."""
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/@\s]+@", str(url or "")))


def _browser_opener_for(spec):
    """An opener callable ``open(url)`` for this browser on the current platform,
    or None when it is not installed. Kept small so tests can monkeypatch the
    platform lookups."""
    plat = sys.platform
    if plat == "darwin":
        app = spec["mac_app"]
        for base in ("/Applications", os.path.expanduser("~/Applications")):
            if os.path.isdir(os.path.join(base, app + ".app")):
                return lambda url, a=app: subprocess.Popen(["open", "-a", a, url])
        return None
    if plat.startswith("win"):
        roots = [os.environ.get(var, "") for var in
                 ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        for root in roots:
            if not root:
                continue
            for sub in spec.get("win", ()):
                exe = os.path.join(root, sub)
                if os.path.isfile(exe):
                    return lambda url, e=exe: subprocess.Popen([e, url])
        for name in spec.get("exe", ()):
            found = shutil.which(name) or shutil.which(name + ".exe")
            if found:
                return lambda url, e=found: subprocess.Popen([e, url])
        return None
    for name in spec.get("exe", ()):
        found = shutil.which(name)
        if found:
            return lambda url, e=found: subprocess.Popen([e, url])
    return None


def find_preferred_browser():
    """The first installed credential-honoring browser as ``(name, opener)``, or
    None. Preference order: Chrome, Edge, Chromium, Brave, Firefox. Safari is
    never a candidate because it strips embedded URL credentials."""
    for spec in _PREFERRED_BROWSERS:
        opener = _browser_opener_for(spec)
        if opener is not None:
            return spec["name"], opener
    return None


def open_dashboard(url):
    """Open the dashboard URL, preferring a credential-honoring browser when the
    URL is pre-authenticated.

    Returns a result dict::

        {"ok": bool,            # something was opened
         "browser": str,        # "Chrome"/"Edge"/... or "default"
         "used_preferred": bool,# a preferred browser was used (not the default)
         "credentialed": bool}  # the URL embedded user:pass@

    For a plain URL (no embedded credentials) the system default browser is used
    directly. For a credentialed URL a preferred browser is tried first and the
    default browser is the fallback. Never raises: a failure returns ok False.
    """
    url = str(url or "")
    result = {"ok": False, "browser": "", "used_preferred": False,
              "credentialed": url_has_credentials(url)}
    if not url:
        return result
    if result["credentialed"]:
        found = find_preferred_browser()
        if found is not None:
            name, opener = found
            try:
                opener(url)
                result.update(ok=True, browser=name, used_preferred=True)
                return result
            except Exception:
                pass   # fall through to the system default browser
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    result.update(ok=bool(opened), browser="default", used_preferred=False)
    return result


# ---------------------------------------------------------------------------
# Real auto-login (Option A from _ai/AUTOLOGIN_INVESTIGATION.md).
#
# oTree ignores HTTP Basic Auth, so the `http://user:pass@host/` URL trick is
# DEAD: it 302-redirects to /login (proven with curl against otree==6.0.15). The
# ONLY way to reach the admin room monitor without a manual login is a valid
# oTree *session cookie* obtained by a real form-login. So we:
#   1. form-login with urllib against the running server (GET /login for the
#      csrftoken + session cookie, POST username/password/csrftoken back with the
#      same jar), capturing the raw `session=...` cookie the server hands back on
#      the 302 -> /demo success;
#   2. stand up a one-shot localhost HTTP responder that answers the browser's
#      first GET with `302 Location: <monitor>` AND
#      `Set-Cookie: session=<value>`, planting the logged-in cookie (it is
#      HttpOnly, so JavaScript cannot set it) and, because oTree's cookie is
#      host-scoped and NOT isolated by port, it rides along to the server;
#   3. open that relay URL in the SYSTEM DEFAULT BROWSER.
# On any failure (wrong password, AUTH_LEVEL unset, server still booting, relay
# error) we fall back to opening the plain monitor URL and the operator logs in
# once (Option D). This is the ONLY dashboard-open path the apps use now; the
# prefer-Chrome open_dashboard/credentialed_url path above is retired (kept only
# so its unit tests stay green).
# ---------------------------------------------------------------------------

# The oTree session cookie is host-scoped (no Domain) and not isolated by port,
# so a cookie set from 127.0.0.1:<relay port> is sent to 127.0.0.1:<server port>.
# Pick ONE host string and use it for the form-login target, the Set-Cookie host
# (the relay URL the browser opens) and the redirect target, so the cookie scope
# lines up. The auto-open is on the operator's OWN machine, so a loopback host is
# right; the participant per-seat links keep the configured lab host, unchanged.
#
# It is the LITERAL IPv4 loopback 127.0.0.1, NOT the name "localhost", on purpose:
# the one-shot cookie relay binds 127.0.0.1 only (IPv4), but on many hosts/browsers
# "localhost" resolves to the IPv6 ::1 first, so a browser sent to
# http://localhost:<relay port>/ would try ::1, find nothing listening, and report
# "cannot connect to localhost:<port>" -- the intermittent auto-login failure. Using
# 127.0.0.1 for BOTH the relay URL AND the monitor URL keeps the browser on the same
# family the relay listens on, and (same host) the planted session cookie still rides
# from the relay to the monitor. See _ai/web_wizard_build.md (Pass 2, Job 2).
AUTOLOGIN_HOST = "127.0.0.1"

_LOGIN_PATH = "/login"
_DEMO_PATH = "/demo"
# The hidden CSRF field oTree embeds in the /login form (see the investigation).
_CSRF_INPUT_RE = re.compile(
    r'name=["\']csrftoken["\']\s+value=["\']([^"\']+)["\']')


def otree_form_login(host, port, username, password, timeout=4.0,
                     attempts=6, backoff=0.5):
    """Real oTree admin form-login; return the raw ``session`` cookie value that
    authenticates the monitor, or ``None`` on any failure.

    Sequence (verified against otree==6.0.15, see _ai/AUTOLOGIN_INVESTIGATION.md):
    GET /login with a cookie jar (the server sets a `session` cookie carrying a
    csrftoken and embeds the SAME token in a hidden field), scrape that hidden
    csrftoken, POST username/password/csrftoken back (urlencoded) with the same
    jar. On success oTree 302-redirects to /demo and the jar now holds the
    logged-in `session` cookie; on a wrong password it re-renders /login (200) and
    no auth cookie is set.

    Connection errors are retried a few times with a short backoff, because the
    launch flow opens the dashboard ~2s after starting prodserver and the server
    may still be booting. A wrong password (a real, answered rejection) is NOT
    retried. Never raises: returns None on anything unexpected.
    """
    import urllib.request
    import urllib.parse
    import urllib.error
    import http.cookiejar

    base = "http://%s:%s" % (host, port)
    for _attempt in range(max(1, int(attempts))):
        try:
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar))
            with opener.open(base + _LOGIN_PATH, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", "replace")
            match = _CSRF_INPUT_RE.search(html)
            if match is None:
                # No login form at all (e.g. AUTH_LEVEL unset): nothing to log in
                # to. Let the caller just open the monitor directly.
                return None
            data = urllib.parse.urlencode({
                "username": username or "",
                "password": password or "",
                "csrftoken": match.group(1),
            }).encode("ascii")
            request = urllib.request.Request(
                base + _LOGIN_PATH, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with opener.open(request, timeout=timeout) as resp:
                # urllib follows the 302 automatically; a success lands on /demo,
                # a wrong password stays on /login.
                final_path = urllib.parse.urlsplit(resp.geturl()).path
                resp.read()
            if _DEMO_PATH in final_path:
                for cookie in jar:
                    if cookie.name == "session":
                        return cookie.value
            # Reached the server and got a definite answer: bad credentials.
            return None
        except (urllib.error.URLError, OSError):
            # Server not up yet (or a transient network error): wait and retry.
            time.sleep(backoff)
    return None


def start_cookie_relay(cookie_value, monitor_url, host=AUTOLOGIN_HOST,
                       idle_timeout=30.0):
    """Start a one-shot localhost cookie-relay responder.

    The first GET it receives is answered with ``302 Location: monitor_url`` plus
    ``Set-Cookie: session=<cookie_value>; Path=/; SameSite=Lax``, planting the
    logged-in oTree session cookie (which is host-scoped and not port-isolated, so
    it then rides to the monitor's server) and immediately shutting the responder
    down. If the browser never arrives it self-destructs after ``idle_timeout``
    seconds so nothing lingers. Bound to the 127.0.0.1 loopback only, on a free
    OS-chosen port, in a background daemon thread.

    Returns ``(relay_url, server, serve_thread)``. Most callers only need
    ``relay_url``; a caller that must not let the process exit before the browser
    has been served (the headless one-click shortcut) can join ``serve_thread`` --
    it ends when the relay has answered its one request, or, at the latest, when
    the ``idle_timeout`` safety net shuts the server down -- so the wait is always
    bounded and can never hang forever.
    """
    import http.server

    class _Relay(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
            self.send_response(302)
            self.send_header("Location", monitor_url)
            self.send_header(
                "Set-Cookie",
                "session=%s; Path=/; SameSite=Lax" % cookie_value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            # shutdown() must run off the serving thread, so hand it to another.
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):  # silence the default stderr logging
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Relay)
    port = server.server_address[1]
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    # Safety net: tear the responder down if the browser never hits it. This also
    # bounds any join() on serve_thread, so a blocking caller can never hang.
    timer = threading.Timer(idle_timeout, server.shutdown)
    timer.daemon = True
    timer.start()
    relay_url = "http://%s:%s/" % (host, port)
    return relay_url, server, serve_thread


# How long to wait for prodserver to answer before reporting a launch as failed.
# A named constant (was a bare 8.0 in three places): 30 s is safer on an old lab
# PC booting a project with many apps, which could take longer than 8 s and be
# wrongly reported as "did not become ready" while the server then came up fine.
# The wait is still a POLL (we open the instant the server responds), so a fast
# server is unaffected -- only a slow start is given more room (fable review #8).
READINESS_TIMEOUT = 30.0


def wait_for_server(host, port, timeout=READINESS_TIMEOUT, interval=0.25, on_wait=None):
    """Poll the oTree server until it answers an HTTP request, or ``timeout``.

    Returns ``True`` as soon as a GET to ``/login`` (falling back to ``/``) gets
    any HTTP response at all -- a 200, a 302 redirect, even a 404 all mean the
    server socket is up and handling requests, which is exactly the readiness we
    need before opening the dashboard. Returns ``False`` if the deadline passes
    with no response. Stdlib only; never raises.

    ``on_wait`` (optional) is called with the whole seconds elapsed every few
    seconds while still waiting, so a caller can STREAM "still waiting (12 s)"
    lines to its activity log instead of the wait looking like a silent hang on a
    slow lab PC (fable review #8). Any exception it raises is swallowed.

    This replaces the old blind ``time.sleep`` both launchers did before opening
    the dashboard: instead of waiting a fixed guess, we open the instant the
    server responds.
    """
    import urllib.request
    import urllib.error

    base = "http://%s:%s" % (host, port)
    start = time.time()
    deadline = start + max(0.0, float(timeout))
    step = max(0.01, float(interval))
    next_notice = 3.0   # first "still waiting" line after ~3s, then every ~3s
    while True:
        for path in (_LOGIN_PATH, "/"):
            try:
                with urllib.request.urlopen(base + path, timeout=step * 4):
                    return True
            except urllib.error.HTTPError:
                # A real HTTP status came back (404/403/etc): the server is up.
                return True
            except (urllib.error.URLError, OSError):
                # Not listening yet (or a transient socket error): keep waiting.
                pass
        now = time.time()
        if now >= deadline:
            return False
        elapsed = now - start
        if on_wait is not None and elapsed >= next_notice:
            try:
                on_wait(int(elapsed))
            except Exception:
                pass
            next_notice += 3.0
        time.sleep(step)


def open_dashboard_authenticated(host, port, room, username, password,
                                 auto_login=True, open_url=None,
                                 login=None, start_relay=None,
                                 wait=None, ready_timeout=READINESS_TIMEOUT,
                                 block_relay=False, on_wait=None):
    """The single dashboard-open entry point both launchers call.

    Opens the admin room monitor for ``room`` in the SYSTEM DEFAULT BROWSER. When
    ``auto_login`` is on it first does a real oTree form-login and, on success,
    relays the resulting session cookie through a one-shot localhost redirect so
    the browser lands already authenticated (``method == "cookie"``). If
    auto-login is off, or the login fails for any reason, it opens the plain
    monitor URL and the operator logs in once (``method == "manual"``).

    The monitor host is always the loopback (``AUTOLOGIN_HOST`` = 127.0.0.1): the
    auto-open is on the operator's own machine, and the relay binds the same IPv4
    loopback so the cookie rides. The participant per-seat links keep the
    configured lab host and are unchanged by this function.

    Returns a dict::

        {"ok": bool,            # a page was opened (True even for manual)
         "method": "cookie"|"manual",
         "reason": str,         # plain-language what-happened, for the log/popup
         "monitor_url": str,    # the plain monitor URL (manual login lands here)
         "opened_url": str,     # the URL actually handed to the browser
         "server_ready": bool}  # the server answered the readiness poll

    ``ok`` is True whenever a page opened, because the manual fallback still lets
    the operator finish by hand. ``server_ready`` is the ACTUAL readiness result
    (from ``wait``/:func:`wait_for_server`): it is ``True`` only when the server
    answered within ``ready_timeout``. Callers must gate "Launched" success and
    the ``last_run`` stamp on ``server_ready`` -- a page can open (``ok`` True)
    onto a server that never came up (``server_ready`` False), which means the
    launch really failed and the terminal window holds the traceback. ``open_url``/``login``/``start_relay``/``wait``
    are injection points for tests; by default they are the real
    browser/login/relay and ``wait_for_server``. ``ready_timeout`` caps the
    readiness poll. Never raises.

    ``block_relay`` (default ``False`` -- the GUI/web behaviour is byte-for-byte
    unchanged) is for the headless one-click shortcut, whose process exits the
    instant this returns. When ``True`` and the cookie-relay path is taken, after
    opening the browser this waits for the relay to actually serve the browser's
    request (by joining the relay's serve thread) before returning, so the process
    stays alive long enough for the 302 + Set-Cookie to happen. The wait is
    bounded by the relay's ``idle_timeout`` safety net, so it can never hang -- if
    the browser never arrives the relay self-destructs and this returns anyway.

    Before doing anything else it polls the server for readiness (``wait``, the
    real :func:`wait_for_server`) so the dashboard opens the instant the server
    responds -- both the cookie path and the manual/auto-login-off path go
    through this, so neither launcher needs a blind pre-open ``time.sleep`` any
    more.
    """
    open_url = open_url or webbrowser.open
    login = login or otree_form_login
    start_relay = start_relay or start_cookie_relay
    wait = wait or wait_for_server

    # Readiness gate: wait (only) as long as the server actually needs to boot,
    # then continue immediately. Replaces the old fixed pre-open sleep. We now
    # CAPTURE the result (the old code ignored it) so the caller can tell a real
    # startup from a crash: a page still opens either way, but only a ready
    # server is a real launch.
    server_ready = False
    try:
        # Pass on_wait only when given, so an injected test ``wait`` double that
        # does not accept it keeps working (real wait_for_server accepts it and
        # streams "still waiting (N s)" lines to the caller's activity log).
        if on_wait is not None:
            server_ready = bool(wait(host, port, timeout=ready_timeout, on_wait=on_wait))
        else:
            server_ready = bool(wait(host, port, timeout=ready_timeout))
    except Exception:
        server_ready = False

    monitor_url = "http://%s:%s%s" % (host, port, room_monitor_path(room))
    result = {"ok": False, "method": "manual", "reason": "",
              "monitor_url": monitor_url, "opened_url": monitor_url,
              "server_ready": server_ready}

    def _open(url):
        try:
            opened = open_url(url)
        except Exception:
            return False
        # webbrowser.open returns a bool; a custom opener may return None on ok.
        return opened is None or bool(opened)

    if auto_login:
        cookie = None
        try:
            cookie = login(host, port, username, password)
        except Exception:
            cookie = None
        if cookie:
            try:
                relay = start_relay(cookie, monitor_url, host=host)
            except Exception as error:
                result["reason"] = ("auto-login worked but the cookie relay "
                                    "could not start (%s); opened the login page"
                                    % type(error).__name__)
            else:
                # start_cookie_relay now returns (url, server, serve_thread);
                # tolerate an old-style bare-URL return from an injected double.
                serve_thread = None
                if isinstance(relay, (tuple, list)):
                    relay_url = relay[0]
                    if len(relay) >= 3:
                        serve_thread = relay[2]
                else:
                    relay_url = relay
                if _open(relay_url):
                    if block_relay and serve_thread is not None:
                        # Keep the process alive until the relay has served the
                        # browser (or its idle_timeout self-destruct fires). The
                        # relay's safety-net timer bounds this join, so no hang.
                        try:
                            serve_thread.join()
                        except Exception:
                            pass
                    result.update(ok=True, method="cookie", opened_url=relay_url,
                                  reason="logged in automatically (form-login + "
                                         "cookie relay)")
                    return result
                result["reason"] = ("auto-login worked but the browser could not "
                                    "be opened; opened the login page")
        else:
            result["reason"] = ("auto-login could not log in (wrong password, or "
                                "the server was not ready); opened the login page")
    else:
        result["reason"] = "auto-login is off; opened the login page"

    result.update(ok=_open(monitor_url), method="manual", opened_url=monitor_url)
    return result


# ---------------------------------------------------------------------------
# Launch briefing (Feature 1 caution flag lives here), the whole host/room
# decision stays in Python; both launchers only render this dict.
# ---------------------------------------------------------------------------

CAUTION_TEXT = ("Caution: shared lab database. It may be reset between sessions. "
                "Download your data as soon as the experiment finishes.")

# oTree 6 shows a Welcome/Start page on a bare room seat link, which needs a
# click before the participant is admitted. Appending this flag makes oTree
# admit the seat straight into the experiment with zero clicks (verified
# empirically), so it is part of EVERY per-seat link the launcher builds, shows
# or documents. See WELCOME_NOTE for the wording shown to lab staff.
WELCOME_FLAG = "welcome_page_ok=1"

# What the launch briefing tells staff about the per-seat link. Kept in one place
# so the Tk popup, the web popup and the docs all say the same thing.
LOCAL_ONLY_TEXT = "Runs on this computer only."
WELCOME_NOTE = ("This exact link (with welcome_page_ok=1) is THE link to put in "
                "all lab documentation and on the lab computers. welcome_page_ok=1 "
                "skips oTree 6's Welcome/Start page, so each seat auto-admits with "
                "no click. Create the room session first: until it exists the link "
                "shows a wait page that advances on its own the moment the session "
                "opens (no re-click needed).")


# Rooms whose per-seat participant links the lab PCs' desktop shortcuts open
# (http://HOST:PORT/room/ROOM?participant_label=SEAT&welcome_page_ok=1). The lab-shortcut room is
# per-lab now (a lab's ``default_room``); this tuple is only the fallback used
# when no lab room is supplied. See _ai/ROOM_PARTICIPANT_LINKS_NOTE.md.
PARTICIPANT_LINK_ROOMS = (DEFAULT_ROOM_NAME,)


def room_has_participant_links(name, lab_room=None):
    """True when the lab PC desktop shortcuts already open this room's per-seat
    links (so participants can join straight from the lab computers).

    ``lab_room`` is the lab's own default room; when given, the shortcuts open
    exactly that room. Without it we fall back to the built-in default room, so
    older callers keep their behaviour.
    """
    if lab_room:
        return str(name).strip() == sanitize_room_name(lab_room)
    return str(name).strip() in PARTICIPANT_LINK_ROOMS


def study_shortcut_name(lab_label):
    """The lab-PC desktop-shortcut name for the study room.

    The one source of truth for the convention "Study Room <Lab name>" with a
    title-cased lab name: "Study Room Large Lab", "Study Room Small Lab",
    "Study Room Rotterdam Annex". A lab shortcut encodes BOTH the room and which
    server (large vs small), because a small-lab machine can reach the large-lab
    server, so which server matters.
    """
    label = str(lab_label or "").strip()
    return ("Study Room " + label.title()) if label else "Study Room"


def launch_briefing(cfg, lab_presets=None):
    """What to tell the experimenter BEFORE the server starts.

    A pure description; it starts nothing. It names the chosen lab and host and
    the exact per-seat link the lab computers open, warns when the chosen room
    is not the ``study`` room the desktop shortcuts point at, and raises the
    ``caution`` flag when the run is on the shared lab database
    (Feature 1). The UI shows the caution bar only when the flag is set.
    """
    c = normalize_config(cfg)
    host = resolve_host(c, lab_presets)
    lab = c["lab"]
    resolved_presets = _presets_or_default(lab_presets)
    preset = find_lab_preset(lab, resolved_presets)
    if lab == LAB_LOCAL:
        lab_label = LOCAL_LAB_LABEL
    elif lab == LAB_CUSTOM:
        lab_label = "Custom host"
    elif preset is not None:
        lab_label = preset.get("name") or lab
    else:
        lab_label = lab or "Custom host"

    # This lab's own default room + the human name of its participant-PC desktop
    # shortcuts (both blank-safe): the briefing names the right room and shortcut
    # automatically instead of hardcoding "study".
    lab_room = lab_default_room(resolved_presets, lab)
    shortcut_label = lab_shortcut_label(resolved_presets, lab)

    port = (c["port"] or "8000").strip()
    room = c["room_name"].strip() or DEFAULT_ROOM_NAME
    is_study = (room == lab_room)
    # The mode a launch will REALLY use: no seats (absent/empty file, no default
    # seats) falls back to the open room, so the briefing shows the open-room
    # summary rather than a phantom seat board.
    seat_mode = effective_seat_mode(c, lab_presets)
    seats = resolve_seats(c, lab_presets)
    open_room = (seat_mode == SEAT_NONE)

    if open_room:
        example_seat = ""
    elif seat_mode == SEAT_FILE:
        example_seat = "SEAT"      # labels come from the researcher's own file
    else:
        example_seat = seats[0] if seats else "SEAT"

    def link(seat):
        base = "http://%s:%s/room/%s" % (host, port, room)
        # Every per-seat link carries welcome_page_ok=1 so oTree 6 admits the
        # seat with no Welcome-page click. The open-room link has no seat and no
        # such flag.
        if not seat:
            return base
        return base + "?participant_label=%s&%s" % (seat, WELCOME_FLAG)

    # The caution bar: this run uses this PC's DEFAULT database (the shared one
    # that may be reset between sessions), followed or picked explicitly.
    caution = (c["db_mode"] == DB_MODE_LAB
               or (c["db_mode"] == DB_MODE_CUSTOM and bool(_DEFAULT_DB_ID)
                   and current_database_id(c) == _DEFAULT_DB_ID))
    # A room with participant PC links has a lab desktop shortcut (which encodes
    # both the room and which server); the briefing names that shortcut instead
    # of a raw link. A room without one has no shortcut, so the briefing gives
    # the manual per-seat link to open on each computer. The shortcut room is the
    # lab's OWN default_room now, not a hardcoded "study".
    has_participant_links = room_has_participant_links(room, lab_room)
    # Only a real lab has desktop shortcuts on its PCs. "Other host" has none, so
    # the briefing shows the link; "localhost" has no participant computers at
    # all, so it says so instead of naming a shortcut that does not exist.
    local_only = (lab == LAB_LOCAL)
    if lab in (LAB_CUSTOM, LAB_LOCAL):
        has_participant_links = False
    return {
        "lab_label": lab_label,
        "host": host,
        "port": port,
        "room": room,
        "is_study": is_study,
        "default_room": lab_room,
        "open_room": open_room,
        "seat_mode": seat_mode,
        "seat_count": len(seats),
        "example_seat": example_seat,
        "example_link": link(example_seat),
        "link_template": link("SEAT"),
        "has_participant_links": has_participant_links,
        "local_only": local_only,
        "local_only_text": LOCAL_ONLY_TEXT if local_only else "",
        "shortcut_name": study_shortcut_name(lab_label),
        # The human name of THIS lab's participant-PC desktop shortcuts (blank
        # when the lab did not set one). When set, the briefing tells the user to
        # open the "<label>" shortcut on each participant PC.
        "shortcut_label": shortcut_label,
        "caution": caution,
        "caution_text": CAUTION_TEXT if caution else "",
        # The database this run uses, and "on HOST:PORT" when that is another
        # computer (blank for localhost / SQLite), shown in grey.
        "db_label": database_summary_label(c),
        "db_host_note": database_host_note(c),
        # Guidance for staff about the per-seat link: it already includes
        # welcome_page_ok=1, and this is the link to document / put on the PCs.
        "welcome_note": WELCOME_NOTE,
    }


# ---------------------------------------------------------------------------
# Participant-PC kiosk shortcuts (Windows .lnk bundle)
#
# A folder of one Windows .lnk per seat, each opening THAT seat's welcome-page
# link in a full-screen kiosk browser on the participant PC. The .lnk bytes are
# written DIRECTLY here from the [MS-SHLLINK] Shell Link binary format, using only
# the standard library (struct), so a lab manager on ANY OS (Mac / Linux /
# Windows) produces a real Windows shortcut -- all participant PCs are Windows.
# We deliberately do NOT use WScript.Shell / pywin32 (Windows-only, and absent on
# the lab manager's Mac). Windows resolves the stored target path at CLICK time on
# the participant PC, so the browser exe need not exist on the generating machine.
#
# The exe path and the kiosk flags are easy-to-change module constants: Julian
# confirms the exact link/exe on a real lab PC later; the mechanism is what
# matters now. Default is Edge in kiosk full-screen; Chrome would be
# ``chrome.exe --kiosk "<url>"`` (no --edge-kiosk-type flag).
# ---------------------------------------------------------------------------

# The fixed Windows path to the browser the participant-PC shortcuts launch. The
# .lnk stores this verbatim; the participant PC resolves it at click time.
PARTICIPANT_BROWSER_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# The kiosk flags, split around the seat URL so the whole command line reads
# exactly like the one Julian sets by hand in the shortcut's Properties -> Target:
#   ...\msedge.exe --kiosk "http://HOST:PORT/room/ROOM?participant_label=SEAT&welcome_page_ok=1" --edge-kiosk-type=fullscreen
PARTICIPANT_KIOSK_FLAG = "--kiosk"            # goes BEFORE the URL
PARTICIPANT_KIOSK_EXTRA_FLAGS = "--edge-kiosk-type=fullscreen"   # goes AFTER the URL

# The 16-byte Shell Link CLSID {00021401-0000-0000-C000-000000000046}, in the
# little-endian-mixed on-disk layout [MS-SHLLINK] 2.1 requires. This plus the
# 0x0000004C HeaderSize is the "magic header" every valid .lnk begins with.
SHELL_LINK_CLSID = (b"\x01\x14\x02\x00\x00\x00\x00\x00"
                    b"\xc0\x00\x00\x00\x00\x00\x00\x46")

# Shell Link header LinkFlags bits ([MS-SHLLINK] 2.1.1) we use.
_LNK_HAS_LINK_INFO = 0x00000002
_LNK_HAS_NAME = 0x00000004
_LNK_HAS_WORKING_DIR = 0x00000010
_LNK_HAS_ARGUMENTS = 0x00000020
_LNK_HAS_ICON_LOCATION = 0x00000040
_LNK_IS_UNICODE = 0x00000080
_LNK_FILE_ATTRIBUTE_ARCHIVE = 0x00000020
_LNK_SW_SHOWNORMAL = 1
_LNK_DRIVE_FIXED = 3


def _lnk_string_data(text):
    """One Unicode StringData block ([MS-SHLLINK] 2.4): a 2-byte character count
    (UTF-16 code units, no terminator) followed by the UTF-16LE bytes. Used with
    the IsUnicode header flag set."""
    data = str(text).encode("utf-16-le")
    return struct.pack("<H", len(data) // 2) + data


def _lnk_link_info(local_base_path):
    """A LinkInfo structure ([MS-SHLLINK] 2.3) carrying the target's LocalBasePath.

    Windows uses this ANSI path to find the target when there is no
    LinkTargetIDList (which we omit -- an IDList would encode the generating
    machine's own shell namespace, wrong for a shortcut resolved on a different
    Windows PC). A minimal fixed-drive VolumeID accompanies the path, as the
    format requires.
    """
    # ANSI (non-Unicode) path. The default Edge path is ASCII; latin-1 keeps any
    # byte 1:1 rather than raising, and the participant PC reads it back as its
    # own ANSI code page.
    base = local_base_path.encode("latin-1", "replace") + b"\x00"
    header_size = 0x1C                      # 7 x uint32, no Unicode-offset fields
    volume_label = b"\x00"                  # empty volume label, null-terminated
    volume_label_offset = 0x10
    volume_id_size = 4 + 4 + 4 + 4 + len(volume_label)
    volume_id = struct.pack("<IIII", volume_id_size, _LNK_DRIVE_FIXED, 0,
                            volume_label_offset) + volume_label
    volume_id_offset = header_size
    local_base_path_offset = volume_id_offset + len(volume_id)
    common_path_suffix = b"\x00"            # empty suffix, null-terminated
    common_path_suffix_offset = local_base_path_offset + len(base)
    link_info_size = common_path_suffix_offset + len(common_path_suffix)
    header = struct.pack(
        "<IIIIIII",
        link_info_size, header_size,
        0x00000001,                         # VolumeIDAndLocalBasePath
        volume_id_offset, local_base_path_offset,
        0,                                  # CommonNetworkRelativeLinkOffset (none)
        common_path_suffix_offset)
    return header + volume_id + base + common_path_suffix


# Windows .lnk HotKey modifier flags ([MS-SHLLINK] 2.1.3.1). The full 16-bit
# HotKey field packs the virtual-key code in its LOW byte and these modifier
# flags in its HIGH byte.
_HOTKEYF_SHIFT = 0x01
_HOTKEYF_CONTROL = 0x02
_HOTKEYF_ALT = 0x04


def hotkey_word(key, ctrl=True, alt=True, shift=False):
    """The 16-bit Windows .lnk HotKey value for a single ``key`` character.

    ``key`` is a single letter A-Z (case-insensitive) or digit 0-9; for those the
    virtual-key code is just the uppercase ASCII code (e.g. S -> 0x53, 0 -> 0x30).
    The modifier flags go in the high byte (default Ctrl+Alt, the Windows norm for
    a .lnk letter hotkey). Forgiving by design: blank, None or any unsupported
    input returns 0, which means "no hotkey".
    """
    text = str(key or "").strip()
    if len(text) != 1:
        return 0
    ch = text.upper()
    if "A" <= ch <= "Z" or "0" <= ch <= "9":
        vk = ord(ch)
    else:
        return 0
    modifiers = 0
    if ctrl:
        modifiers |= _HOTKEYF_CONTROL
    if alt:
        modifiers |= _HOTKEYF_ALT
    if shift:
        modifiers |= _HOTKEYF_SHIFT
    return vk | (modifiers << 8)


def build_windows_lnk(target_path, arguments="", working_dir="",
                      description="", icon_path=None, hotkey=0):
    """The raw bytes of a Windows ``.lnk`` (a [MS-SHLLINK] Shell Link).

    ``target_path`` is the TargetPath the shortcut launches (here a browser exe),
    ``arguments`` its command line. Pure stdlib (struct only), so it produces the
    same valid Windows shortcut bytes on Mac, Linux and Windows alike. The bytes
    are: the 76-byte ShellLinkHeader, a LinkInfo carrying the target path, then
    the Unicode StringData blocks (name, working dir, arguments, icon) and a
    4-byte terminal block.
    """
    flags = _LNK_HAS_LINK_INFO | _LNK_IS_UNICODE
    if description:
        flags |= _LNK_HAS_NAME
    if working_dir:
        flags |= _LNK_HAS_WORKING_DIR
    if arguments:
        flags |= _LNK_HAS_ARGUMENTS
    if icon_path:
        flags |= _LNK_HAS_ICON_LOCATION
    header = struct.pack(
        "<I16sIIQQQIIIHHII",
        0x0000004C,                 # HeaderSize (always 76)
        SHELL_LINK_CLSID,           # LinkCLSID
        flags,                      # LinkFlags
        _LNK_FILE_ATTRIBUTE_ARCHIVE,  # FileAttributes
        0, 0, 0,                    # Creation / Access / Write FILETIMEs (unset)
        0,                          # FileSize (unknown / resolved at click time)
        0,                          # IconIndex
        _LNK_SW_SHOWNORMAL,         # ShowCommand
        int(hotkey) & 0xFFFF,       # HotKey
        0,                          # Reserved1
        0,                          # Reserved2
        0)                          # Reserved3
    out = header + _lnk_link_info(target_path)
    # StringData order is fixed by the spec (2.4): NAME, RELATIVE_PATH,
    # WORKING_DIR, COMMAND_LINE_ARGUMENTS, ICON_LOCATION. We omit RELATIVE_PATH.
    if description:
        out += _lnk_string_data(description)
    if working_dir:
        out += _lnk_string_data(working_dir)
    if arguments:
        out += _lnk_string_data(arguments)
    if icon_path:
        out += _lnk_string_data(icon_path)
    out += struct.pack("<I", 0)     # ExtraData TerminalBlock (< 0x00000004)
    return out


def participant_kiosk_arguments(url, kiosk_flag=None, extra_flags=None):
    """The .lnk Arguments string for a kiosk shortcut to ``url``.

    Produces ``--kiosk "<url>" --edge-kiosk-type=fullscreen`` by default (the flags
    are module constants). The URL is quoted, exactly as in a hand-set shortcut.
    """
    flag = PARTICIPANT_KIOSK_FLAG if kiosk_flag is None else kiosk_flag
    extra = PARTICIPANT_KIOSK_EXTRA_FLAGS if extra_flags is None else extra_flags
    parts = ['%s "%s"' % (flag, url)] if flag else ['"%s"' % url]
    if extra:
        parts.append(extra)
    return " ".join(parts)


def participant_seat_url(host, port, room, seat):
    """The exact per-seat link a lab PC opens: the welcome_page_ok=1 room link.

    Seat labels are already validated to [A-Za-z0-9_] (and host/room are simple
    tokens), so no percent-encoding is needed; the link matches the one the launch
    briefing documents byte-for-byte.
    """
    base = "http://%s:%s/room/%s" % (str(host).strip(), str(port).strip(),
                                     sanitize_room_name(room))
    return base + "?participant_label=%s&%s" % (str(seat).strip(), WELCOME_FLAG)


def build_participant_seat_lnk(host, port, room, seat, browser_exe=None,
                               description="", hotkey=0):
    """The .lnk bytes for ONE seat: the kiosk browser opening that seat's link.

    ``hotkey`` is an optional 16-bit Windows HotKey value (0 = no hotkey).
    """
    exe = browser_exe or PARTICIPANT_BROWSER_EXE
    url = participant_seat_url(host, port, room, seat)
    args = participant_kiosk_arguments(url)
    # The exe's own folder as working dir, and the exe as the icon source, so the
    # shortcut shows the browser icon on the participant PC.
    working_dir = exe.rsplit("\\", 1)[0] if "\\" in exe else ""
    return build_windows_lnk(exe, arguments=args, working_dir=working_dir,
                             description=description or ("Seat %s" % seat),
                             icon_path=exe, hotkey=hotkey)


_FILENAME_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name, fallback):
    """A Windows-safe file/folder name: illegal characters -> '_', trimmed of
    trailing dots/spaces (which Windows forbids), with a fallback when empty."""
    cleaned = _FILENAME_BAD_RE.sub("_", str(name or "")).strip().rstrip(". ")
    return cleaned or fallback


def participant_shortcut_folder_name(lab_name):
    """The subfolder the bundle writes into: '<lab name> participant PC shortcuts'."""
    return "%s participant PC shortcuts" % _sanitize_filename(lab_name, "Lab")


def export_participant_shortcuts(dest_dir, lab_name, host, seats, room=None,
                                 port=None, shortcut_label="", browser_exe=None,
                                 hotkey_key=None):
    """Write a bundle of per-seat Windows kiosk .lnk shortcuts under ``dest_dir``.

    Creates ``<dest_dir>/<lab name> participant PC shortcuts/`` and writes one
    ``<shortcut label> - <seat>.lnk`` per seat, each opening that seat's
    welcome_page_ok=1 link in the kiosk browser. Always produces Windows .lnk
    files regardless of the OS this runs on. Returns a small result dict
    (``ok``/``message``, and on success ``folder``/``count``/``files``); never
    raises for the ordinary cancel/validation/IO cases.

    ``shortcut_label`` names the files (falls back to the lab name when blank).
    ``port`` defaults to the launcher's default (8000).

    ``hotkey_key`` is an optional single key character (a letter or digit). When
    set, the SAME Ctrl+Alt+<key> global hotkey is written to every seat .lnk in
    the bundle; blank/None means no hotkey (the default behaviour).
    """
    port = str(port or DEFAULT_CONFIG["port"]).strip() or "8000"
    hotkey = hotkey_word(hotkey_key)
    labels = [str(s).strip() for s in (seats or []) if str(s).strip()]
    if not str(host or "").strip():
        return {"ok": False, "message": "This lab has no host/IP set, so no links can be made."}
    if not labels:
        return {"ok": False, "message": "This lab has no seats, so there is nothing to make shortcuts for."}
    if not dest_dir or not os.path.isdir(dest_dir):
        return {"ok": False, "message": "Choose a destination folder first."}
    room = sanitize_room_name(room)
    label = str(shortcut_label or "").strip() or str(lab_name or "").strip() or "Study room"
    out_dir = os.path.join(dest_dir, participant_shortcut_folder_name(lab_name))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as error:
        return {"ok": False, "message": "Could not create the folder: %s" % error}
    written = []
    for seat in labels:
        data = build_participant_seat_lnk(
            host.strip(), port, room, seat, browser_exe=browser_exe,
            description="%s - %s" % (label, seat), hotkey=hotkey)
        filename = "%s - %s.lnk" % (_sanitize_filename(label, "Study room"),
                                    _sanitize_filename(seat, "seat"))
        path = os.path.join(out_dir, filename)
        try:
            with open(path, "wb") as handle:
                handle.write(data)
        except OSError as error:
            return {"ok": False,
                    "message": "Could not write %s: %s" % (filename, error),
                    "folder": out_dir, "count": len(written), "files": written}
        written.append(path)
    plural = "" if len(written) == 1 else "s"
    return {
        "ok": True,
        "folder": out_dir,
        "count": len(written),
        "files": written,
        "message": ("Wrote %d participant-PC shortcut%s to \"%s\". Copy the folder "
                    "to the lab, drop one .lnk on each participant PC, and rename "
                    "it per machine if you like." % (len(written), plural, out_dir)),
    }


def lab_room_mismatch(cfg, lab_presets=None):
    """Non-blocking launch warning when a config's room lags the lab's room.

    Existing SAVED configs keep their own room when a lab's default_room is later
    changed, but the launch screen should point it out. Returns ``None`` when the
    config's room already matches its lab's CURRENT default room (or the lab has
    no lab room -- Custom / Local / an unknown id), otherwise a dict::

        {config_room, lab_room, lab_label, message}

    describing a YELLOW, non-blocking warning with a one-click "Use new default".
    The config stays fully runnable as-is; both faces call this so they agree on
    when the warning fires (only on a real difference, silent when they match).
    """
    c = normalize_config(cfg)
    lab = c["lab"]
    if lab in (LAB_CUSTOM, LAB_LOCAL):
        return None
    resolved = _presets_or_default(lab_presets)
    preset = find_lab_preset(lab, resolved)
    if preset is None:
        return None
    lab_room = lab_default_room(resolved, lab)
    config_room = c["room_name"].strip() or DEFAULT_ROOM_NAME
    if config_room == lab_room:
        return None
    lab_label = preset.get("name") or lab
    return {
        "config_room": config_room,
        "lab_room": lab_room,
        "lab_label": lab_label,
        "message": ("The lab default room is now '%s' (this config uses '%s'). Use it?"
                    % (lab_room, config_room)),
    }


# ---------------------------------------------------------------------------
# Room enumeration (Feature 3), read the project's own ROOMS by importing its
# settings in a throwaway subprocess with NO lab environment set. A static
# regex parse breaks on ROOMS built at runtime; importing and reading the
# resolved list is reliable, and the subprocess isolation means a slow,
# printing, side-effecting or crashing project only kills its own subprocess.
# ---------------------------------------------------------------------------

ROOMS_PROBE_MARKER = "___OTREE_ROOMS_JSON___"
_ROOMS_PROBE = r'''
import json, os, sys
try:
    import importlib.util as _u
    _p = os.path.join(os.getcwd(), "settings.py")
    _spec = _u.spec_from_file_location("_lab_probe_settings", _p)
    _mod = _u.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _rooms = getattr(_mod, "ROOMS", [])
    _names = [r.get("name") for r in _rooms
              if isinstance(r, dict) and r.get("name")]
    sys.stdout.write("%s" + json.dumps([str(n) for n in _names]))
    sys.stdout.flush()
except Exception as _e:
    sys.stderr.write(repr(_e))
    sys.exit(3)
''' % ROOMS_PROBE_MARKER


def enumerate_project_rooms(project_path, timeout=8.0, python_exe=None):
    """The room names defined in a project's settings.py, or a fallback signal.

    Returns a dict:
      ok       True when settings imported and a ROOMS list was read
      rooms    the room names (possibly empty; an empty list is a real answer)
      empty    True when ok and no rooms are defined
      reason   a short machine tag when ok is False
      error    a copyable, human-readable reason (real subprocess error)
    """
    project_path = (project_path or "").strip()
    result = {"ok": False, "rooms": [], "empty": False, "reason": "", "error": ""}
    if not project_path or not os.path.isdir(project_path):
        result["reason"] = "no_project"
        result["error"] = "No project folder to read rooms from."
        return result
    if not os.path.isfile(settings_path_for(project_path)):
        result["reason"] = "no_settings"
        result["error"] = "No settings.py in %s" % project_path
        return result

    # A clean environment: strip the launcher's own seat variables so the
    # project is imported exactly as it would run OFF the lab.
    env = {k: v for k, v in os.environ.items() if k not in SEAT_ENV_KEYS}
    # Prefer the interpreter the ``otree`` command runs under (its venv), so a
    # settings.py that imports otree (or anything from the project's venv) is read
    # with the right interpreter, not necessarily the launcher's own. Falls back
    # to the launcher's interpreter when the oTree interpreter cannot be located.
    python_exe = python_exe or otree_runtime_python() or sys.executable

    try:
        proc = subprocess.run(
            [python_exe, "-c", _ROOMS_PROBE],
            cwd=project_path, env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True,
            # No console flash + no inherited pythonw handles (see CREATE_NO_WINDOW).
            creationflags=_no_window_flags(),
        )
    except subprocess.TimeoutExpired:
        result["reason"] = "timeout"
        result["error"] = ("Reading the project's rooms timed out after %gs. "
                           "The project may hang on import." % timeout)
        return result
    except OSError as error:
        result["reason"] = "spawn_failed"
        result["error"] = "Could not start a Python subprocess: %s" % error
        return result

    if proc.returncode != 0:
        result["reason"] = "import_failed"
        tail = (proc.stderr or "").strip().splitlines()
        result["error"] = (tail[-1] if tail
                           else "settings.py failed to import (exit code %s)." % proc.returncode)
        return result

    out = proc.stdout or ""
    index = out.rfind(ROOMS_PROBE_MARKER)
    if index < 0:
        result["reason"] = "no_output"
        result["error"] = "The project imported but reported no ROOMS list."
        return result
    payload = out[index + len(ROOMS_PROBE_MARKER):].strip()
    try:
        names = json.loads(payload)
    except ValueError as error:
        result["reason"] = "bad_output"
        result["error"] = "Could not parse the rooms list: %s" % error
        return result

    names = [str(n) for n in names if str(n).strip()]
    result["ok"] = True
    result["rooms"] = names
    result["empty"] = (len(names) == 0)
    return result


def project_has_live_lab_block(project_path):
    """True when settings.py carries a COMPLETE, un-overridden lab support block.

    "Live" means the block WILL define the launcher's chosen room at launch:
    the start marker is present (``has_block``), both markers are present
    (``complete``), and no later top-level ``ROOMS =`` reassignment throws the
    block's room away (not ``rooms_after``). Those are exactly the cases
    ``inspect_settings`` already distinguishes; this single predicate is shared
    by the pre-launch room check and the room picker so they agree on when a
    block-having project also supports the launcher's lab room.

    An absent, cut-off (incomplete), overridden or STALE block returns False, so
    the genuine problems still surface (a stale pre-no-seats-fix block guarded the
    room on the seat file, so it would NOT define an open room at launch).
    Unreadable settings.py -> False too.
    """
    state = inspect_settings(project_path)
    return bool(state.get("has_block") and state.get("complete")
                and not state.get("rooms_after") and not state.get("stale"))


def enumerate_rooms_for_picker(project_path, lab_room=None, timeout=8.0,
                               python_exe=None):
    """Block-aware room list for the room picker.

    Returns the same dict as ``enumerate_project_rooms`` (the honest OFF-lab
    rooms), but when the project has a LIVE lab block the launcher's lab room is
    added to the offered list (de-duplicated), because the block WILL define that
    room at launch. This lets a user who just appended the block actually pick
    "study" (or the configured lab room) instead of being limited to the
    project's own rooms.

    ``enumerate_project_rooms`` itself is NOT changed: it still reports the true
    off-lab rooms. The lab room is layered on here, at the picker, so both faces
    (Tk + web) agree. ``lab_room`` defaults to the lab default room ("study").
    """
    lab_room = (lab_room or DEFAULT_ROOM_NAME).strip() or DEFAULT_ROOM_NAME
    info = enumerate_project_rooms(project_path, timeout=timeout,
                                   python_exe=python_exe)
    if info["ok"] and lab_room not in info["rooms"] \
            and project_has_live_lab_block(project_path):
        info = dict(info)
        info["rooms"] = list(info["rooms"]) + [lab_room]
        info["empty"] = False
        info["lab_room"] = lab_room
    return info


# ---------------------------------------------------------------------------
# Pre-launch preflight gate (checks 1–4)
#
# A handful of fast, fail-SOFT checks run in-process in the ~couple of seconds
# BEFORE the launcher hands off to oTree. Each check returns a dict
# {check, ok, message, detail}. Nothing here hard-blocks a launch: the Tk
# launcher shows any failures in one modal with a "Launch anyway" button, and
# everything else still defers to the oTree dashboard. Shared here (not in the
# Tk file) so the web app can reuse the exact same checks later.
# ---------------------------------------------------------------------------

PREFLIGHT_DB_TIMEOUT = 4          # seconds for the psycopg2 connect probe
PREFLIGHT_ROOMS_TIMEOUT = 6.0     # seconds for the ROOMS subprocess import
# A just-quit server (Ctrl-C) can leave the launch port in TIME_WAIT for a
# moment, so a plain bind fails EADDRINUSE even though nothing is listening.
# These bound the disambiguation (a short connect probe + one retried bind) so
# the port check stops "crying wolf" right after a quit (Job 3).
PREFLIGHT_PORT_CONNECT_TIMEOUT = 0.5   # seconds for the "is anything accepting?" probe
PREFLIGHT_PORT_RETRY_DELAY = 0.4       # seconds to let a TIME_WAIT socket clear before re-binding


# Field tags for pre-launch issues (the ``field`` key). An issue carries one when
# it is about a single form field, so two issues about the SAME field can be
# recognised as such whatever their wording.
FIELD_PROJECT_PATH = "project_path"


def _preflight_result(check, ok, message, detail=""):
    return {"check": check, "ok": bool(ok), "message": message, "detail": detail}


def preflight_check_database(config, timeout=PREFLIGHT_DB_TIMEOUT):
    """Check 1: the lab Postgres is reachable and the credentials authenticate.

    Skipped (reported ok, "n/a") in SQLite mode. Otherwise it makes a REAL
    ``psycopg2.connect`` using the SAME ``DATABASE_URL`` the launcher builds, so
    a wrong host, a down server, a wrong user/password or a missing database all
    surface here rather than at the oTree dashboard. Fail-soft throughout: a
    missing psycopg2 is reported as "could not verify" (ok) rather than crashing.
    """
    c = normalize_config(config)
    if c["db_mode"] == DB_MODE_NONE:
        return _preflight_result(
            "database", True,
            "No lab database (oTree SQLite): nothing to check.",
            "db_mode is 'none', so no Postgres connection is attempted.")

    host = c["db_host"]
    port = c["db_port"]
    problem = config_database_login_problem(c)
    if problem:
        # A database that connects as the admin login, with none stored on this
        # PC: unusable until the admin login is added. Said plainly, no connect.
        result = _preflight_result("database", False, problem,
                                   "Lab Settings > Postgres admin is empty.")
        result["db_mode"] = c["db_mode"]
        result["is_default"] = (current_database_id(c) == _DEFAULT_DB_ID
                                or c["db_mode"] == DB_MODE_LAB)
        return result
    url = build_database_url(c)

    try:
        import psycopg2
    except ImportError:
        return _preflight_result(
            "database", True,
            "Could not verify the lab database: psycopg2 is not installed in "
            "the launcher's Python.",
            "Install psycopg2-binary to have the launcher pre-check the database.")

    try:
        conn = psycopg2.connect(url, connect_timeout=timeout)
    except Exception as error:
        # OperationalError covers wrong host/port/user/password/missing DB; any
        # other psycopg2 or DSN problem is treated the same way (fail-soft).
        result = _preflight_result(
            "database", False,
            "Could not connect to the %s database at %s:%s: %s"
            % ("default" if current_database_id(c) == _DEFAULT_DB_ID else "chosen",
               host, port,
               _pg_error(error)),
            mask_database_url(url))
        # Carry the db_mode so issue_fix_for can offer "Use lab default instead"
        # only when it is a CUSTOM database that failed (switching to the lab
        # default is meaningless when the lab default is itself what failed).
        result["db_mode"] = c["db_mode"]
        result["is_default"] = (current_database_id(c) == _DEFAULT_DB_ID
                                or c["db_mode"] == DB_MODE_LAB)
        return result
    try:
        conn.close()
    except Exception:
        pass
    return _preflight_result(
        "database", True,
        "Connected to the database at %s:%s." % (host, port),
        mask_database_url(url))


def _port_bind_free(port):
    """(True, None) when a fresh socket can bind ``port`` here, else (False, err).

    SO_REUSEADDR is deliberately LEFT OFF: on POSIX it would let the probe bind
    over a TIME_WAIT socket (we WANT to detect that here and disambiguate it
    below), and on Windows it would let the probe bind over an ACTIVE listener
    (hijack), which would hide a genuinely-busy port. So the plain bind is the
    conservative signal; the caller sorts a real listener from a TIME_WAIT lag.
    """
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("", port))
        return True, None
    except OSError as error:
        return False, error
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _port_has_listener(port, host="127.0.0.1", timeout=PREFLIGHT_PORT_CONNECT_TIMEOUT):
    """True when something is actually ACCEPTING connections on ``port``.

    A live server accepts the test connection; a lingering TIME_WAIT / closing
    socket left by a just-quit server does NOT (connection refused), so this
    tells a genuinely-busy port from the post-Ctrl-C release lag. The launcher
    binds oTree on all interfaces, so a localhost probe reaches it.
    """
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


# The one-click shortcut's message box for the same situation (no window, so no
# Re-check button to point at).
HEADLESS_SESSION_RUNNING_MESSAGE = (
    "A session is already running on the launch port, so the one-click shortcut "
    "did not start another one and did not reset the database. Stop the running "
    "session first (close its server window), then run the shortcut again.")
PORT_IN_USE_MESSAGE = ("Port %d is in use: a session is already running on it. Stop it "
                       "first (close its server window), then Re-check.")


# The one-click shortcut (--run) has no window, so it cannot show the launch
# screen's warnings. It STOPS AND ASKS ("Launch anyway" / "Cancel", one line per
# problem, one box for all of them) for exactly these, and stays silent about
# everything else (the shared-database caution, local changes, being offline,
# info items). A running session on the port is not asked about: it stops the
# shortcut outright (HEADLESS_SESSION_RUNNING_MESSAGE), as before.
#   1. a newer version of the study is waiting online (quiet check; offline, not
#      a repository or a login problem = silent)
#   2. the database cannot be reached
#   3. the config's saved room is not the lab's default room (a config saved
#      WITH the lab default follows it and never warns: follow_lab_room)
HEADLESS_UPDATE_TIMEOUT = 5
HEADLESS_ASK_TITLE = "oTree Lab Launcher"
HEADLESS_ASK_QUESTION = "Launch anyway?"
HEADLESS_CANCELLED_EXIT = 10


def headless_ask_problems(cfg, lab_presets=None, update_checker=None, db_checker=None):
    """The problems the one-click shortcut asks about before it launches ``cfg``
    (already resolved with :func:`follow_lab_room`): a list of one-line texts,
    empty when there is nothing to ask. The update check and the database check
    run side by side so the shortcut is not slowed down by their sum.
    ``update_checker`` / ``db_checker`` are test seams. Never raises."""
    c = normalize_config(cfg)
    found = {}

    def check_update():
        try:
            checker = update_checker or (lambda path: project_update_status(
                path, timeout=HEADLESS_UPDATE_TIMEOUT))
            found["update"] = checker(c["project_path"].strip())
        except Exception:
            found["update"] = None

    def check_database():
        try:
            found["database"] = (db_checker or preflight_check_database)(c)
        except Exception:
            found["database"] = None

    workers = [threading.Thread(target=check_update, daemon=True),
               threading.Thread(target=check_database, daemon=True)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(HEADLESS_UPDATE_TIMEOUT + PREFLIGHT_DB_TIMEOUT + 10)

    problems = []
    if (found.get("update") or {}).get("state") == UPDATE_STATE_BEHIND:
        problems.append(HEADLESS_UPDATE_PROBLEM)
    database = found.get("database")
    if database and not database.get("ok"):
        problems.append(str(database.get("message") or "The database cannot be reached."))
    try:
        mismatch = lab_room_mismatch(c, lab_presets)
    except Exception:
        mismatch = None
    if mismatch:
        problems.append("This config uses the room '%s'; the lab default (what the "
                        "participant computers open) is '%s'."
                        % (mismatch["config_room"], mismatch["lab_room"]))
    return problems


def headless_ask_text(problems, config_name=""):
    """The text of the one-click shortcut's question box: every problem on its
    own line, then "Launch anyway?"."""
    problems = [str(p).strip() for p in problems if str(p).strip()]
    head = ('"%s" has %s to look at first:' % (
        config_name, "something" if len(problems) == 1 else "a few things")
        if config_name else "Before this launch:")
    lines = [head, ""] + [("• " + p) for p in problems] + ["", HEADLESS_ASK_QUESTION]
    return "\n".join(lines)


def running_server_problem(config):
    """The last guard before a launch touches anything: "" when the launch port
    is free, else the plain reason (a server is already answering there).

    Every launch path (both faces and the headless one-click run) calls this
    BEFORE ``otree resetdb``, so a relaunch can never reset the database of a
    session that is still running, and can never report the OLD server's answer
    as its own success. Fail-soft: anything unexpected reads as free."""
    try:
        result = preflight_check_port(config)
    except Exception:
        return ""
    if result.get("ok") or not result.get("blocking"):
        return ""
    return str(result.get("message") or "")


def preflight_check_port(config):
    """Check 2: the launch port is free to bind on this machine.

    Tries to bind the port oTree will use (from ``config['port']``). A bind that
    fails with "address already in use" USUALLY means another server is still
    running -- but right after a Ctrl-C quit the OS can hold the port in
    TIME_WAIT for a moment, so a plain bind fails even though nothing is
    listening (Job 3). To stop "crying wolf" then, a failed bind is
    disambiguated: only when something actually ACCEPTS a test connection is the
    port reported in use; a lingering TIME_WAIT socket (which refuses
    connections) reads as free, after one retried bind following a short pause.
    Either way this is a SOFT check -- launch-anyway still works.
    """
    import errno
    c = normalize_config(config)
    raw = str(c["port"]).strip()
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return _preflight_result(
            "port", True,
            "No numeric launch port set, skipping the port check.",
            "port=%r" % raw)

    # A value outside 1..65535 would make socket.bind raise OverflowError (not
    # OSError), so it slipped past the except below and crashed the preflight.
    # Treat it as a clean validation failure instead.
    if not (1 <= port <= 65535):
        return _preflight_result(
            "port", False,
            "The launch port %d is out of range (it must be between 1 and 65535)." % port,
            "port=%r" % raw)

    ok, error = _port_bind_free(port)
    if ok:
        return _preflight_result("port", True, "Port %d is free." % port, "")

    # A non-"in use" error (e.g. an odd platform errno) is not something we can
    # act on: pass with a note, exactly as before.
    if error.errno not in (errno.EADDRINUSE, errno.EACCES):
        return _preflight_result(
            "port", True,
            "Could not test port %d (%s), continuing." % (port, error),
            str(error))

    def _in_use(err):
        # INFORM, never kill: the launcher only starts its own things and never
        # stops whatever holds the port. The operator chooses a different port or
        # stops the other server themselves.
        # A launch can never work while the port is taken (the new server
        # cannot bind), and a "launch anyway" would reset the database under the
        # session that is still running: so this is a MUST-FIX (``blocking``),
        # not a warning. Both faces render it as a blocker with Re-check.
        result = _preflight_result("port", False, PORT_IN_USE_MESSAGE % port, str(err))
        result["blocking"] = True
        return result

    # EACCES is a permission problem (a privileged port), not a TIME_WAIT lag --
    # keep reporting it as busy.
    if error.errno == errno.EACCES:
        return _in_use(error)

    # EADDRINUSE: a live listener, or just the release lag from a quit server?
    if _port_has_listener(port):
        return _in_use(error)

    # Nothing is accepting -- most likely a TIME_WAIT socket from a just-quit
    # server. Give the OS a moment and re-check the bind once before deciding.
    time.sleep(PREFLIGHT_PORT_RETRY_DELAY)
    ok2, error2 = _port_bind_free(port)
    if ok2:
        return _preflight_result("port", True, "Port %d is free." % port, "")
    if _port_has_listener(port):
        return _in_use(error2)
    return _preflight_result(
        "port", True,
        "Port %d looks free: nothing is listening (a just-closed server may "
        "still be releasing it)." % port,
        str(error2))


def preflight_check_project(config, timeout=PREFLIGHT_ROOMS_TIMEOUT):
    """Check 3: the project has a settings.py and the chosen room is in its ROOMS.

    The project folder must exist and hold a ``settings.py``; then the chosen
    room (``config['room_name']``) must appear in the project's own ``ROOMS``.
    Reuses ``enumerate_project_rooms`` (the same path the room picker uses). If
    the project's rooms cannot be read (import error / timeout) the room half is
    a soft pass with a note, never a hard failure.
    """
    c = normalize_config(config)
    path = c["project_path"].strip()
    room = c["room_name"].strip()
    if not path or not os.path.isdir(path):
        out = _preflight_result(
            "project", False,
            "Project folder not found: %s" % (path or "(none chosen)"),
            "Pick the folder that holds settings.py.")
        # Which form field this is about, so the pre-launch screen can drop this
        # soft warning when a hard blocker already covers the same field (see
        # suppress_shadowed_warnings).
        out["field"] = FIELD_PROJECT_PATH
        return out
    if not os.path.isfile(settings_path_for(path)):
        out = _preflight_result(
            "project", False,
            "No settings.py found in %s. Is this an oTree project?" % path,
            settings_path_for(path))
        # Tag it so the pre-launch screen can style this one as a RED problem (a
        # folder that is very likely not an oTree project), not a neutral warning.
        out["kind"] = "not_otree"
        return out

    info = enumerate_project_rooms(path, timeout=timeout)
    if info["ok"]:
        if room and room not in info["rooms"]:
            # A COMPLETE, un-overridden lab support block WILL define whatever
            # room the launcher selects at launch (its ROOMS clause fires when
            # OTREE_LAB_ROOM_NAME is set), even though the static import above —
            # run with the lab seat env STRIPPED, i.e. exactly as it runs OFF the
            # lab — cannot see it. So a block-having project whose static ROOMS
            # lack the chosen room is NOT a problem: pass with a note. We still
            # warn for the genuine cases (no/incomplete block, or a later
            # top-level ROOMS = that overrides the block) — project_has_live_lab_block
            # is False for all of those.
            if project_has_live_lab_block(path):
                return _preflight_result(
                    "project", True,
                    "settings.py has the lab support block, which defines room "
                    "'%s' at launch." % room,
                    "Static ROOMS off the lab: %s (the block adds '%s' when the "
                    "launcher sets the seat file)."
                    % (", ".join(info["rooms"]) or "(none)", room))
            out = _preflight_result(
                "project", False,
                "Room '%s' is not defined in this project's ROOMS." % room,
                "Rooms found: %s" % (", ".join(info["rooms"]) or "(none)"))
            # Carry the machine-readable room list + a kind tag so the pre-launch
            # screen can offer an inline room picker (issue_fix_for reads these).
            out["kind"] = "room"
            out["rooms"] = list(info["rooms"])
            return out
        return _preflight_result(
            "project", True,
            "settings.py found and room '%s' is defined in ROOMS." % room,
            "Rooms: %s" % (", ".join(info["rooms"]) or "(none)"))
    return _preflight_result(
        "project", True,
        "settings.py found; could not read the project's ROOMS to confirm '%s'."
        % room,
        info.get("error", ""))


def otree_available():
    """True when oTree can be launched here: the ``otree`` command is on PATH or
    the ``otree`` package is importable in this Python."""
    if shutil.which("otree"):
        return True
    try:
        import importlib.util
        return importlib.util.find_spec("otree") is not None
    except (ImportError, ValueError):
        return False


def preflight_check_otree(config=None):
    """Check 4: oTree is installed / runnable in this environment."""
    if otree_available():
        return _preflight_result(
            "otree", True, "oTree is available in this environment.", "")
    return _preflight_result(
        "otree", False,
        "oTree is not installed in this environment.",
        "The launcher runs 'otree resetdb' / 'otree prodserver'; install oTree "
        "or start the launcher from the Python environment that has it.")


PREFLIGHT_PSYCOPG2_TIMEOUT = 6   # seconds for the oTree-runtime import probe


def otree_runtime_python():
    """Best-effort path to the Python interpreter the ``otree`` command runs under.

    This matters because the launcher starts oTree as a SUBPROCESS
    (``otree resetdb`` / ``otree prodserver``), and on macOS especially the
    interpreter behind that ``otree`` console script can DIFFER from the
    launcher's own ``sys.executable`` (e.g. the launcher runs under the system
    ``python3`` while oTree lives in ``~/Library/Python/3.12``). To answer
    "is psycopg2 importable where oTree actually runs?" we must probe THAT
    interpreter, not our own.

    The reliable, cross-tool signal is the Python interpreter CO-LOCATED with the
    ``otree`` launcher: pip, uv, pipx and system installs all place the console
    script next to the interpreter it targets (``.../bin/otree`` beside
    ``.../bin/python``; on Windows ``Scripts\\otree.exe`` beside
    ``Scripts\\python.exe``). We fall back to parsing the console script's
    shebang, including the ``#!/bin/sh`` ... ``'''exec' '<python>'`` polyglot that
    pip/uv emit (a naive shebang read would see ``/bin/sh`` there). Returns
    ``None`` when it cannot be determined."""
    exe = shutil.which("otree")
    if not exe:
        return None
    bindir = os.path.dirname(exe)
    names = ("python3", "python", "python3.13", "python3.12", "python3.11",
             "python3.10", "python.exe", "python3.exe")
    for base in (bindir, os.path.dirname(bindir), os.path.join(bindir, "..", "Scripts")):
        for name in names:
            cand = os.path.normpath(os.path.join(base, name))
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    # Fall back to the shebang of the console script.
    try:
        with open(exe, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    text = head.decode("utf-8", "replace")
    lines = text.splitlines()
    if lines and lines[0].startswith("#!"):
        parts = lines[0][2:].strip().split()
        if parts:
            cand = parts[1] if (len(parts) > 1 and os.path.basename(parts[0]) == "env") else parts[0]
            if os.path.basename(cand).startswith("python"):
                if os.path.isabs(cand) and os.path.exists(cand):
                    return cand
                found = shutil.which(cand)
                if found:
                    return found
    # The "#!/bin/sh" polyglot pip/uv emits names the python in an exec line.
    match = re.search(r"""exec['"]?\s+['"]([^'"]*python[^'"]*)['"]""", text)
    if match and os.path.exists(match.group(1)):
        return match.group(1)
    return None


def psycopg2_available():
    """True when ``psycopg2`` can be imported in the environment oTree runs in.

    Probes the oTree-runtime interpreter (``otree_runtime_python``) in a short
    subprocess so the answer reflects where ``otree resetdb`` will actually try
    to import the driver, not necessarily the launcher's own interpreter. Falls
    back to the launcher's interpreter only when the oTree interpreter cannot be
    located (best reasonable check; see POLISH note on the residual limitation).
    """
    interp = otree_runtime_python()
    if interp:
        try:
            result = subprocess.run(
                [interp, "-c", "import psycopg2"],
                # Redirect ALL three streams so a windowless (pythonw) parent
                # never hands the child an invalid inherited handle to block on,
                # and no console window flashes (see CREATE_NO_WINDOW).
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=PREFLIGHT_PSYCOPG2_TIMEOUT,
                creationflags=_no_window_flags())
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass   # fall through to the launcher's own interpreter
    try:
        import importlib.util
        return importlib.util.find_spec("psycopg2") is not None
    except (ImportError, ValueError):
        return False


def preflight_check_psycopg2(config):
    """Check 5: psycopg2 is importable when a Postgres database is selected.

    In SQLite / no-database mode this is skipped (reported ok, "n/a"). When the
    config selects a Postgres database (lab or custom) but ``psycopg2`` cannot be
    imported, ``otree resetdb`` would later die with a cryptic
    ``ModuleNotFoundError: No module named 'psycopg2'``. Catch it here, up front,
    with an actionable message instead. Only Postgres modes trigger it, SQLite /
    no-database never does.
    """
    c = normalize_config(config)
    if c["db_mode"] == DB_MODE_NONE:
        return _preflight_result(
            "psycopg2", True,
            "No lab database (oTree SQLite): psycopg2 is not needed.",
            "db_mode is 'none', so no Postgres driver is required.")
    if psycopg2_available():
        return _preflight_result(
            "psycopg2", True, "psycopg2 (the Postgres driver) is installed.", "")
    return _preflight_result(
        "psycopg2", False,
        "Postgres is selected but psycopg2 is not installed. Run: "
        "pip install psycopg2-binary, or use the oTree default (SQLite).",
        "Without psycopg2 'otree resetdb' fails with "
        "ModuleNotFoundError: No module named 'psycopg2'.")


def preflight(config, lab_info=None):
    """Run the pre-launch checks and return their result dicts, in order.

    Each entry is ``{check, ok, message, detail}``. This is FAIL-SOFT: it reports
    problems, it never blocks, the caller lists any failures and offers a
    "Launch anyway". ``lab_info`` is accepted for parity with the web app and
    future checks; the checks read everything they need from ``config`` (in
    lab-database mode ``normalize_config`` has already forced the lab Postgres
    credentials into it, so ``build_database_url`` yields the real lab DSN).

    The psycopg2 check runs first: when a Postgres database is chosen but the
    driver is missing, its actionable "pip install psycopg2-binary" message is
    the one the user most needs to see (the database connect check below then
    reports the same absence as a soft "could not verify").
    """
    return [
        preflight_check_psycopg2(config),
        preflight_check_database(config),
        preflight_check_port(config),
        preflight_check_project(config),
        preflight_check_otree(config),
    ]


def preflight_failures(results):
    """Just the failed checks from a ``preflight()`` result list."""
    return [r for r in results if not r.get("ok", False)]


def suppress_shadowed_warnings(issues):
    """Drop every soft warning whose field already has a hard blocker.

    General rule for the pre-launch screen (both launchers): a hard blocker on a
    field suppresses the softer warning about that same field. With no project
    folder chosen, the must-fix "Choose your oTree project folder" item (with its
    Choose folder action) is the whole story, so the fail-soft preflight "Project
    folder not found" warning about the same field is not shown beside it.

    ``issues`` is the list of ``{level: 'block'|'warn', field?, ...}`` dicts the
    screen renders. Issues without a ``field`` are never touched, order is kept,
    and a new list is returned.
    """
    blocked = set(issue.get("field") for issue in issues
                  if issue.get("level") == "block" and issue.get("field"))
    return [issue for issue in issues
            if not (issue.get("level") == "warn" and issue.get("field") in blocked)]


def issue_fix_for(failure):
    """Inline-action metadata for a preflight failure on the pre-launch screen.

    Every issue the "Before you launch" screen shows should be resolvable right
    there (Julian: "get to a point where you can just click Launch"). This maps a
    failed check to a UI-agnostic fix descriptor both launchers render:

      psycopg2  -> change_to_sqlite  (switch to the oTree default database)
      port      -> recheck           (re-run the checks after freeing the port)
      project   -> pick_room         (choose one of the project's own rooms),
                                      only for the room-not-in-ROOMS case, and it
                                      carries the ``rooms`` list to pick from.

    Anything else returns an empty dict (hint only, no inline action).
    """
    check = failure.get("check")
    if check == "psycopg2":
        return {"fix": "change_to_sqlite", "fix_label": "Change to SQLite"}
    if check == "port":
        return {"fix": "recheck", "fix_label": "Re-check"}
    if check == "project" and failure.get("kind") == "room":
        return {"fix": "pick_room", "fix_label": "Use this room",
                "rooms": list(failure.get("rooms", []))}
    # A CUSTOM database that could not be connected to -> offer a one-click switch
    # to the lab shared (default) database so the user can re-launch at once. Not
    # offered for a failed LAB database (switching to it would change nothing).
    if (check == "database" and failure.get("db_mode") == DB_MODE_CUSTOM
            and not failure.get("is_default") and _DEFAULT_DB_ID in _DB_REGISTRY):
        return {"fix": "use_lab_default", "fix_label": "Use this computer's default database"}
    return {}


def switch_to_lab_default(cfg):
    """Return a normalized copy of ``cfg`` switched to THIS PC's default database.

    The switch behind the "Use this computer's default database" recovery action
    both launchers show when the chosen database cannot be connected to. It pins
    the config to the default's id (or SQLite when the PC has none). The single
    source of truth for that switch, shared by both UIs.
    """
    out = dict(cfg or {})
    out["database_id"] = (_DEFAULT_DB_ID if _DEFAULT_DB_ID in _DB_REGISTRY
                          else DB_BUILTIN_SQLITE)
    out["db_mode"] = DB_MODE_CUSTOM
    return normalize_config(out)


# ---------------------------------------------------------------------------
# Create a new Postgres database (Feature 2), uses psycopg2 in AUTOCOMMIT
# (CREATE DATABASE cannot run inside a transaction). psycopg2 is imported
# lazily so importing this module stays standard-library-only for the Tk app.
# ---------------------------------------------------------------------------

# A conservative set of SQL reserved words rejected up front, so a database or
# role name that would need quoting (or confuse a later hand-written query) is
# refused with a clear message rather than quietly created.
PG_RESERVED_WORDS = frozenset("""
all analyse analyze and any array as asc asymmetric authorization binary both
case cast check collate column constraint create cross current_catalog
current_date current_role current_schema current_time current_timestamp
current_user default deferrable desc distinct do else end except false fetch
for foreign freeze from full grant group having ilike in initially inner
intersect into is isnull join lateral leading left like limit localtime
localtimestamp natural not notnull null offset on only or order outer overlaps
placing primary references returning right select session_user similar some
symmetric table tablesample then to trailing true union unique user using
variadic verbose when where window with postgres template0 template1
""".split())


def validate_pg_identifier(name, kind="database"):
    """Check a database/role name against Postgres identifier rules.

    Validated BEFORE any SQL is sent. Even so the actual statements use
    psycopg2's ``sql.Identifier`` quoting, so this is a friendly-error gate, not
    the only line of defence against injection.
    """
    name = (name or "").strip()
    if not name:
        return False, "enter a %s name." % kind
    if len(name) > 63:
        return False, "the %s name must be 63 characters or fewer." % kind
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
        return False, ("the %s name may use only letters, digits, underscore and $, and may "
                       "not start with a digit (no spaces or other characters)." % kind)
    if name.lower() in PG_RESERVED_WORDS:
        return False, "%r is a reserved SQL word; choose a different %s name." % (name, kind)
    return True, ""


def _pg_error(error):
    """A short, copyable one-line rendering of a psycopg2 error."""
    text = str(error).strip()
    first = text.splitlines()[0] if text else error.__class__.__name__
    return first


def _try_drop_role(conn, sql, role):
    """Best-effort cleanup of a role we created before CREATE DATABASE failed."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        return True, ""
    except Exception as error:
        return False, _pg_error(error)


def existing_database_fields(admin, new_db, new_user="", new_password="",
                             host=None, port=None):
    """The Custom config fields for REGISTERING an already-existing database
    (no CREATE DATABASE). ``host``/``port`` are the dialog's Host/Port fields;
    when blank they fall back to the Lab Settings admin host/port (localhost /
    5432 by default). A blank user falls back to the admin role (and its
    password), so the entry has the same shape as a freshly-created one."""
    admin = admin or {}
    a_user = str(admin.get("admin_username", "")).strip()
    a_pw = str(admin.get("admin_password", ""))
    user = str(new_user or "").strip()
    host = str(host or "").strip() or str(admin.get("admin_host", "")).strip() or "localhost"
    port = str(port or "").strip() or str(admin.get("admin_port", "")).strip() or "5432"
    return {
        "db_mode": DB_MODE_CUSTOM,
        "db_name": str(new_db or "").strip(),
        "db_user": user or a_user,
        "db_password": (new_password or "") if user else a_pw,
        "db_host": host,
        "db_port": port,
        # Blank user = connect as the admin login, by reference (the stored
        # entry keeps no copy; see normalize_database_entry).
        "uses_admin_login": not user,
    }


def create_database(admin, new_db, new_user="", new_password="", host=None, port=None):
    """Create a Postgres database (and optionally a role) using the admin config.

    Returns a result dict. On a confirmed success ``ok`` is True and ``fields``
    holds the Custom database config to auto-fill; on anything else ``ok`` is
    False, ``fields`` is None (nothing is auto-filled) and ``message`` carries a
    real, copyable reason. Fails closed at every step.

    Rules (settled by Julian): an existing name is never clobbered; an existing
    role keeps its own password (we never reset it) and if that password does
    not connect we refuse to auto-fill; a blank new user means the admin role
    owns the database; the admin config must be complete or the create is
    blocked; a role created just before a failed CREATE DATABASE is dropped
    again (or named if it cannot be), never left a silent orphan.

    ``host``/``port`` (the dialog's Host/Port fields) override the admin
    host/port when given. Creating only works on THIS computer, so a non-local
    host is refused before anything connects (register it instead).
    """
    result = {"ok": False, "reason": "", "message": "", "created_db": False,
              "created_role": False, "connect_ok": False, "fields": None}

    admin = admin or {}
    # The target host (the dialog's Host field) is checked FIRST: a database on
    # another host can only be registered, whatever the admin details say.
    target = str(host or "").strip()
    if host is not None and not is_local_host(target):
        result["reason"] = "remote_host"
        result["message"] = ("Not created: %s is another host. %s Tick \"The database "
                             "already exists in Postgres\" to register it."
                             % (target, REMOTE_DB_NOTE))
        return result
    missing = pg_admin_ready(admin)
    if missing:
        result["reason"] = "admin_missing"
        result["message"] = ("Fill in the Postgres admin details on the Lab Settings page first "
                             "(%s). No database was created." % ", ".join(missing))
        return result
    a_user = str(admin.get("admin_username", "")).strip()
    a_pw = str(admin.get("admin_password", ""))
    a_host = str(admin.get("admin_host", "")).strip()
    a_port = str(admin.get("admin_port", "")).strip()
    if host is not None:
        a_host = target or a_host
    if str(port or "").strip():
        a_port = str(port).strip()

    # Creating a database (CREATE ROLE / CREATE DATABASE) is only supported on a
    # local Postgres for now. A remote admin host is refused here, so the block
    # holds for the Tk UI and for any stale preset that slips past the UI guard.
    if not is_local_host(a_host):
        result["reason"] = "remote_host"
        result["message"] = ("Creating a database on a remote host (%s) is not supported yet. "
                             "Register the existing database instead." % a_host)
        return result

    try:
        import psycopg2
        from psycopg2 import sql
        import psycopg2.errors as pgerrors
    except ImportError:
        result["reason"] = "no_psycopg2"
        result["message"] = ("The psycopg2 library is not installed, so the launcher cannot "
                             "create a database. Install it with:  pip install psycopg2-binary")
        return result

    ok, why = validate_pg_identifier(new_db, "database")
    if not ok:
        result["reason"] = "bad_db_name"
        result["message"] = "Failed: " + why
        return result

    new_user = (new_user or "").strip()
    new_password = new_password or ""
    if new_user:
        ok, why = validate_pg_identifier(new_user, "user")
        if not ok:
            result["reason"] = "bad_user_name"
            result["message"] = "Failed: " + why
            return result

    # Connect as the admin, to the always-present "postgres" database, and turn
    # on AUTOCOMMIT because CREATE DATABASE cannot run inside a transaction.
    try:
        conn = psycopg2.connect(dbname="postgres", user=a_user,
                                password=pg_password_arg(a_pw),
                                host=a_host, port=a_port, connect_timeout=8)
    except psycopg2.OperationalError as error:
        result["reason"] = "admin_connect_failed"
        result["message"] = ("Failed: could not connect as the admin user %r at %s:%s: %s"
                             % (a_user, a_host, a_port, _pg_error(error)))
        return result
    conn.autocommit = True

    role_existed = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (new_db,))
            if cur.fetchone():
                result["reason"] = "db_exists"
                result["message"] = "Failed: a database with that name already exists."
                return result

            owner = a_user
            if new_user:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (new_user,))
                role_existed = bool(cur.fetchone())
                owner = new_user
                if not role_existed:
                    try:
                        # A blank password creates the role WITHOUT one
                        # (PASSWORD '' would only be turned into NULL with a
                        # warning); it can then log in only where pg_hba.conf
                        # trusts it, which is what "no password" means.
                        if new_password:
                            cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                                sql.Identifier(new_user), sql.Literal(new_password)))
                        else:
                            cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN").format(
                                sql.Identifier(new_user)))
                        result["created_role"] = True
                    except pgerrors.InsufficientPrivilege:
                        result["reason"] = "no_createrole"
                        result["message"] = ("Failed: the admin role %r lacks the CREATEROLE "
                                             "privilege needed to create the new user %r."
                                             % (a_user, new_user))
                        return result
                    except psycopg2.Error as error:
                        result["reason"] = "create_role_failed"
                        result["message"] = ("Failed: could not create the role %r: %s"
                                             % (new_user, _pg_error(error)))
                        return result

            try:
                cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(new_db), sql.Identifier(owner)))
                result["created_db"] = True
            except psycopg2.Error as error:
                # Partial failure: a role we just created has no database. Drop
                # it again so it is not left an orphan; if the drop fails, name
                # it so the operator can clean it up by hand.
                orphan = ""
                if result["created_role"]:
                    dropped, drop_err = _try_drop_role(conn, sql, new_user)
                    if not dropped:
                        orphan = (" The role %r was created but could NOT be removed (%s); "
                                  "it is left behind." % (new_user, drop_err))
                if isinstance(error, pgerrors.InsufficientPrivilege):
                    base = ("Failed: the admin role %r lacks the CREATEDB privilege needed to "
                            "create a database." % a_user)
                else:
                    base = "Failed: could not create the database: %s" % _pg_error(error)
                result["reason"] = "create_db_failed"
                result["message"] = base + orphan
                return result

            # Re-check pg_database to confirm the database really exists now.
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (new_db,))
            confirmed = bool(cur.fetchone())
    finally:
        conn.close()

    if not confirmed:
        result["reason"] = "not_confirmed"
        result["message"] = ("Failed: the database was reported created but was not found on a "
                             "re-check. Nothing was auto-filled.")
        return result

    # Verify a real CONNECT with the resulting Custom credentials: the new role
    # if one was named, otherwise the admin role that owns the database.
    if new_user:
        c_user, c_pw = new_user, new_password
    else:
        c_user, c_pw = a_user, a_pw
    fields = {
        "db_mode": DB_MODE_CUSTOM,
        "db_name": new_db,
        "db_user": c_user,
        "db_password": c_pw,
        "db_host": a_host,
        "db_port": a_port,
        # No new user: the database is owned by, and connects as, the admin
        # login -- stored as a reference, never a copy of its password.
        "uses_admin_login": not new_user,
    }
    try:
        vconn = psycopg2.connect(dbname=new_db, user=c_user,
                                 password=pg_password_arg(c_pw),
                                 host=a_host, port=a_port, connect_timeout=8)
        vconn.close()
        result["connect_ok"] = True
    except psycopg2.Error as error:
        if new_user and not new_password:
            # A NEW user with a blank password: Postgres refuses it unless
            # pg_hba.conf trusts it (Postgres.app does, a standard Windows
            # install does not). The database is real and useful, so keep it
            # and register it, but say clearly what to fix.
            result["ok"] = True
            result["reason"] = "no_password_login"
            result["fields"] = fields
            result["warning"] = BLANK_PASSWORD_CREATED_WARNING
            result["connect_error"] = _pg_error(error)
            result["message"] = ("Created database %r, owned by %s role %r. %s"
                                 % (new_db, "the existing" if role_existed else "the new",
                                    new_user, BLANK_PASSWORD_CREATED_WARNING))
            return result
        if new_user and role_existed:
            result["reason"] = "role_pw_mismatch"
            result["message"] = ("Database %r was created and owned by %r, but could not connect "
                                 "with the password given - that role's own password is in force. "
                                 "Enter the correct password or use a different role name. "
                                 "Nothing was auto-filled." % (new_db, new_user))
        else:
            result["reason"] = "connect_failed"
            result["message"] = ("Database %r was created, but a test connection as %r failed: %s. "
                                 "Nothing was auto-filled." % (new_db, c_user, _pg_error(error)))
        return result

    result["ok"] = True
    result["fields"] = fields
    if new_user and role_existed:
        who = "owned by the existing role %r" % new_user
    elif new_user:
        who = "owned by the new role %r" % new_user
    else:
        who = "owned by the admin role %r" % a_user
    result["message"] = "Created database %r, %s. Connection verified." % (new_db, who)
    return result


def test_database_connection(dbname, user, password, host, port, timeout=5):
    """Advisory, NEVER-raising psycopg2 connect check for an existing database.

    Used when registering a remote existing database: it tries a short-timeout
    connect and reports the result, but is purely informational -- registration
    goes ahead whatever this returns. Returns a dict:
      ``ok``      True on a successful connect,
      ``error``   a short one-line reason when it could not connect (else ""),
      ``skipped`` True when psycopg2 is not installed (nothing was tried).
    """
    result = {"ok": False, "error": "", "skipped": False}
    try:
        import psycopg2
    except ImportError:
        result["skipped"] = True
        result["error"] = "psycopg2 is not installed, so the connection was not tested."
        return result
    try:
        conn = psycopg2.connect(
            dbname=dbname, user=user, password=pg_password_arg(password),
            host=str(host or "").strip() or "localhost",
            port=str(port or "").strip() or "5432",
            connect_timeout=timeout)
        conn.close()
        result["ok"] = True
    except Exception as error:   # never raise: this is advisory only
        result["error"] = _pg_error(error)
    return result


def registered_database_login_warning(fields, tester=None):
    """The connection check after REGISTERING an existing database on this
    computer (both faces). Tries to log in with the registered user/password;
    returns "" when it worked (or psycopg2 is missing, so nothing was tried),
    else a warning -- the registration itself is never undone. A refused login
    with a BLANK password gets :data:`BLANK_PASSWORD_REGISTERED_WARNING`.
    ``tester`` replaces :func:`test_database_connection` in tests."""
    fields = fields or {}
    tester = tester or test_database_connection
    if fields.get("uses_admin_login") and not str(fields.get("db_user", "") or "").strip():
        return ADMIN_LOGIN_MISSING
    user = str(fields.get("db_user", "") or "")
    password = str(fields.get("db_password", "") or "")
    check = tester(str(fields.get("db_name", "") or ""), user, password,
                   fields.get("db_host", ""), fields.get("db_port", ""))
    if check.get("ok") or check.get("skipped"):
        return ""
    if not password:
        return BLANK_PASSWORD_REGISTERED_WARNING % (user or "this user")
    return ("Registered, but a test login as %s failed: %s. Check the name, user and "
            "password (Edit database)." % (user or "this user",
                                           check.get("error") or "unknown error"))


# NOTE: pure logic, no tkinter. Shared by otree_lab_launcher.py (Tk UI) and
# otree_launcher_web.py (pywebview UI).


# ===========================================================================
# 1.5.0 MIGRATION: the old (v0) data folder -> schema 1
# ---------------------------------------------------------------------------
# prepare_storage() runs at startup in BOTH faces and the headless --run, before
# any UI. It is versioned (schema_version; missing = v0), holds a lock, is
# idempotent (machine.json ``migrations`` records a finished run) and resumable
# (an interrupted run leaves its backup, which the next run converts from),
# refuses to touch a file with a newer schema, and is fail-soft: an error never
# crashes the app and leaves the old files in place (the loaders then read them
# through an in-memory conversion, see _legacy_view).
#
# Steps: 1 back up everything to retired/<date>-from-v0/backup-before-upgrade/;
# 2 convert (convert_legacy_data, pure); 3 write machine.json, saved_configs.json,
# launch_history.jsonl, lab_info.json; 4 move every retired original into
# retired/<date>-from-v0/ + README.txt; 5 delete stale locks; 6 record the run +
# queue the one-time banner; 7 drop the backup copy (the moved originals are the
# backup from then on).
# ===========================================================================

MIGRATION_ID = "v0_to_v1"
UPGRADE_NOTICE_ID = "upgrade_v1"
UPGRADE_NOTICE_TEXT = ("Settings upgraded to the new format. The old files were moved "
                       "to data/retired/ (safe to delete once the launcher works).")
LAB_INFO_UPGRADE_NOTICE_TEXT = (
    "lab_info.json was in the old format and has been upgraded. The old copy is in "
    "data/retired/ (safe to delete once the launcher works).")
RETIRED_README_TEXT = """These are the old-format files from before the oTree Lab Launcher
1.5.0 settings upgrade ({date}).

They are safe to delete once the launcher works.

To go back to an older launcher version: copy these files back into data/
and remove data/machine.json, data/saved_configs.json and
data/launch_history.jsonl.

Files in this folder:
{files}
"""
BACKUP_DIRNAME = "backup-before-upgrade"
_LEGACY_DATA_FILES = (PRESETS_FILENAME, LAB_MARKER_FILENAME, LEGACY_UI_PREFS_FILENAME,
                      SESSIONS_FILENAME)
# Old shipped files that lived in data/ (moved to app/assets/ in 1.5.0) and
# leftovers of old saves: retired with the rest when still present.
_LEGACY_LEFTOVER_FILES = ("README.md", LAB_INFO_EXAMPLE_FILENAME, "ui_prefs.json.tmp")
_LEGACY_LEFTOVER_PATTERNS = (r"^presets\.json\.broken-.*$", r"^\.presets-.*\.tmp$",
                             r"^\.lab_info-.*\.tmp$", r"^lab_info\.json\..*\.corrupt$")


def _paths_for(folder=None):
    """The data files of ``folder`` (the default data folder honours the
    OTREE_LAB_* path overrides)."""
    if folder is None or os.path.abspath(folder) == os.path.abspath(data_dir()):
        return {"folder": data_dir(), "lab_info": lab_info_path(),
                "machine": machine_path(), "saved": saved_configs_path(),
                "history": launch_history_path()}
    return {"folder": folder, "lab_info": os.path.join(folder, LAB_INFO_FILENAME),
            "machine": os.path.join(folder, MACHINE_FILENAME),
            "saved": os.path.join(folder, SAVED_CONFIGS_FILENAME),
            "history": os.path.join(folder, LAUNCH_HISTORY_FILENAME)}


def newer_schema_files(folder=None):
    """``[(file name, version)]`` for data files a NEWER launcher wrote."""
    out = []
    paths = _paths_for(folder)
    for key in ("lab_info", "machine", "saved"):
        status, data = _read_json(paths[key])
        if status == "ok" and schema_version_of(data) > SCHEMA_VERSION:
            out.append((os.path.basename(paths[key]), schema_version_of(data)))
    return out


def migration_done(folder=None):
    """True when machine.json records a finished v0 -> v1 migration."""
    status, data = _read_json(_paths_for(folder)["machine"])
    return (status == "ok" and isinstance(data, dict)
            and MIGRATION_ID in (data.get("migrations") or {}))


def _legacy_leftovers(folder):
    """Top-level names in ``folder`` that belong to the old layout."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out = []
    for name in names:
        full = os.path.join(folder, name)
        if name in _LEGACY_DATA_FILES or name in _LEGACY_LEFTOVER_FILES:
            out.append(name)
        elif name == LEGACY_MAPS_DIRNAME and os.path.isdir(full):
            out.append(name)
        elif any(re.match(p, name) for p in _LEGACY_LEFTOVER_PATTERNS):
            out.append(name)
    return sorted(out)


def _is_v0_lab_info_file(path):
    status, data = _read_json(path)
    return (status == "ok" and isinstance(data, dict) and schema_version_of(data) == 0
            and _looks_v0_lab_info(data))


def _legacy_source_present(folder, lab_info_file=None):
    """True when ``folder`` holds old-layout data worth converting."""
    lab_file = lab_info_file or os.path.join(folder, LAB_INFO_FILENAME)
    if _is_v0_lab_info_file(lab_file):
        return True
    for name in _LEGACY_DATA_FILES:
        if os.path.isfile(os.path.join(folder, name)):
            return True
    maps = os.path.join(folder, LEGACY_MAPS_DIRNAME)
    try:
        return any(f.endswith(".json") for f in os.listdir(maps))
    except OSError:
        return False


def _pending_backup(folder):
    """``(retired_dir, backup_dir)`` of an interrupted migration, or (None, None)."""
    root = os.path.join(folder, RETIRED_DIRNAME)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None, None
    for name in names:
        backup = os.path.join(root, name, BACKUP_DIRNAME)
        if "-from-v0" in name and os.path.isdir(backup):
            return os.path.join(root, name), backup
    return None, None


def _new_retired_dir(folder, date, tag):
    base = os.path.join(folder, RETIRED_DIRNAME, "%s-%s" % (date, tag))
    candidate, n = base, 2
    while os.path.exists(candidate):
        candidate = "%s-%d" % (base, n)
        n += 1
    os.makedirs(candidate)
    return candidate


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _map_lookup(folder, info=None):
    """A ``name -> map dict`` resolver for converting old files: an inline
    ``maps`` table in ``info``, then <folder>/maps/<name>.json, then the shipped
    examples."""
    table = (info or {}).get("maps") if isinstance((info or {}).get("maps"), dict) else {}

    def lookup(name):
        name = str(name or "").strip()
        if not name:
            return None
        if isinstance(table.get(name), dict):
            return table[name]
        if folder:
            found = _read_map_file(os.path.join(folder, LEGACY_MAPS_DIRNAME), name)
            if found is not None:
                return found
        return _read_map_file(maps_dir(), name)
    return lookup


def lab_info_from_v0(data, folder=None, maps_lookup=None):
    """Convert ONE old-layout lab_info dict to schema 1 (file-only: no
    presets.json overrides, no machine data). Labs become a list, each named map
    is copied into the ``maps`` table, ``admin`` becomes ``default_admin``;
    ``default_lab`` and the ``database`` block are dropped (they are per PC)."""
    data = dict(data or {})
    lookup = maps_lookup or _map_lookup(folder, data)
    raw_labs = data.get("labs") or {}
    if isinstance(raw_labs, dict):
        raw_labs = [dict(v or {}, id=k) for k, v in raw_labs.items()]
    labs, table = [], {}
    for raw in raw_labs:
        if not isinstance(raw, dict):
            continue
        entry = normalize_stored_lab(raw)
        if not entry["id"]:
            continue
        field = raw.get("map")
        if isinstance(field, dict):
            name = _unique_key(entry["id"], table)
            table[name] = field
            entry["map"] = name
        elif entry["map"]:
            obj = lookup(entry["map"])
            if isinstance(obj, dict):
                table[entry["map"]] = obj
            else:
                entry["map"] = ""
        labs.append(entry)
    out = {k: v for k, v in data.items()
           if k not in ("labs", "maps", "default_lab", "database", "admin", "_comment",
                        "schema_version")}
    out["labs"] = labs
    out["maps"] = table
    admin = data.get("admin") if isinstance(data.get("admin"), dict) else {}
    out["default_admin"] = data.get("default_admin") or admin
    return normalize_lab_info(out)


def convert_legacy_data(source, now=None, pc=None):
    """PURE conversion of an old-layout data folder ``source`` (read only).

    Returns ``{"lab_info": dict or None, "machine": dict, "saved": dict,
    "report": dict}``. The report holds counts and names only, never a password.
    ``now`` (a datetime) and ``pc`` (a created_on stamp) make it deterministic in
    tests. See _ai/storage_redesign_plan.md for the key-by-key mapping."""
    now = now or _dt.datetime.now()
    when = now.replace(microsecond=0).isoformat()
    report = {"labs": {}, "maps": {}, "home_lab": {}, "theme": {}, "github": "",
              "databases": {}, "default_database": "", "suggested_database": {},
              "configs": {}, "researchers": 0, "dropped_keys": []}

    _s, lab_raw = _read_json(os.path.join(source, LAB_INFO_FILENAME))
    lab_raw = lab_raw if isinstance(lab_raw, dict) else None
    _s, presets_raw = _read_json(os.path.join(source, PRESETS_FILENAME))
    _s, prefs = _read_json(os.path.join(source, LEGACY_UI_PREFS_FILENAME))
    prefs = prefs if isinstance(prefs, dict) else {}
    marker = str(_read_text(os.path.join(source, LAB_MARKER_FILENAME)) or "").strip().lower()
    store = presets_raw if isinstance(presets_raw, dict) else {}
    lookup = _map_lookup(source, lab_raw)

    # -- labs: lab_info labs, overridden by the presets.json lab_presets (which
    #    carry every Lab Settings edit and the per-lab display flags) --------------
    if lab_raw is not None and schema_version_of(lab_raw) >= 1:
        base_labs = {l["id"]: l for l in normalize_lab_info(lab_raw)["labs"]}
        base_maps = lab_info_maps(lab_raw)
        base_display = {}
    else:
        raw = (lab_raw or {}).get("labs") or {}
        if isinstance(raw, list):
            raw = {str(x.get("id", "")): x for x in raw if isinstance(x, dict)}
        base_labs, base_display = {}, {}
        for lab_id, entry in raw.items():
            entry = entry or {}
            base_labs[str(lab_id)] = dict(entry, id=str(lab_id))
            base_display[str(lab_id)] = bool(entry.get("display", True))
        base_maps = {}
    overrides = [p for p in (store.get("lab_presets") or []) if isinstance(p, dict)
                 and str(p.get("id", "")).strip()]
    over_by_id = {str(p["id"]).strip(): p for p in overrides}
    order = [str(p["id"]).strip() for p in overrides]
    order += [lab_id for lab_id in base_labs if lab_id not in over_by_id]
    maps = dict(base_maps)
    labs, display = [], {}
    snapshot_used, missing_maps, renamed = [], [], []
    map_files = {}
    try:
        for name in os.listdir(os.path.join(source, LEGACY_MAPS_DIRNAME)):
            if name.endswith(".json"):
                obj = _read_map_file(os.path.join(source, LEGACY_MAPS_DIRNAME), name[:-5])
                if obj is not None:
                    map_files[name[:-5]] = obj
    except OSError:
        pass
    for lab_id in order:
        if not lab_id or any(l["id"] == lab_id for l in labs):
            continue
        base = base_labs.get(lab_id, {})
        over = over_by_id.get(lab_id)
        merged = dict(base)
        if over is not None:
            merged.update({
                "name": over.get("name") or base.get("name") or lab_id,
                "host": over.get("ip", base.get("host", "")),
                "seats": over.get("seats", base.get("seats", [])),
                "geometry": over.get("geometry", base.get("geometry")),
                "cols": over.get("cols", base.get("cols", 0)),
                "default_room": over.get("default_room") or base.get("default_room"),
                "shortcut_label": over.get("shortcut_label", base.get("shortcut_label", "")),
                "deleted": bool(over.get("deleted", base.get("deleted", False))),
            })
            if base.get("name") and merged["name"] != base.get("name"):
                renamed.append(lab_id)
        entry = normalize_stored_lab(dict(merged, id=lab_id, map=""))
        name = base.get("map") if isinstance(base.get("map"), str) else ""
        name = str(name or "").strip()
        snapshot = over.get("map") if over is not None and isinstance(over.get("map"), dict) \
            else (base.get("map") if isinstance(base.get("map"), dict) else None)
        if name:
            found = maps.get(name) if isinstance(maps.get(name), dict) else lookup(name)
            if snapshot is not None and snapshot != found:
                maps[name] = snapshot
                snapshot_used.append(name)
            elif found is not None:
                maps[name] = found
            else:
                missing_maps.append(name)
                name = ""
        elif snapshot is not None:
            same = [n for n, obj in list(maps.items()) + list(map_files.items()) if obj == snapshot]
            name = same[0] if same else _unique_key(lab_id, maps)
            maps[name] = snapshot
        entry["map"] = name
        labs.append(entry)
        display[lab_id] = bool(over.get("display", True)) if over is not None \
            else base_display.get(lab_id, True)
    # Every map file of the old maps/ folder is kept (the shipped examples only
    # when a lab uses them), so no hand-made map is lost.
    for name, obj in map_files.items():
        if name not in maps and not name.startswith("example_"):
            maps[name] = obj
    lab_ids = [l["id"] for l in labs]
    live_ids = [l["id"] for l in labs if not l["deleted"]]
    report["labs"] = {"total": len(labs), "from_saved_settings": len(overrides),
                      "lab_info_only": len([i for i in lab_ids if i not in over_by_id]),
                      "deleted": len(lab_ids) - len(live_ids), "renamed": renamed,
                      "rooms": {l["id"]: l["default_room"] for l in labs}}
    report["maps"] = {"inlined": sorted(maps), "snapshot_used": snapshot_used,
                      "missing": missing_maps}

    # -- this PC: home lab, shown labs, theme --------------------------------
    if marker and marker in lab_ids:
        home, source_note = marker, "lab.local"
    elif marker:
        home, source_note = "", "lab.local names an unknown lab (%s); the wizard will ask" % marker
    else:
        home, source_note = "", "no lab.local; the setup wizard will ask"
    report["home_lab"] = {"value": home, "source": source_note}
    shown = [i for i in live_ids if display.get(i, True)]
    if home and home not in shown:
        shown.append(home)
    theme_source = "ui_prefs.json" if "theme" in prefs else "default"
    report["theme"] = {"value": normalize_theme(prefs.get("theme")), "source": theme_source}
    stamp = dict(pc or current_pc_stamp(home_lab=home, when=when))
    stamp["migrated"] = True

    # -- this PC's databases -------------------------------------------------
    dbs = []
    for raw in store.get("databases") or []:
        if isinstance(raw, dict) and str(raw.get("id", "")).strip():
            entry = normalize_database_entry(raw)
            if not entry["created_on"]:
                entry["created_on"] = normalize_created_on(
                    dict(stamp, when=entry["created"] or stamp.get("when", "")))
            dbs.append(entry)
    report["databases"]["from_saved_settings"] = len(dbs)
    lab_db_id = ""
    block = (lab_raw or {}).get("database") if lab_raw is not None else None
    if isinstance(block, dict) and str(block.get("db_name", "") or "").strip():
        conn = {"db_name": str(block.get("db_name", "")).strip(),
                "db_user": str(block.get("db_user", "") or "").strip(),
                "db_password": str(block.get("db_password", "") or ""),
                "db_host": str(block.get("db_host", "") or "").strip() or "localhost",
                "db_port": str(block.get("db_port", "") or "").strip() or "5432"}
        match = find_database_by_connection(conn, dbs)
        if match is not None:
            lab_db_id = match["id"]
            report["databases"]["lab_database"] = "matched an existing entry (%s)" % lab_db_id
        else:
            entry = normalize_database_entry(dict(
                conn, id=_unique_db_id(conn["db_name"], [d["id"] for d in dbs]),
                title=conn["db_name"], postgres_user=conn["db_user"], created="",
                created_on=stamp))
            dbs.append(entry)
            lab_db_id = entry["id"]
            report["databases"]["lab_database"] = "added as a normal database (%s)" % lab_db_id
        if home:
            for lab in labs:
                if lab["id"] == home and "suggested_database" not in lab:
                    lab["suggested_database"] = {"db_name": conn["db_name"],
                                                 "db_user": conn["db_user"]}
                    report["suggested_database"][home] = conn["db_name"]
    else:
        report["databases"]["lab_database"] = "none in the old lab_info.json"
    live_db_ids = [d["id"] for d in dbs if not d["deleted"]]
    old_default = str(store.get("default_database", "") or "").strip()
    if old_default and old_default != DB_BUILTIN_LAB and old_default in live_db_ids:
        default_id = old_default
    else:
        default_id = lab_db_id
    report["default_database"] = default_id

    # -- configs --------------------------------------------------------------
    raw_configs = presets_raw if isinstance(presets_raw, list) else store.get("presets", [])
    counts = {"total": 0, "sqlite": 0, "default": 0, "matched": 0, "created": 0,
              "kept_id": 0, "password_differs": 0, "builtin_dropped": 0}
    configs = []
    for index, item in enumerate(raw_configs if isinstance(raw_configs, list) else []):
        if not isinstance(item, dict):
            continue
        if is_builtin(item):
            counts["builtin_dropped"] += 1
            continue
        cfg = {k: v for k, v in item.items() if k not in CONFIG_DB_FIELDS}
        if not str(cfg.get("name", "")).strip():
            cfg["name"] = "Unnamed config %d" % (index + 1)
        mode = str(item.get("db_mode", DB_MODE_LAB) or DB_MODE_LAB)
        kept = str(item.get("database_id", "") or "").strip()
        conn = {"db_name": str(item.get("db_name", "") or "").strip(),
                "db_user": str(item.get("db_user", "") or "").strip(),
                "db_password": str(item.get("db_password", "") or ""),
                "db_host": str(item.get("db_host", "") or "").strip() or "localhost",
                "db_port": str(item.get("db_port", "") or "").strip() or "5432"}
        if kept:
            cfg["database_id"] = kept
            counts["kept_id"] += 1
        elif mode == DB_MODE_NONE:
            cfg["database_id"] = DB_BUILTIN_SQLITE
            counts["sqlite"] += 1
        elif mode == DB_MODE_LAB and default_id:
            cfg["database_id"] = default_id
            counts["default"] += 1
        elif conn["db_name"]:
            match = find_database_by_connection(conn, dbs)
            if match is not None:
                cfg["database_id"] = match["id"]
                counts["matched"] += 1
                if match.get("db_password", "") != conn["db_password"]:
                    counts["password_differs"] += 1
            else:
                entry = normalize_database_entry(dict(
                    conn, id=_unique_db_id(conn["db_name"], [d["id"] for d in dbs]),
                    title=conn["db_name"], postgres_user=conn["db_user"],
                    researcher=str(item.get("author", "") or ""), created="",
                    created_on=stamp))
                dbs.append(entry)
                cfg["database_id"] = entry["id"]
                counts["created"] += 1
        else:
            cfg["database_id"] = DB_BUILTIN_SQLITE
            counts["sqlite"] += 1
        configs.append(cfg)
    counts["total"] = len(configs)
    report["configs"] = counts
    report["databases"]["total"] = len(dbs)
    report["databases"]["deleted"] = len([d for d in dbs if d["deleted"]])
    if not default_id:
        live = [d["id"] for d in dbs if not d["deleted"]]
        report["default_database"] = ""
        if live:
            report["databases"]["note"] = "no default database (the old install had none)"

    researchers = [str(r) for r in (store.get("researchers") or []) if str(r).strip()]
    report["researchers"] = len(researchers)
    saved = {"schema_version": SCHEMA_VERSION, "configs": configs}
    if "researchers" in store:
        saved["researchers"] = researchers
    if "last_author" in store:
        saved["last_author"] = str(store.get("last_author") or "")
    dropped = []
    for key, value in store.items():
        if key in ("presets", "lab_presets", "pg_admin", "databases", "default_database",
                   "researchers", "last_author", "version", "schema_version"):
            continue
        if key == "last_project":
            dropped.append(key)
            continue
        saved[key] = value

    machine = default_machine()
    machine.update({"home_lab": home, "shown_labs": shown,
                    "theme": normalize_theme(prefs.get("theme")),
                    "databases": dbs, "default_database": default_id})
    if isinstance(store.get("pg_admin"), dict) and store["pg_admin"]:
        machine["pg_admin"] = {k: str(store["pg_admin"].get(k, "") or "") for k in PG_ADMIN_KEYS}
    report["pg_admin"] = bool(machine["pg_admin"])

    lab_info = None
    if labs or lab_raw is not None:
        info = {}
        if lab_raw is not None:
            info = {k: v for k, v in lab_raw.items()
                    if k not in ("labs", "maps", "default_lab", "database", "admin",
                                 "_comment", "schema_version")}
            for key in ("default_lab", "database", "_comment"):
                if key in lab_raw:
                    dropped.append("lab_info.%s" % key)
            admin = lab_raw.get("default_admin") or lab_raw.get("admin") or {}
            info["default_admin"] = admin if isinstance(admin, dict) else {}
        moved = []
        for key in _GITHUB_SYNC_KEYS:
            if key in prefs and key not in info:
                info[key] = prefs[key]
                moved.append(key)
        report["github"] = ("moved from ui_prefs.json" if moved else "unchanged")
        info["labs"] = labs
        info["maps"] = maps
        lab_info = normalize_lab_info(info)
    report["dropped_keys"] = sorted(set(dropped))
    return {"lab_info": lab_info, "machine": normalize_machine(machine),
            "saved": saved, "report": report}


_LEGACY_CACHE = {"key": None, "value": None}


def _legacy_view(folder=None):
    """FAIL-SOFT fallback: an in-memory conversion of the old files while the
    migration has not (successfully) run, or None. Nothing is written."""
    try:
        folder = folder or data_dir()
        if migration_done(folder):
            return None
        _retired, backup = _pending_backup(folder)
        source = backup or folder
        if not _legacy_source_present(source):
            return None
        stamps = []
        for name in (LAB_INFO_FILENAME,) + _LEGACY_DATA_FILES:
            try:
                stamps.append(os.path.getmtime(os.path.join(source, name)))
            except OSError:
                stamps.append(None)
        key = (os.path.abspath(source), tuple(stamps))
        if _LEGACY_CACHE["key"] != key:
            _LEGACY_CACHE["value"] = convert_legacy_data(source)
            _LEGACY_CACHE["key"] = key
        return _LEGACY_CACHE["value"]
    except Exception:
        return None


def _backup_folder(folder, backup_dir):
    """Copy every entry of ``folder`` (except locks/ and retired/) to backup_dir."""
    os.makedirs(backup_dir, exist_ok=True)
    for name in os.listdir(folder):
        if name in (LOCKS_DIRNAME, RETIRED_DIRNAME):
            continue
        src = os.path.join(folder, name)
        dst = os.path.join(backup_dir, name)
        if os.path.isdir(src):
            if not os.path.exists(dst):
                shutil.copytree(src, dst)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)


def _delete_stale_locks(folder):
    """Remove leftover lock files (locks/*.lock and old presets.json.lock). Only
    called by the migration, under its own lock. Returns how many were removed."""
    removed = 0
    targets = []
    locks = os.path.join(folder, LOCKS_DIRNAME)
    try:
        targets += [os.path.join(locks, n) for n in os.listdir(locks)
                    if n.endswith(".lock") and n != "migration.lock"]
    except OSError:
        pass
    old = os.path.join(folder, PRESETS_FILENAME + ".lock")
    if os.path.isfile(old):
        targets.append(old)
    for path in targets:
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def _write_text_atomic(path, text):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder,
                                         prefix=".history-", suffix=".tmp", delete=False)
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except Exception:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _merge_history(source_file, target_file):
    """launch_history.jsonl = the old sessions.jsonl lines, then any lines already
    in the target that are not among them. Returns the number of lines."""
    old = [l for l in (_read_text(source_file) or "").splitlines() if l.strip()]
    existing = [l for l in (_read_text(target_file) or "").splitlines() if l.strip()]
    seen = set(old)
    lines = old + [l for l in existing if l not in seen]
    if lines:
        _write_text_atomic(target_file, "\n".join(lines) + "\n")
    return len(lines)


def _add_notice(machine, notice_id, text):
    notices = machine.setdefault("notices", [])
    if not any(n.get("id") == notice_id and not n.get("shown") for n in notices):
        notices.append({"id": notice_id, "text": text, "shown": False})


def migrate_data_folder(folder=None, now=None, pc=None):
    """Upgrade an old (v0) data folder to schema 1. See the section comment.

    Returns ``{"ok", "migrated", "newer_schema", "message", "report",
    "retired_dir", "moved", "locks_removed"}``. Never raises."""
    now = now or _dt.datetime.now()
    result = {"ok": True, "migrated": False, "newer_schema": False, "message": "",
              "report": None, "retired_dir": "", "moved": [], "locks_removed": 0}
    paths = _paths_for(folder)
    folder = paths["folder"]
    try:
        newer = newer_schema_files(folder)
        if newer:
            result.update(ok=False, newer_schema=True, message=str(
                NewerSchemaError(os.path.join(folder, newer[0][0]), newer[0][1])))
            return result
        if not os.path.isdir(folder):
            return result
        with exclusive_file_lock(lock_path("migration", folder)):
            if migration_done(folder):
                _finish_leftovers(folder, paths, now, result)
                return result
            retired_dir, backup_dir = _pending_backup(folder)
            if backup_dir is None and not _legacy_source_present(folder, paths["lab_info"]):
                return result
            _full_migration(folder, paths, now, pc, retired_dir, backup_dir, result)
    except NewerSchemaError as error:
        result.update(ok=False, newer_schema=True, message=str(error))
    except Exception as error:   # fail-soft: never crash the app
        result.update(ok=False, message=(
            "Could not upgrade the settings to the new format (%s: %s). The old "
            "files were left in place and are still used." % (type(error).__name__, error)))
    _LEGACY_CACHE.update(key=None, value=None)
    return result


def _full_migration(folder, paths, now, pc, retired_dir, backup_dir, result):
    date = now.strftime("%Y-%m-%d")
    if backup_dir is None:
        retired_dir = _new_retired_dir(folder, date, "from-v0")
        backup_dir = os.path.join(retired_dir, BACKUP_DIRNAME)
        # The lab_info file may be an override path outside the folder.
        _backup_folder(folder, backup_dir)
        if os.path.dirname(os.path.abspath(paths["lab_info"])) != os.path.abspath(folder) \
                and os.path.isfile(paths["lab_info"]):
            shutil.copy2(paths["lab_info"], os.path.join(backup_dir, LAB_INFO_FILENAME))
    conv = convert_legacy_data(backup_dir, now=now, pc=pc)
    result["report"] = conv["report"]
    result["retired_dir"] = retired_dir
    # 3. the new files (machine.json WITHOUT the done-marker until the end)
    machine = conv["machine"]
    with exclusive_file_lock(lock_path("saved_configs", folder)):
        write_json_atomic(paths["saved"], conv["saved"], prefix=".saved_configs-")
    _merge_history(os.path.join(backup_dir, SESSIONS_FILENAME), paths["history"])
    save_machine(machine, paths["machine"])
    lab_was_v0 = _is_v0_lab_info_file(os.path.join(backup_dir, LAB_INFO_FILENAME))
    if conv["lab_info"] is not None:
        save_lab_info(conv["lab_info"], paths["lab_info"])
    # 4. retire the originals
    moved = []
    for name in _legacy_leftovers(folder):
        target = os.path.join(retired_dir, name)
        if os.path.exists(target):
            target = os.path.join(retired_dir, "%s.%s" % (name, now.strftime("%H%M%S")))
        shutil.move(os.path.join(folder, name), target)
        moved.append(name)
    old_lab = os.path.join(backup_dir, LAB_INFO_FILENAME)
    if lab_was_v0 and not os.path.exists(os.path.join(retired_dir, LAB_INFO_FILENAME)):
        shutil.copy2(old_lab, os.path.join(retired_dir, LAB_INFO_FILENAME))
        moved.append(LAB_INFO_FILENAME)
    result["locks_removed"] = _delete_stale_locks(folder)
    files = sorted(n for n in os.listdir(retired_dir) if n != BACKUP_DIRNAME)
    with open(os.path.join(retired_dir, "README.txt"), "w", encoding="utf-8") as handle:
        handle.write(RETIRED_README_TEXT.format(
            date=date, files="\n".join("  " + f for f in files)))
    # 6. record the run + the one-time banner
    rel = os.path.relpath(retired_dir, folder).replace("\\", "/")

    def _mark(m):
        m.setdefault("migrations", {})[MIGRATION_ID] = {
            "when": now.replace(microsecond=0).isoformat(), "retired": rel}
        _add_notice(m, UPGRADE_NOTICE_ID, UPGRADE_NOTICE_TEXT)

    update_machine(_mark, paths["machine"])
    # 7. the moved originals are the backup from now on
    shutil.rmtree(backup_dir, ignore_errors=True)
    result.update(migrated=True, moved=sorted(moved),
                  message=UPGRADE_NOTICE_TEXT)


def _finish_leftovers(folder, paths, now, result):
    """After a finished migration: drop a leftover backup, retire any old file
    that reappeared, and upgrade an OLD lab_info.json copied in from another PC
    (file-only; its database block becomes this PC's suggested database)."""
    retired_dir, backup_dir = _pending_backup(folder)
    if backup_dir is not None:
        shutil.rmtree(backup_dir, ignore_errors=True)
    if not _is_v0_lab_info_file(paths["lab_info"]):
        return
    date = now.strftime("%Y-%m-%d")
    retired_dir = _new_retired_dir(folder, date, "lab_info-from-v0")
    shutil.copy2(paths["lab_info"], os.path.join(retired_dir, LAB_INFO_FILENAME))
    old = load_lab_info_raw(paths["lab_info"])
    info = lab_info_from_v0(old, folder)
    home = str(load_machine(paths["machine"]).get("home_lab") or "")
    block = old.get("database") if isinstance(old.get("database"), dict) else {}
    if home and str(block.get("db_name", "") or "").strip():
        for lab in info["labs"]:
            if lab["id"] == home and "suggested_database" not in lab:
                lab["suggested_database"] = {"db_name": str(block["db_name"]).strip(),
                                             "db_user": str(block.get("db_user", "") or "")}
    save_lab_info(info, paths["lab_info"])
    with open(os.path.join(retired_dir, "README.txt"), "w", encoding="utf-8") as handle:
        handle.write(RETIRED_README_TEXT.format(date=date, files="  " + LAB_INFO_FILENAME))
    update_machine(lambda m: _add_notice(m, "lab_info_v0_" + date,
                                         LAB_INFO_UPGRADE_NOTICE_TEXT), paths["machine"])
    result.update(migrated=True, retired_dir=retired_dir, moved=[LAB_INFO_FILENAME],
                  message=LAB_INFO_UPGRADE_NOTICE_TEXT)


def prepare_storage(folder=None):
    """The startup step both faces and the headless run call BEFORE any UI:
    migrate an old data folder (fail-soft) and load the live state. Returns the
    migrate_data_folder result; ``newer_schema`` True means the app must not
    continue (a newer launcher wrote this data folder)."""
    result = migrate_data_folder(folder)
    try:
        reload_lab_info()
    except Exception:
        pass
    return result


def take_notices():
    """The one-time banners not shown yet (e.g. "Settings upgraded ..."), each
    marked shown so it appears exactly once. Returns a list of texts. Fail-soft."""
    texts = []

    def _apply(machine):
        for notice in machine.get("notices") or []:
            if not notice.get("shown"):
                texts.append(str(notice.get("text") or ""))
                notice["shown"] = True
    try:
        if any(not n.get("shown") for n in (load_machine().get("notices") or [])):
            update_machine(_apply)
    except Exception:
        return texts
    return [t for t in texts if t]


# ===========================================================================
# UI WORDING + SUMMARIES shared by both faces (UX simplification, 2026-10-01).
#
# Rule both faces follow: a sentence that EXPLAINS goes behind an info tip (web
# ``infoBadge``, Tk ``info_tip``); a sentence that tells the user to ACT stays
# visible. The tip texts live here so both faces say the same words. Set-once
# sections (Settings cards, the oTree admin row) collapse to ONE summary line
# built by the helpers below, and expand only to edit.
# ===========================================================================

# The ONE name of the action that appends the lab support block to settings.py,
# wherever it is offered (warning strip, get-ready popup, pre-launch screen,
# block viewer).
GET_READY_LABEL = "Get ready for the lab"

# Help for the per-lab "Default oTree room" field (wizard + Lab Settings).
ROOM_HELP = ("If the lab PCs already have shortcuts that open a room, put that room "
             "name here so participants land in it; otherwise leave it as study.")

UI_TIPS = {
    "this_computer": "Which lab this computer is in (saved on this computer). The "
                     "main screen then shows that lab and uses its default room.",
    "labs": "Ticked labs appear on the main screen; with one ticked it is selected "
            "automatically. Labs are stored in lab_info.json, the one file you copy "
            "to every computer of the lab.",
    "databases": "Every database this computer uses (saved in machine.json, never "
                 "copied to another PC). The default one is what the Lab default "
                 "config uses.",
    "localhost_only": "On: every database is on this computer. Off: a database on "
                      "another computer can be registered (it must already exist "
                      "there).",
    "postgres_admin": "The Postgres superuser on this computer. Only used to create "
                      "databases, never to launch.",
    "save_as": "Saved configs are never changed. This adds a new one and leaves "
               "every existing config exactly as it is.",
    "edit_database": "Connection details for this database on this computer, used "
                     "by every config that references it. Saved in machine.json.",
    "create_database": "Created with the Postgres admin login from Settings. With "
                       "no user of its own, that login owns the database and "
                       "connects to it.",
    "host_locked": "This computer only: “%s” is on in Settings."
                   % LOCALHOST_ONLY_LABEL,
    "db_picker": "The databases on this computer. A database on another computer "
                 "shows its host.",
    "seat_file": "Read from a file in your project at launch; the file is never "
                 "changed.",
    "localhost_run": "Runs oTree on this computer (localhost). Pair it with the oTree "
                     "default (SQLite) database for a self-contained test away from "
                     "the lab.",
    "room_picker": "The lab default room is the one the lab computers’ desktop "
                   "shortcuts open. Only change it if the computers will open a "
                   "different room’s link.",
    "export_hotkey": "Leave blank for no hotkey. Type one letter or digit, e.g. S, to "
                     "give every exported shortcut Ctrl+Alt+S. It works once the "
                     ".lnk is on the Desktop or Start Menu.",
    "shortcut_label": "The name the participant-PC desktop shortcuts are saved as; "
                      "the launch screen then names that shortcut.",
    "wizard_postgres": "The Postgres superuser on this computer. Used only to "
                       "create this computer’s databases, never to launch.",
    "wizard_databases": "The ticked database is this computer’s default: the Lab "
                        "default config uses it.",
    "welcome_link": WELCOME_NOTE,
    "pg_blank_password": PG_BLANK_PASSWORD_HINT,
    "pg_new_user_password": PG_NEW_USER_PASSWORD_HINT,
    "room_help": ROOM_HELP,
    "production_mode": "Serve as a real study, no debug pages.",
    "reset_db": "Runs otree resetdb before the server starts: the database is "
                "emptied and rebuilt, so the session starts clean. Untick to keep "
                "the data already in it.",
    "one_click_shortcut": "Starts this config from one desktop shortcut and opens "
                          "the oTree monitor straight away. Save the config first: "
                          "the shortcut runs a saved config by name.",
    "github": "Lets an experimenter download a study from your lab’s GitHub "
              "organisation and keep it up to date, without a terminal. Type the "
              "organisation name to switch it on; empty is off. Each computer "
              "needs a GitHub login once.",
    "github_login": GITHUB_LOGIN_DIALOG_NOTE,
    "clone": "Downloads the repository into a new folder inside “Saves to” and "
             "selects it as your study folder. Type its name, or paste a full "
             "GitHub link.",
    "wizard_no_labs": "No labs yet: add your lab here; with several, the selected one "
                      "is the lab this computer is in. Or copy a lab_info.json from "
                      "another computer of the lab into data/ and restart.",
}


def ui_tip(key):
    """The info-tip text for ``key`` ("" for an unknown key)."""
    return UI_TIPS.get(key, "")


# The project folder on the main page (2026-10-08): the folder NAME is what the
# user recognises, so it is shown prominently; the parent folder is a short
# muted hint (cut to about this many characters, root + final segments kept);
# the full path is the hover tip. Both faces render project_path_display; the
# web page keeps a JS mirror (projectPathDisplay) that a test holds equal.
PROJECT_PARENT_MAX = 36


def project_path_parts(path):
    """Split a project folder path into ``(name, parent)``: the final folder
    name and the folder it sits in, for either OS's separator. An empty path
    gives ``("", "")``; a bare name has an empty parent; the parent of a
    folder directly under a root keeps that root ("C:\\\\" / "/")."""
    cleaned = str(path or "").strip().rstrip("\\/")
    if not cleaned:
        return "", ""
    sep_at = max(cleaned.rfind("\\"), cleaned.rfind("/"))
    if sep_at < 0:
        return cleaned, ""
    name = cleaned[sep_at + 1:]
    parent = cleaned[:sep_at]
    if not parent or parent.endswith(":"):
        parent = cleaned[:sep_at + 1]
    return name, parent


def shorten_parent(parent, limit=PROJECT_PARENT_MAX):
    """A parent folder path cut to about ``limit`` characters, keeping the
    root (drive letter or "/") and as many FINAL segments as fit:
    "C:\\\\Users\\\\julian\\\\Desktop\\\\TAIT" -> "C:\\\\…\\\\Desktop\\\\TAIT". The last
    segment is always kept, so the hint still says where the folder is."""
    text = str(parent or "")
    if len(text) <= limit:
        return text
    sep = "\\" if ("\\" in text and "/" not in text) else "/"
    parts = re.split(r"[\\/]", text)
    head = parts[0]                       # "C:" or "" for a unix root
    rest = [part for part in parts[1:] if part]
    tail = []
    while rest:
        segment = rest.pop()
        candidate = sep.join([head, "\u2026", segment] + tail)
        if len(candidate) > limit and tail:
            break
        tail.insert(0, segment)
    return sep.join([head, "\u2026"] + tail)


def project_path_display(path, limit=PROJECT_PARENT_MAX):
    """What the main page shows for the chosen project folder: ``name`` (bold),
    ``parent_short`` (the muted hint), ``parent`` and ``full`` (the tooltip).
    Empty ``name`` = no folder chosen = nothing is shown."""
    name, parent = project_path_parts(path)
    return {"name": name, "parent": parent, "parent_short": shorten_parent(parent, limit),
            "full": str(path or "").strip()}


# Main page pass 2 (2026-10-08, Julian's decisions on MAIN_PAGE_UI_SUGGESTIONS):
# the header names the config (no "Config:" prefix) with a muted "saved by"
# subline and an unsaved marker; Quit launcher moved from the launch bar into
# Lab Settings; the empty project state leads with one bold sentence.
QUIT_LAUNCHER_LABEL = "Quit launcher"
QUIT_LAUNCHER_NOTE = ("Shuts down the launcher. A study you already launched keeps "
                      "running; the launcher never stops it.")
UNSAVED_CHANGES_TEXT = "Unsaved changes"
UNSAVED_SETTINGS_TITLE = "Unsaved settings"


def format_saved_date(stamp):
    """A config's ``created`` stamp as "15 Sep 2026" ("" when unknown)."""
    if not stamp:
        return ""
    try:
        when = _dt.datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    return "%d %s" % (when.day, when.strftime("%b %Y"))


def saved_by_line(preset):
    """"saved by J. Tait · 15 Sep 2026" for a researcher config: who saved it and
    when (either part alone when the other is unknown); "" for the built-in Lab
    default (a template, never saved) or when nothing is known."""
    preset = preset or {}
    if is_builtin(preset):
        return ""
    author = str(preset.get("author", "") or "").strip()
    date = format_saved_date(preset.get("created"))
    if author and date:
        return "saved by %s · %s" % (author, date)
    if author:
        return "saved by %s" % author
    if date:
        return "saved %s" % date
    return ""


def config_title(preset):
    """The header title: the config's display name, or "Unsaved settings" for a
    scratch setup that is not a saved config (``preset`` None)."""
    return display_name(preset) if preset else UNSAVED_SETTINGS_TITLE


def config_subline(preset, dirty):
    """The muted line under the header title: "Unsaved changes · saved by A ·
    D" while edited, "Saved by A · D" when clean, "Unsaved changes" for an
    edited scratch setup, "" when there is nothing to say (the built-in Lab
    default, unedited). The web page mirrors this in JS (configSubline)."""
    saved = saved_by_line(preset) if preset else ""
    if dirty and saved:
        return UNSAVED_CHANGES_TEXT + " · " + saved
    if dirty:
        return UNSAVED_CHANGES_TEXT
    return (saved[:1].upper() + saved[1:]) if saved else ""


def split_lead_sentence(text):
    """``(lead, rest)``: the first sentence of ``text`` (up to and including its
    full stop) and whatever follows. The empty project state shows the lead in
    bold with the actions right under it. A text with one sentence gives
    ``(text, "")``."""
    text = str(text or "").strip()
    match = re.match(r"(.*?[.!?])(?:\s+(.*))?$", text, re.S)
    if not match:
        return text, ""
    return match.group(1), (match.group(2) or "").strip()


def admin_summary(config):
    """The collapsed oTree admin row: "admin · STUDY · production · auto login".
    Username, authentication level, production/debug and auto/manual login are
    set once per study, so the main screen shows this one line and the pen opens
    all four."""
    c = dict(config or {})
    level = str(c.get("auth_level") or "").strip()
    return " · ".join([
        str(c.get("admin_username") or "").strip() or "admin",
        level if level and level.lower() != "none" else "no auth level",
        "production" if c.get("production", True) else "debug",
        "auto login" if c.get("auto_login", True) else "manual login"])


def pg_admin_summary(admin):
    """The collapsed Postgres admin card: "postgres@localhost" (the port only
    when it is not 5432), or "Not set"."""
    admin = admin or {}
    user = str(admin.get("admin_username") or "").strip()
    if not user:
        return "Not set"
    host = str(admin.get("admin_host") or "").strip() or "localhost"
    port = str(admin.get("admin_port") or "").strip() or "5432"
    return "%s@%s%s" % (user, host, "" if port == "5432" else ":" + port)


def databases_summary(entries, default_id=""):
    """The collapsed Databases card: "otree_large_lab (default) + 2 more"."""
    entries = [e for e in (entries or []) if not e.get("builtin") and not e.get("deleted")]
    if not entries:
        return "None yet (launches use oTree’s own SQLite)"
    default = [e for e in entries if e.get("id") == default_id or e.get("is_default")]
    first = default[0] if default else entries[0]
    text = str(first.get("title") or first.get("db_name") or "database")
    if default:
        text += " (default)"
    others = len(entries) - 1
    if others:
        text += " + %d more" % others
    return text


def labs_summary(lab_presets):
    """The collapsed Labs card: the shown labs by name, "(1 hidden)" when any."""
    presets = [p for p in (lab_presets or []) if not p.get("deleted")]
    shown = [str(p.get("name") or p.get("id")) for p in presets if p.get("display", True)]
    hidden = len(presets) - len(shown)
    text = ", ".join(shown) if shown else "None shown"
    if hidden:
        text += " (%d hidden)" % hidden
    return text


def lab_identity_status(lab_name):
    """What "which lab is this computer" just did, including the side effect on
    the main screen (every other lab is hidden)."""
    return ("This computer is now %s. Only %s is shown on the main screen."
            % (lab_name, lab_name))


def lab_has_drawn_map(preset):
    """True when a lab has a real, DRAWN room layout (a map with cells), False
    when its "map" is only the ordered list of its seat labels in a plain grid.

    Julian's rule for the main screen's seat map (2026-10-01): a drawn layout is
    worth seeing, so it starts EXPANDED; a plain grid of seat numbers adds
    nothing over "31 seats", so it starts COLLAPSED. ("Edit for this run" opens
    it either way: there the seats are the control.) Both faces use this."""
    preset = preset or {}
    return build_seatmap_from_map(preset.get("map"), preset.get("seats") or []) is not None


def seats_caption(total, excluded=0):
    """The seat map caption: "Room layout · 31 seats", or "29 of 31 seats" when
    seats are switched off for this run."""
    total = int(total or 0)
    excluded = max(0, min(int(excluded or 0), total))
    if excluded:
        return "Room layout · %d of %d seats" % (total - excluded, total)
    return "Room layout · %d seats" % total


def env_log_line(keys, env):
    """ONE activity-log line for the environment a launch sets (passwords masked
    by describe_env_value), instead of one line per variable."""
    pairs = ["%s=%s" % (k, describe_env_value(k, env.get(k, ""))) for k in keys if k in env]
    if not pairs:
        return "Environment: nothing set for this run."
    return "Environment: " + " · ".join(pairs)


SEAT_FILE_MISSING_WARNING = ("No participant file is chosen, so this launch uses an open "
                             "room with no seat list. Choose a file, or pick another "
                             "participant list.")


def seat_file_missing_warning(config):
    """A pre-launch WARNING (never a block: seats are never a hard block) when
    "Use a file from my project" is selected but no readable file is set, so the
    silent fallback to an open room is said out loud. "" otherwise."""
    c = dict(config or {})
    if c.get("seat_mode") != SEAT_FILE:
        return ""
    path = str(c.get("seat_file") or "").strip()
    if path and os.path.isfile(path):
        return ""
    return SEAT_FILE_MISSING_WARNING


NO_BLOCK_ISSUE_TITLE = ("This project is not lab-ready yet: without the lab support "
                        "block the lab room, seat board and lab database do not take "
                        "effect.")
NO_BLOCK_ISSUE_HINT = ("Adds a clearly-marked block to the end of settings.py, after a "
                       "timestamped .bak backup (fully revertible).")


def launch_room_mismatch(cfg, saved_config=None, lab_presets=None):
    """:func:`lab_room_mismatch`, but ONLY for a saved config whose room is
    still the one it was saved with: that is the case the warning exists for (the
    lab's default room changed after the config was saved). A room the user just
    picked on screen is their choice, already stated in the launch summary, so it
    raises no warning. ``saved_config`` is the stored config the screen was loaded
    from (None for the built-in default or an unsaved setup)."""
    if not saved_config or saved_config.get("builtin"):
        return None
    saved_config = follow_lab_room(saved_config, lab_presets)
    saved_room = str(saved_config.get("room_name") or "").strip() or DEFAULT_ROOM_NAME
    room = str((cfg or {}).get("room_name") or "").strip() or DEFAULT_ROOM_NAME
    if room != saved_room:
        return None
    return lab_room_mismatch(cfg, lab_presets)


def wizard_postgres_next(admin, tester=None):
    """What "Next" on the wizard's Postgres step does. Returns
    ``{"action", "ok", "message"}``:

      no superuser typed  -> action "skip"  (same as Skip: no Postgres here)
      login works         -> action "next"  (go on to the Databases step)
      login fails         -> action "stay"  (show the message; fix it or Skip)

    so a login that cannot work is caught HERE and not three steps later at Save.
    ``tester`` replaces :func:`test_pg_admin_connection` in tests."""
    admin = wizard_pg_admin(admin)
    if not admin["admin_username"]:
        return {"action": "skip", "ok": False, "message": ""}
    result = (tester or test_pg_admin_connection)(admin)
    if result.get("ok"):
        return {"action": "next", "ok": True, "message": result.get("message", "")}
    return {"action": "stay", "ok": False,
            "message": result.get("message") or "Could not connect to Postgres."}


# ===========================================================================
# SETUP WIZARD (per PC). Runs when lab_info.json has no labs or this PC has no
# (valid) home lab. Steps: 1 which lab is this PC (from lab_info.json; with no
# labs, create them as before); 2 Postgres on THIS computer (superuser,
# password, port; host localhost; Test connection); 3 create databases (the
# default one prefilled with the lab's suggested_database), add another / link
# an existing one. The last step's button SAVES to machine.json (there is no
# separate review step). Step 2 may be skipped: step 3 is then skipped too (a
# database cannot be created without a Postgres login) and the wizard saves at
# once; step 3 may also be skipped on its own. Launches then use oTree's own
# SQLite until a Postgres login and a database are added in Lab Settings. Next on
# step 2 tests the login first (wizard_postgres_next). Both faces only render;
# the logic is here.
# ===========================================================================

WIZARD_STEPS = ("Lab", "Postgres", "Databases")
WIZARD_NO_PG_NOTE = ("Without a Postgres login no databases can be created. Launches "
                     "use oTree's own SQLite until a Postgres login is added in Lab "
                     "Settings.")
WIZARD_SKIPPED_NOTE = ("No Postgres database on this computer: launches use oTree's own "
                       "SQLite until a Postgres login and a database are added in Lab "
                       "Settings.")


def setup_needed():
    """True when this PC must run the setup wizard: lab_info.json has no lab, or
    this PC's home lab is unset or not one of them."""
    ids = [l["id"] for l in lab_info_labs() if not l.get("deleted")]
    if not ids:
        return True
    return (read_lab_marker() or "") not in ids


def suggested_database_name(lab_id):
    """The database name the wizard prefills for ``lab_id``: the lab's
    ``suggested_database`` or a name derived from the lab id."""
    for lab in lab_info_labs():
        if lab["id"] == lab_id and lab.get("suggested_database"):
            return lab["suggested_database"]
    return {"db_name": slugify_pg_dbname("otree_%s" % (lab_id or "lab")), "db_user": ""}


def setup_state():
    """Everything the wizard shows, for both faces (JSON-serialisable)."""
    machine = load_machine()
    labs = [l for l in lab_info_labs() if not l.get("deleted")]
    # List rows show only "on HOST:PORT" for another computer's database (the
    # created-on details live in the Edit view) plus the location warning.
    dbs = [dict(d, host_note=database_host_note(d),
                location_warning=database_location_warning(d))
           for d in machine["databases"] if not d.get("deleted")]
    for d in dbs:
        d.pop("db_password", None)
    admin = pg_admin_from_store(machine)
    return {
        "steps": list(WIZARD_STEPS),
        "has_labs": bool(labs),
        "labs": [{"id": l["id"], "name": l["name"], "host": l["host"],
                  "seats": len(l["seats"]), "default_room": l["default_room"],
                  "suggested_database": suggested_database_name(l["id"])} for l in labs],
        "home_lab": machine.get("home_lab") or "",
        "pg_admin": {"admin_username": admin["admin_username"],
                     "admin_password": admin["admin_password"],
                     "admin_host": "localhost", "admin_port": admin["admin_port"]},
        "databases": dbs,
        "default_database": machine.get("default_database") or "",
        "maps": available_maps(),
        "default_admin": dict((LAB_INFO or {}).get("default_admin") or
                              {"username": "admin", "password": ""}),
        "no_pg_note": WIZARD_NO_PG_NOTE,
        "skipped_note": WIZARD_SKIPPED_NOTE,
    }


def wizard_pg_admin(admin):
    """The wizard's Postgres login with the host fixed to this computer."""
    admin = admin or {}
    return {"admin_username": str(admin.get("admin_username", "") or "").strip(),
            "admin_password": str(admin.get("admin_password", "") or ""),
            "admin_host": "localhost",
            "admin_port": str(admin.get("admin_port", "") or "").strip() or "5432"}


def test_pg_admin_connection(admin):
    """Step 2's Test connection: log in to this computer's Postgres (database
    "postgres") with the superuser. Returns ``{"ok", "message", "skipped"}``."""
    admin = wizard_pg_admin(admin)
    # The password may be blank (a superuser with no password, e.g. Postgres.app).
    if not admin["admin_username"]:
        return {"ok": False, "skipped": False,
                "message": "Enter the Postgres superuser."}
    check = test_database_connection("postgres", admin["admin_username"],
                                     admin["admin_password"], "localhost",
                                     admin["admin_port"])
    if check.get("ok"):
        return {"ok": True, "skipped": False,
                "message": "Connected to Postgres on this computer."}
    return {"ok": False, "skipped": bool(check.get("skipped")),
            "message": "Could not connect: %s" % (check.get("error") or "unknown error")}


def finish_machine_setup(home_lab, pg_admin=None, databases=None, default_index=0,
                         default_database_id=None, new_labs=None, default_admin=None,
                         researcher="", creator=None, login_check=None):
    """Step 4 of the wizard: make it so, then write machine.json.

    ``new_labs`` (only when lab_info.json has no labs) are validated and written
    to lab_info.json first, with ``default_admin`` as the default oTree login.
    ``home_lab`` must name a lab. ``pg_admin`` None = the Postgres step was
    skipped. ``databases`` is a list of ``{db_name, db_user, db_password, title?,
    create: bool}``: create=True runs CREATE DATABASE (an existing one of that
    name is simply linked), False links an existing one. ``default_index`` picks
    the new PC default among them; ``default_database_id`` may instead name a
    database this PC already has. Any failure returns ``ok`` False and writes
    NOTHING to machine.json (a retry links what was already created).
    ``creator`` replaces :func:`create_database` in tests; ``login_check``
    replaces :func:`registered_database_login_warning` (the login check run on
    every LINKED database; a created one was already checked by the create).

    Returns ``{"ok", "message", "results": [...], "default_database",
    "warnings": [...]}``; ``warnings`` are shown after a successful save (a
    database whose user cannot log in is kept, with a clear warning)."""
    creator = creator or create_database
    login_check = login_check or registered_database_login_warning
    out = {"ok": False, "message": "", "results": [], "default_database": "",
           "warnings": []}
    if new_labs:
        if [l for l in lab_info_labs() if not l.get("deleted")]:
            out["message"] = "lab_info.json already has labs; choose one of them."
            return out
        ok, message = validate_wizard_labs(new_labs)
        if not ok:
            out["message"] = message
            return out
        existing = load_lab_info() or {}
        info = build_lab_info(new_labs, admin=default_admin or {"username": "admin"})
        for key in (GITHUB_SYNC_ENABLED_KEY, GITHUB_ORG_KEY, DB_LOCALHOST_ONLY_KEY):
            if key in existing:
                info[key] = existing[key]
        save_lab_info(info)
        reload_lab_info()
        if not home_lab and info["labs"]:
            home_lab = info["labs"][0]["id"]
    home_lab = str(home_lab or "").strip().lower()
    ids = [l["id"] for l in lab_info_labs() if not l.get("deleted")]
    if home_lab not in ids:
        out["message"] = "Choose which lab this computer is."
        return out
    admin = wizard_pg_admin(pg_admin) if pg_admin else None
    machine = load_machine()
    extra = {"databases": copy.deepcopy(machine["databases"]),
             "default_database": machine.get("default_database", ""),
             "pg_admin": dict(machine.get("pg_admin") or {})}
    stamp = current_pc_stamp(home_lab=home_lab)
    created_ids = []
    for index, raw in enumerate(databases or []):
        name = str(raw.get("db_name", "") or "").strip()
        if not name:
            continue
        want_create = bool(raw.get("create"))
        if want_create and admin is None:
            out["message"] = ("%s: %s" % (name, WIZARD_NO_PG_NOTE))
            return out
        if want_create:
            res = creator(admin, name, raw.get("db_user", ""), raw.get("db_password", ""),
                          host="localhost", port=admin["admin_port"])
            if res.get("ok"):
                fields = res.get("fields") or {}
                outcome = "created"
                if res.get("warning"):
                    out["warnings"].append("%s: %s" % (name, res["warning"]))
            elif res.get("reason") == "db_exists":
                fields = existing_database_fields(admin, name, raw.get("db_user", ""),
                                                  raw.get("db_password", ""),
                                                  host="localhost", port=admin["admin_port"])
                outcome = "exists"
            else:
                out["message"] = "%s: %s" % (name, res.get("message") or "could not create it.")
                out["results"].append({"db_name": name, "outcome": "failed"})
                return out
        else:
            fields = existing_database_fields(admin or {}, name, raw.get("db_user", ""),
                                              raw.get("db_password", ""),
                                              host="localhost",
                                              port=(admin or {}).get("admin_port") or
                                              str(raw.get("db_port", "") or "5432"))
            outcome = "linked"
            warning = login_check(fields)
            if warning:
                out["warnings"].append("%s: %s" % (name, warning))
        match = find_database_by_connection(fields, known_databases_from_store(extra))
        if match is not None:
            entry = match
            outcome += " (already on the list)"
        else:
            entry = register_database(extra, title=str(raw.get("title", "") or name),
                                      researcher=researcher, connection=fields,
                                      postgres_user=fields.get("db_user", ""),
                                      created_on=dict(stamp))
        created_ids.append(entry["id"])
        out["results"].append({"db_name": name, "id": entry["id"], "outcome": outcome})
    default_id = ""
    if default_database_id and find_database(extra, default_database_id) is not None:
        default_id = default_database_id
    elif created_ids:
        default_id = created_ids[min(max(int(default_index or 0), 0), len(created_ids) - 1)]
    else:
        default_id = extra.get("default_database", "")
        if find_database(extra, default_id) is None:
            default_id = ""
    extra["default_database"] = default_id

    def _apply(m):
        m["home_lab"] = home_lab
        m["shown_labs"] = [home_lab]
        if admin is not None:
            m["pg_admin"] = admin
        m["databases"] = extra["databases"]
        m["default_database"] = default_id

    update_machine(_apply)
    # A lab with no suggested database gets the one chosen here, so the next PC
    # that copies lab_info.json is prefilled.
    chosen = find_database(extra, default_id)
    if chosen is not None:
        info = load_lab_info() or {}
        changed = False
        for lab in info.get("labs", []):
            if lab["id"] == home_lab and not lab.get("suggested_database"):
                lab["suggested_database"] = {
                    "db_name": chosen["db_name"],
                    "db_user": "" if chosen.get("uses_admin_login") else chosen["db_user"]}
                changed = True
        if changed:
            save_lab_info(info)
    reload_lab_info()
    out.update(ok=True, default_database=default_id,
               message=("This computer is set up." if default_id else
                        "This computer is set up. " + WIZARD_SKIPPED_NOTE))
    return out


# The module's live state is loaded LAST: the loaders use helpers defined above.
MACHINE = default_machine()
try:
    reload_lab_info()
except Exception:   # never let a bad data file break the import
    pass
