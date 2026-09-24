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
import datetime as _dt
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
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
APP_VERSION = "1.0.2"
PRESETS_FILENAME = "presets.json"
SESSIONS_FILENAME = "sessions.jsonl"
UPDATE_CHECK_FILENAME = "update_check.json"
# A gitignored, one-word per-machine marker ("large"/"small") that identifies
# which lab this computer is. It sits next to the launcher checkout, not in a
# researcher's project, because it is a property of the machine.
LAB_MARKER_FILENAME = "lab.local"
STORAGE_VERSION = 1

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
    the repo root's data/ folder (parent of app/)."""
    return os.path.join(repo_root(), DATA_DIRNAME)


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
    """
    handle = open(lock_path, "a+")
    locked = False
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except ImportError:
            try:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            except (ImportError, OSError):
                pass
        yield
    finally:
        if locked:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
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
                     (a hand-edit typo, or an interrupted write from before the
                     atomic-save fix); it is RECOVERABLE, so a wizard must not
                     silently overwrite it (see :func:`load_lab_info_for_setup`)
      "ok"       -- the file exists and parses to a dict
    """
    path = path or lab_info_path()
    if not os.path.exists(path):
        return "missing"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return "malformed"
    return "ok" if isinstance(data, dict) else "malformed"


def load_lab_info(path=None):
    """Read lab_info.json into a dict, or None when it is absent or unreadable.

    A present-but-unparseable file also returns None, so a hand-edit typo is
    treated the same as "not set up yet" (first run) rather than crashing. To
    tell those apart (and preserve a recoverable file) use
    :func:`lab_info_status` / :func:`preserve_corrupt_lab_info`.
    """
    path = path or lab_info_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def preserve_corrupt_lab_info(path=None):
    """Rename a malformed lab_info.json to a timestamped ``.corrupt`` copy.

    Called before a first-run wizard is allowed to write a fresh lab_info.json:
    when the existing file EXISTS but is unreadable/invalid it is recoverable, so
    it is moved aside (``lab_info.json.<stamp>.corrupt``) rather than silently
    overwritten. Returns the recovery path if one was made, else None (the file
    was absent or already valid). Never raises: a rename failure just returns
    None so the caller can still proceed.
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
    """True when a readable lab_info.json exists. The first-run check: when this
    is False the launcher runs its setup wizard to create the file."""
    return load_lab_info(path) is not None


def save_lab_info(data, path=None):
    """Write a lab_info dict to lab_info.json atomically, owner-only. Returns path.

    The pretty-printed JSON is written to a temp file in the same directory,
    flushed + fsync'd, then os.replace'd into place, so an interrupted write can
    never leave a half-written (malformed) lab_info.json that the next start
    would mistake for a broken first run. The file holds DB and oTree admin
    passwords, so it is created 0o600 (owner-only) on POSIX -- see
    :func:`secure_chmod`.
    """
    path = path or lab_info_path()
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    # If the file we are about to replace is malformed (a recoverable typo or a
    # half-written file from an old, non-atomic save), move it aside first so the
    # first-run wizard's fresh write never silently destroys recoverable data. A
    # valid file is left untouched (this is a no-op) and overwritten as normal.
    preserve_corrupt_lab_info(path)
    text = json.dumps(data, indent=2)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=folder, prefix=".lab_info-", suffix=".tmp", delete=False
    )
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


def lab_info_example_path():
    """Where the shipped lab_info.example.json lives: data/ at the repo root."""
    return os.path.join(data_dir(), LAB_INFO_EXAMPLE_FILENAME)


def load_example_lab_info():
    """The committed lab_info.example.json as a dict, or None."""
    return load_lab_info(lab_info_example_path())


def available_maps():
    """Sorted names of the map files in the maps/ folder (bare stems), so a
    setup wizard can offer them for a lab to reference."""
    try:
        names = [f[:-5] for f in os.listdir(maps_dir()) if f.endswith(".json")]
    except OSError:
        names = []
    return sorted(names)


def validate_wizard_labs(labs):
    """Validate a wizard's list of labs with the SAME rule the Lab Settings path
    uses (:func:`validate_lab_preset_fields`): each lab needs a name, a non-empty
    Host/IP and at least one valid, non-duplicate seat label; and no two labs may
    resolve to the same id. Returns ``(ok, message)``.

    Both wizard faces (the Tk FirstRunWizard and the web setup wizard) call this
    before writing lab_info.json, so the first-run path can no longer save a lab
    with an EMPTY host -- which used to render ``<host>`` in the participant links
    and then hide the wizard on every future start. There is deliberately ONE
    validator (this delegates to ``validate_lab_preset_fields``); the wizards do
    not invent their own.
    """
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


def build_lab_info(labs, database, admin, default_lab=None):
    """Assemble a lab_info dict from wizard inputs.

    ``labs`` is a list of {"id"?, "name", "host", "seats": [...], "map": <name or "">}.
    A blank id is slugified from the name. ``database`` and ``admin`` are dicts.

    Each lab is checked with the shared :func:`validate_lab_preset_fields` rule
    (the same one the Lab Settings path uses); a lab that fails it -- an empty
    Host/IP, no/invalid seats, or a duplicate id -- is SKIPPED rather than written
    as a broken default. Callers should gate the save on
    :func:`validate_wizard_labs` so a rejection is a clear error, not a silent
    drop, but this is the backstop that keeps a broken lab out of the file.
    """
    out_labs = {}
    first_id = None
    for raw in labs:
        name = str(raw.get("name") or "")
        host = str(raw.get("host") or "")
        seats = raw.get("seats") or []
        ok, _message, parsed = validate_lab_preset_fields(name, host, seats)
        if not ok:
            # Skip invalid labs (empty host / bad or missing seats): never write a
            # broken default. The wizard front-ends validate first, so a valid run
            # never reaches this branch.
            continue
        lab_id = str(raw.get("id") or "").strip() or _slugify_lab_id(name)
        if not lab_id or lab_id in out_labs:
            continue
        entry = {
            "name": name.strip() or lab_id,
            "host": host.strip(),
            "seats": [str(s) for s in parsed],
            # Per-lab default room (missing/blank -> "study"); drives the built-in
            # Lab default config + new configs on this machine.
            "default_room": sanitize_room_name(raw.get("default_room")),
        }
        if str(raw.get("map") or "").strip():
            entry["map"] = str(raw["map"]).strip()
        # Optional: the human name the participant-PC desktop shortcuts are saved
        # as, shown in the launch briefing. Only written when set.
        if str(raw.get("shortcut_label") or "").strip():
            entry["shortcut_label"] = str(raw["shortcut_label"]).strip()
        out_labs[lab_id] = entry
        if first_id is None:
            first_id = lab_id
    data = {
        "default_lab": default_lab or first_id or "",
        "labs": out_labs,
        "database": {
            "db_name": str(database.get("db_name", "otree")),
            "db_user": str(database.get("db_user", "otree")),
            "db_password": str(database.get("db_password", "")),
            "db_host": str(database.get("db_host", "localhost")),
            "db_port": str(database.get("db_port", "5432")),
        },
        "admin": {
            "username": str(admin.get("username", "admin")),
            "password": str(admin.get("password", "")),
        },
    }
    return data


# Loaded once at import. The launcher re-reads via load_lab_info() after the
# wizard writes the file, so a first run does not need a restart.
LAB_INFO = load_lab_info()

# Safe placeholders, used ONLY until lab_info.json exists. Real values always
# come from the file.
_DUMMY_DB = {
    "db_name": "otree", "db_user": "otree", "db_password": "",
    "db_host": "localhost", "db_port": "5432",
}


def lab_db_from_info(info):
    """The database credentials dict for a lab_info dict (placeholders if None)."""
    out = dict(_DUMMY_DB)
    db = (info or {}).get("database") or {}
    for key in _DUMMY_DB:
        if db.get(key) is not None:
            out[key] = str(db[key])
    return out


def wizard_lab_db():
    """The IMMUTABLE lab-shared credentials from the setup wizard (lab_info.json).

    This is the original wizard-time database. It is the fallback for the
    lab-shared default when no other database has been promoted, and it stays
    fixed regardless of which database is currently chosen as the default (unlike
    :data:`LAB_DB`, which reflects the *current* default). Keeping the two apart
    is what lets the default-database picker offer the wizard DB as a stable
    choice even after a custom database has been made the default.
    """
    return lab_db_from_info(LAB_INFO)


# LAB_DB holds the RESOLVED lab-shared database credentials: what DB_MODE_LAB
# ("Lab shared database (Postgres)") resolves to right now. On a fresh import it
# is the setup-wizard DB from lab_info.json; once a store is loaded both UIs call
# apply_default_database(extra), which re-points LAB_DB at whichever database the
# store's default_database reference names (the wizard DB, or any custom DB
# promoted to default via Lab Settings). Everything downstream -- normalize_config,
# build_database_url, build_env, the built-in lab picker entry, database_config_fields
# -- reads this live module attribute, so promoting a custom database to default
# transparently redirects every DB_MODE_LAB launch to it.
LAB_DB = lab_db_from_info(LAB_INFO)

_admin_info = (LAB_INFO or {}).get("admin") or {}
DEFAULT_ADMIN_USERNAME = str(_admin_info.get("username") or "admin")
DEFAULT_ADMIN_PASSWORD = str(_admin_info.get("password") or "")

DB_MODE_LAB = "lab"
DB_MODE_CUSTOM = "custom"
DB_MODE_NONE = "none"

DB_MODE_LABELS = {
    DB_MODE_LAB: "Lab shared database (Postgres)",
    DB_MODE_CUSTOM: "My own Postgres database",
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


def _default_lab_id_from_info(info):
    chosen = str((info or {}).get("default_lab") or "").strip()
    if chosen:
        return chosen
    labs = (info or {}).get("labs") or {}
    return next(iter(labs), "lab")


DEFAULT_LAB_ID = _default_lab_id_from_info(LAB_INFO)

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
    if out["db_mode"] == DB_MODE_LAB:
        out.update(LAB_DB)
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
    return "postgres://{user}:{password}@{host}:{port}/{name}".format(
        user=_urlquote(c["db_user"]),
        password=_urlquote(c["db_password"]),
        host=c["db_host"],
        port=c["db_port"],
        name=c["db_name"],
    )


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


def validate_project(path):
    """Check that a folder looks like an oTree project.

    Returns (level, message) where level is "ok", "warn" or "error".
    A missing folder is an error and blocks launching; anything else only warns.
    """
    path = (path or "").strip()
    if not path:
        return "warn", "No project folder chosen yet. Click Browse to pick one."
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
    """The directory where presets.json and seats/ live: the app's data/ folder."""
    return data_dir()


def presets_path():
    override = os.environ.get("OTREE_LAB_LAUNCHER_PRESETS")
    if override:
        return override
    return os.path.join(config_dir(), PRESETS_FILENAME)


# --- Per-machine lab identity (lab.local) ----------------------------------
# Each lab PC carries a gitignored one-word marker file, `lab.local`, next to
# the launcher, saying which lab it is ("large" or "small"). The launcher reads
# it at startup to configure the built-in Lab default's lab, and writes it
# ONCE from a first-launch operator choice. It is never auto-rewritten after
# that, so the choice can only be changed by hand-editing the file.


def lab_marker_path():
    """Where lab.local lives. OTREE_LAB_MARKER overrides it (used by tests)."""
    override = os.environ.get("OTREE_LAB_MARKER")
    if override:
        return override
    return os.path.join(data_dir(), LAB_MARKER_FILENAME)


def read_lab_marker(path=None):
    """This machine's lab id, or None when unset.

    Historically the marker held only "large"/"small"; it now holds ANY lab
    preset id (a lowercase slug), so a machine can be identified as a lab the
    operator added on the Lab Settings page. The stored word is returned as-is
    (stripped, lower-cased), so "large"/"small" still resolve to the two
    built-in labs. An empty or missing file means "unset", first launch, where
    the operator is asked to choose.
    """
    path = path or lab_marker_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            word = handle.read().strip().lower()
    except OSError:
        return None
    return word or None


def set_lab_marker(lab_id, path=None):
    """Record this machine's lab id, OVERWRITING any existing marker.

    This is the UI-settable path (the first-run chooser and the Lab Settings
    "which lab is this computer" control), so the operator never has to
    hand-edit lab.local to change which lab the machine is. Accepts any non-empty
    lab id. Returns the id written.
    """
    lab_id = str(lab_id or "").strip().lower()
    if not lab_id:
        raise ValueError("lab_id must be a non-empty lab id")
    path = path or lab_marker_path()
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(lab_id + "\n")
    return lab_id


def write_lab_marker(lab, path=None):
    """First-run write: record the lab id only if no valid marker exists yet.

    Returns True if written, False if refused because a marker already exists,
    so a first-run choice can never silently revert a machine that is already
    identified. Accepts any non-empty lab id (not only large/small). To CHANGE
    an existing identity from the UI use set_lab_marker, which overwrites.
    """
    lab = str(lab or "").strip().lower()
    if not lab:
        raise ValueError("lab must be a non-empty lab id")
    path = path or lab_marker_path()
    if read_lab_marker(path) is not None:
        return False
    set_lab_marker(lab, path)
    return True


_MARKER_UNSET = object()


def apply_lab_marker(presets, marker=_MARKER_UNSET, lab_presets=None):
    """Point the built-in Lab default's lab at this machine's lab, in place.

    The built-in default is app-owned, so its lab tracks lab.local. User configs
    are never touched. A no-op when the marker is unset (first launch), which
    leaves the built-in on its code default. Any non-empty lab id is honoured,
    so a machine identified as an added lab points the default there too.

    When ``lab_presets`` is given, the built-in default's ``room_name`` is also
    re-derived from that lab's ``default_room`` (the LIVE source of truth, so a
    room edited in Lab Settings updates the Lab default at once). Without
    ``lab_presets`` the room is left as :func:`default_preset` set it.
    """
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
    """Make ``lab_id`` this machine's single lab: display only it, hide the rest.

    The machine's lab is a UI choice recorded in lab.local; this reflects that
    choice in the existing per-preset display toggles so the main lab selector
    collapses to the one lab (and cannot pick a wrong one). Returns
    (ok, message, new_list); refuses when no preset carries that id.
    """
    presets = [normalize_lab_preset(p) for p in (lab_presets or [])]
    if find_lab_preset(lab_id, presets) is None:
        return False, "No lab preset with that id.", presets
    for preset in presets:
        preset["display"] = (preset["id"] == str(lab_id))
    return True, "", presets


def default_preset():
    preset = dict(DEFAULT_CONFIG)
    preset["name"] = "Lab default"
    preset["created"] = now_iso()
    preset["last_run"] = None
    # author/builtin are metadata (like name/created/last_run), NOT config fields
    # in FIELD_KEYS, so they never enter the config-equality comparison that
    # guards immutability. The shipped default is built-in and built in.
    preset["author"] = "builtin"
    preset["builtin"] = True
    # This machine's lab identity (from lab.local) configures the built-in
    # default's lab, so on a lab PC the default already points at the right lab.
    # Any non-empty id is honoured; unset (first launch) leaves the code default.
    marker = read_lab_marker()
    if marker:
        preset["lab"] = marker
        # ...and the default config's room follows THAT lab's default_room, so on
        # a lab PC the Lab default opens the room the lab actually uses (falls
        # back to "study"). The base is lab_info.json; a room later edited in Lab
        # Settings is re-applied live via apply_lab_marker(..., lab_presets=...).
        preset["room_name"] = lab_default_room(default_lab_presets(), marker)
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


def load_store(path=None, default_factory=None):
    """Read the presets file.

    Returns (presets, extra) where `presets` is the list of stored records
    exactly as they were written (unknown keys included) and `extra` holds any
    top-level keys of the file this version does not know about.  A file that
    cannot be parsed is moved aside rather than overwritten.

    ``default_factory`` builds the fallback "Lab default" record used when the
    file is absent, unreadable, or empty. It defaults to :func:`default_preset`;
    the Tk launcher passes its own so it keeps its own default (e.g. its
    browser-open delay) while sharing this one parse/backup implementation.
    """
    make_default = default_factory or default_preset
    path = path or presets_path()
    if not os.path.exists(path):
        return [make_default()], {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        backup = path + ".broken-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass
        return [make_default()], {}

    extra = {}
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("presets", [])
        extra = {k: v for k, v in data.items() if k not in ("presets", "version")}
    else:
        raw = []

    presets = [item for item in raw if isinstance(item, dict)]
    # A record without a usable name still belongs to somebody, so keep it
    # rather than dropping it silently.
    for index, item in enumerate(presets):
        if not str(item.get("name", "")).strip():
            item["name"] = "Unnamed config %d" % (index + 1)
    if not presets:
        presets = [make_default()]
    return presets, extra


def save_store(presets, extra=None, path=None):
    """Write the presets file atomically.

    The new content goes to a temporary file in the same directory, is flushed
    to disk, and only then replaces the old file, so an interrupted write can
    never leave a half-written presets.json behind.

    The whole write is wrapped in a best-effort CROSS-PROCESS file lock on a
    sibling ``.lock`` file, so two launcher instances sharing one data dir (e.g.
    a GUI and the headless one-click shortcut) serialise their writes instead of
    racing os.replace. The in-process store lock each face holds guards threads
    within one process; this guards separate processes. It degrades to a no-op
    where OS locking is unavailable (the atomic temp-file + os.replace is still
    correct on its own -- see :func:`exclusive_file_lock`).
    """
    path = path or presets_path()
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    payload = dict(extra or {})
    payload["version"] = STORAGE_VERSION
    payload["presets"] = presets
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)

    with exclusive_file_lock(path + ".lock"):
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=folder, prefix=".presets-", suffix=".tmp", delete=False
        )
        tmp_name = handle.name
        try:
            # presets.json carries DB creds, custom-DB creds and Postgres-admin creds,
            # so lock it down to owner-only (0o600) on POSIX, including the temp file,
            # before it is fsync'd and replaced into place. See secure_chmod (Windows
            # ACLs are not hardened here).
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
    staff should always begin from it and Browse to the study of the day. Falls
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


def preset_from_fields(name, fields, created=None, author=None):
    preset = normalize_config(fields)
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


def sessions_path():
    """Where the launch history log lives. OTREE_LAB_SESSIONS overrides it
    (used by tests). It sits in data/, never in a researcher's project."""
    override = os.environ.get("OTREE_LAB_SESSIONS")
    if override:
        return override
    return os.path.join(data_dir(), SESSIONS_FILENAME)


def _session_database_label(cfg):
    """A short, human database label for one session-log line (no secrets)."""
    c = normalize_config(cfg)
    mode = c["db_mode"]
    if mode == DB_MODE_NONE:
        return "SQLite (no lab DB)"
    name = (c.get("db_name") or "otree").strip() or "otree"
    if mode == DB_MODE_LAB:
        return "Lab shared Postgres (%s)" % name
    return "Custom Postgres (%s)" % name


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
    """Append ONE JSON line to data/sessions.jsonl for a launch.

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
    """The most recent launches from data/sessions.jsonl, NEWEST FIRST.

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


def _fetch_release_payload(timeout=4.0):
    """GET the latest-release JSON from GitHub (raw bytes). Short timeout so a
    stalled network never hangs the check; the caller swallows any error
    (including the 404 GitHub returns when there are no releases yet)."""
    import urllib.request
    request = urllib.request.Request(
        GITHUB_RELEASES_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "otree-lab-launcher"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
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
         "checked": bool,               # True only if the network was hit this call
         "repo_url": REPO_URL,          # where the footer link points
         "label": str,                  # UPDATE_LABEL when an update exists, else ""
         "tooltip": UPDATE_TOOLTIP}     # the hover text next to the link

    When the cached check is fresh (< a day old) and not ``force``, it returns the
    cached verdict with NO network. Any network error, a 404 (no releases), or a
    parse error is swallowed and the last known cache (or a silent "no update") is
    returned -- the check never raises and never blocks longer than the fetch
    timeout. ``fetcher`` (returns a payload for :func:`parse_github_release_tag`)
    is injectable for tests.
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
    fresh = False
    if not force and last_checked:
        try:
            when = _dt.datetime.fromisoformat(str(last_checked))
            fresh = (now - when).total_seconds() < UPDATE_CHECK_INTERVAL_SECONDS
        except (TypeError, ValueError):
            fresh = False

    checked = False
    if not fresh:
        fetch = fetcher or _fetch_release_payload
        try:
            tag = parse_github_release_tag(fetch())
            if tag:
                remote_version = tag
                checked = True
                _write_update_cache(path, {
                    "remote_version": remote_version,
                    "last_checked": now.isoformat(timespec="seconds")})
        except Exception:
            # No network / no releases (404) / parse error => silent; keep the
            # cache we had.
            pass

    update_available = version_is_newer(remote_version, APP_VERSION)
    return {"update_available": update_available,
            "current": APP_VERSION,
            "remote_version": remote_version,
            "checked": checked,
            "repo_url": REPO_URL,
            "label": UPDATE_LABEL if update_available else "",
            "tooltip": UPDATE_TOOLTIP}


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
            "set DATABASE_URL=postgres://%DB_USER%:%DB_PASSWORD%@%DB_HOST%:%DB_PORT%/%DB_NAME%",
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
    base = sanitize_shortcut_name(name)
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
_MAP_FILE_CACHE = {}


def maps_dir():
    return os.path.join(data_dir(), MAPS_DIRNAME)


def load_map_file(name):
    """The map dict in maps/<name>.json, or None. Cached; name is a bare stem."""
    name = str(name or "").strip()
    if not name:
        return None
    if name in _MAP_FILE_CACHE:
        return _MAP_FILE_CACHE[name]
    # basename guards against a reference trying to escape the maps/ folder.
    path = os.path.join(maps_dir(), os.path.basename(name) + ".json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    data = data if isinstance(data, dict) else None
    _MAP_FILE_CACHE[name] = data
    return data


def resolve_lab_map(map_field, maps_table=None):
    """A lab's spatial map object, or None.

    ``map_field`` is either a full map dict (inline in the lab's entry, the
    optional fallback) or a string naming a map. A named map resolves against an
    optional inline ``maps`` table first (if the file supplies one) and then
    against the maps/ folder as maps/<name>.json (the primary, documented path),
    so a real lab can say ``"map": "example_large"`` and inherit that geometry.
    """
    if isinstance(map_field, dict):
        return map_field
    if isinstance(map_field, str) and map_field.strip():
        name = map_field.strip()
        if maps_table and isinstance(maps_table.get(name), dict):
            return maps_table[name]
        return load_map_file(name)
    return None


def default_lab_presets():
    """The built-in labs, seeded from lab_info.json.

    Empty when lab_info.json is absent (first run), the launcher runs its setup
    wizard in that case. Each lab's map is resolved here (a maps/<name>.json
    reference, or an inline object) so callers just read preset["map"].
    """
    info = LAB_INFO or {}
    labs = info.get("labs") or {}
    maps_table = info.get("maps") or {}   # optional inline table, still honoured
    presets = []
    for lab_id, raw in labs.items():
        raw = raw or {}
        presets.append(normalize_lab_preset({
            "id": lab_id,
            "name": raw.get("name") or str(lab_id).title(),
            "ip": raw.get("host", ""),
            "seats": list(raw.get("seats", [])),
            "display": raw.get("display", True),
            "geometry": raw.get("geometry", LAB_GEO_GRID),
            "map": resolve_lab_map(raw.get("map"), maps_table),
            "default_room": raw.get("default_room"),
            "shortcut_label": raw.get("shortcut_label", ""),
            "builtin": True,
        }))
    return presets


def reload_lab_info(path=None):
    """Re-read lab_info.json and refresh the module-level defaults.

    Called after the first-run wizard writes the file, so the app picks up the
    new labs/credentials without a restart.
    """
    global LAB_INFO, LAB_DB, DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD, DEFAULT_LAB_ID
    # A map added or edited while the app runs would otherwise need a restart
    # because load_map_file caches by name; clear the cache so a reload picks up
    # new/edited maps/<name>.json immediately.
    _MAP_FILE_CACHE.clear()
    LAB_INFO = load_lab_info(path)
    LAB_DB = lab_db_from_info(LAB_INFO)
    admin = (LAB_INFO or {}).get("admin") or {}
    DEFAULT_ADMIN_USERNAME = str(admin.get("username") or "admin")
    DEFAULT_ADMIN_PASSWORD = str(admin.get("password") or "")
    DEFAULT_LAB_ID = _default_lab_id_from_info(LAB_INFO)
    DEFAULT_CONFIG.update({
        "db_name": LAB_DB["db_name"], "db_user": LAB_DB["db_user"],
        "db_password": LAB_DB["db_password"], "db_host": LAB_DB["db_host"],
        "db_port": LAB_DB["db_port"], "admin_username": DEFAULT_ADMIN_USERNAME,
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
    """Only the presets the lab selector should show, in stored order."""
    return [p for p in (lab_presets or []) if p.get("display", True)]


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
    return [p for p in presets
            if p.get("display", True)
            or (cid is not None and str(p.get("id", "")) == cid)]


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
    """Show or hide one preset in the lab selector. Guarded so the selector is
    never left with nothing to show."""
    presets = [normalize_lab_preset(p) for p in (lab_presets or [])]
    target = find_lab_preset(lab_id, presets)
    if target is None:
        return False, "No lab preset with that id.", presets
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


# ---------------------------------------------------------------------------
# Postgres admin config (Feature 2/4), used ONLY to create databases, never as
# launch environment variables. Stored, like lab presets, in the store's extra.
# ---------------------------------------------------------------------------

PG_ADMIN_KEYS = ("admin_username", "admin_password", "admin_host", "admin_port")


def pg_admin_from_store(extra):
    """The Postgres admin config for this store, with sane host/port defaults."""
    raw = (extra or {}).get("pg_admin") or {}
    return {
        "admin_username": str(raw.get("admin_username", "")),
        "admin_password": str(raw.get("admin_password", "")),
        "admin_host": str(raw.get("admin_host", "") or "localhost"),
        "admin_port": str(raw.get("admin_port", "") or "5432"),
    }


def pg_admin_ready(admin):
    """Which required admin fields are still blank (empty list means ready)."""
    admin = admin or {}
    missing = []
    labels = {"admin_username": "username", "admin_password": "password",
              "admin_host": "host", "admin_port": "port"}
    for key in PG_ADMIN_KEYS:
        if not str(admin.get(key, "")).strip():
            missing.append(labels[key])
    return missing


# ---------------------------------------------------------------------------
# Known-databases registry + researcher roster (Round 3). Both are GLOBAL,
# passive collections stored in the store's `extra` dict next to lab_presets /
# pg_admin, so every config sees them (exactly like lab presets). The registry
# NEVER connects to Postgres by itself: only create_database() does, and its
# caller then calls register_database() with the confirmed result. See the API
# index at the top of this module and _ai/CORE_DB_API.md.
# ---------------------------------------------------------------------------

# Stable ids of the two always-present built-in databases.
DB_BUILTIN_SQLITE = "otree_default"
DB_BUILTIN_LAB = "lab_shared"

# The store `extra` key that holds WHICH database is the lab-shared default, as a
# REFERENCE (a database id), not a copy of its credentials. See the "default
# database" section further down (default_database_id / set_default_database /
# apply_default_database). Absent -> the lab built-in (the setup-wizard DB).
DEFAULT_DATABASE_KEY = "default_database"

DB_BUILTIN_SQLITE_TITLE = "oTree default (SQLite)"
DB_BUILTIN_LAB_TITLE = "Lab shared database (Postgres)"

# The connection fields a custom entry carries (kept separate from the person).
DATABASE_CONN_KEYS = ("db_name", "db_user", "db_password", "db_host", "db_port")


def _slugify_db_id(title):
    slug = re.sub(r"[^a-z0-9]+", "_", str(title or "").lower()).strip("_")
    return slug or "db"


def _unique_db_id(title, existing_ids):
    """A registry id derived from the title, unique among existing ids and never
    colliding with the two reserved built-in ids."""
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
    # Everything outside the Postgres identifier set (letters/digits/_/$) -- which
    # includes spaces and hyphens -- becomes an underscore. We lowercased first,
    # so the surviving letters are a-z.
    text = re.sub(r"[^a-z0-9_$]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text or text[0].isdigit():
        text = "_" + text
    return text


def normalize_database_entry(raw):
    """One custom registry entry as a normalized dict.

    Every field is a string; the connection keys are always present; the
    creator ``researcher`` (a person) is kept distinct from ``postgres_user``
    (the database credential). ``db_mode`` is always ``custom`` for a registry
    entry (the two built-ins are synthesized by ``builtin_databases``).
    """
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
    }
    for key in DATABASE_CONN_KEYS:
        entry[key] = str(raw.get(key, "") or "")
    if not entry["title"]:
        entry["title"] = entry["db_name"] or "custom database"
    return entry


def known_databases_from_store(extra):
    """The custom databases saved in the store (the append-only registry),
    normalized and in registry (insertion) order. Never includes the built-ins.
    """
    raw = (extra or {}).get("databases")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            entry = normalize_database_entry(item)
            if entry["id"]:
                out.append(entry)
    return out


def builtin_databases():
    """The two always-present built-in database entries: the oTree default
    (SQLite) and the lab shared database (Postgres). Synthesized, never stored.
    The lab entry reflects the live LAB_DB credentials."""
    sqlite = {"id": DB_BUILTIN_SQLITE, "title": DB_BUILTIN_SQLITE_TITLE,
              "researcher": "", "postgres_user": "", "database_url": "",
              "created": "", "builtin": True, "db_mode": DB_MODE_NONE}
    lab = {"id": DB_BUILTIN_LAB, "title": DB_BUILTIN_LAB_TITLE,
           "researcher": "", "postgres_user": str(LAB_DB.get("db_user", "")),
           "database_url": "", "created": "", "builtin": True, "db_mode": DB_MODE_LAB}
    for key in DATABASE_CONN_KEYS:
        sqlite[key] = ""
        lab[key] = str(LAB_DB.get(key, ""))
    return [sqlite, lab]


def list_databases(extra):
    """The full database picker list, in order: the SQLite built-in, the lab
    shared built-in, then every custom database in the registry. Every UI shows
    the SAME global list (anybody may use anybody else's database)."""
    return builtin_databases() + known_databases_from_store(extra)


def find_database(extra, db_id):
    """A database entry by id, across built-ins + the registry, or None."""
    db_id = str(db_id or "").strip()
    if not db_id:
        return None
    for entry in list_databases(extra):
        if entry["id"] == db_id:
            return entry
    return None


def database_config_fields(entry):
    """The config field overrides that selecting this database applies: always
    ``db_mode``, plus the live lab credentials for the lab built-in, or the
    stored connection fields for a custom database. Selecting the SQLite
    built-in only sets ``db_mode`` to none."""
    entry = entry or {}
    mode = entry.get("db_mode", DB_MODE_CUSTOM)
    if mode == DB_MODE_NONE:
        return {"db_mode": DB_MODE_NONE}
    if mode == DB_MODE_LAB:
        fields = {"db_mode": DB_MODE_LAB}
        fields.update(LAB_DB)
        return fields
    fields = {"db_mode": DB_MODE_CUSTOM}
    for key in DATABASE_CONN_KEYS:
        fields[key] = str(entry.get(key, "") or "")
    return fields


def register_database(extra, title, researcher, connection=None,
                      postgres_user="", database_url="", created=None):
    """Append a custom database to the registry (append-only) and record its
    creator in the researcher roster.

    Mutates ``extra`` in place and returns the new, normalized entry.
    ``connection`` is a dict of the ``db_*`` connection keys, so a
    ``create_database`` result's ``fields`` dict can be passed straight in. The
    Postgres ``user`` (a credential) is kept distinct from ``researcher`` (the
    person); when ``postgres_user`` is omitted it defaults to the connection's
    ``db_user``.
    """
    if extra is None:
        raise ValueError("register_database needs a store `extra` dict to write into")
    connection = dict(connection or {})
    raw = {
        "title": str(title or "").strip(),
        "researcher": str(researcher or "").strip(),
        "postgres_user": str(postgres_user or connection.get("db_user", "")).strip(),
        "database_url": str(database_url or ""),
        "created": created or now_iso(),
    }
    for key in DATABASE_CONN_KEYS:
        raw[key] = str(connection.get(key, "") or "")
    existing = known_databases_from_store(extra)
    raw["id"] = _unique_db_id(raw["title"] or raw["db_name"], [e["id"] for e in existing])
    entry = normalize_database_entry(raw)
    stored = extra.get("databases")
    if not isinstance(stored, list):
        stored = []
    stored.append(entry)
    extra["databases"] = stored
    if entry["researcher"]:
        add_researcher(extra, entry["researcher"])
    return entry


def edit_database(extra, db_id, title=None, researcher=None, db_name=None,
                  db_user=None, db_password=None, db_host=None, db_port=None,
                  postgres_user=None):
    """Edit an EXISTING database entry in place and persist the change.

    Works for BOTH a custom registry entry AND the lab-shared built-in
    (:data:`DB_BUILTIN_LAB`) -- the lab-shared database used to be read-only, and
    making it editable here is the point of this function. Only the fields passed
    (non-None) are changed; the rest keep their current values.

      * A custom registry entry is updated in place in ``extra["databases"]``
        (its id, so any default-database reference to it, is preserved). The
        caller then saves presets.json as usual.
      * The lab-shared built-in has no registry row -- it IS the lab_info.json
        ``database`` block -- so its edit is written straight into lab_info.json
        (:func:`save_lab_info` + :func:`reload_lab_info`) and the live
        :data:`LAB_DB` is re-resolved via :func:`apply_default_database`, so every
        DB_MODE_LAB launch immediately follows the change.
      * The oTree default (SQLite) built-in has nothing to edit -> ``ValueError``.

    ``postgres_user`` is an alias for the connection ``db_user`` (the two are the
    same Postgres role); passing either updates it. Mutates ``extra`` in place and
    returns the updated, normalized entry.
    """
    if extra is None:
        raise ValueError("edit_database needs a store `extra` dict to write into")
    db_id = str(db_id or "").strip()
    if db_id == DB_BUILTIN_SQLITE:
        raise ValueError("The oTree default (SQLite) database has nothing to edit.")

    # The connection updates, keyed by the DATABASE_CONN_KEYS. postgres_user is a
    # synonym for db_user; an explicit db_user wins when both are given.
    conn_updates = {"db_name": db_name, "db_user": db_user,
                    "db_password": db_password, "db_host": db_host,
                    "db_port": db_port}
    if postgres_user is not None and db_user is None:
        conn_updates["db_user"] = postgres_user

    if db_id == DB_BUILTIN_LAB:
        info = load_lab_info() or {}
        # Seed from the current resolved wizard credentials so a partial edit
        # (say just the host) keeps the other fields intact.
        block = dict(lab_db_from_info(info))
        for key, value in conn_updates.items():
            if value is not None:
                block[key] = str(value)
        info["database"] = block
        save_lab_info(info)
        reload_lab_info()
        apply_default_database(extra)
        return find_database(extra, DB_BUILTIN_LAB)

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
    if title is not None:
        entry["title"] = str(title).strip() or entry["title"]
    if researcher is not None:
        entry["researcher"] = str(researcher).strip()
    if postgres_user is not None:
        entry["postgres_user"] = str(postgres_user).strip()
    for key, value in conn_updates.items():
        if value is not None:
            entry[key] = str(value)
    # Keep the recorded Postgres user aligned with db_user when only db_user was
    # given (they name the same role), unless postgres_user was set explicitly.
    if db_user is not None and postgres_user is None:
        entry["postgres_user"] = str(db_user).strip()
    entry = normalize_database_entry(entry)
    stored[target_index] = entry
    extra["databases"] = stored
    if researcher is not None and entry["researcher"]:
        add_researcher(extra, entry["researcher"])
    # If this custom database is the one currently promoted to the lab-shared
    # default, its connection just changed, so re-resolve the live LAB_DB.
    if default_database_id(extra) == entry["id"]:
        apply_default_database(extra)
    return entry


# -- The lab-shared DEFAULT database (a reference into the one list) ---------
#
# The "default" (lab shared) database is NOT a special, wizard-time database any
# more: it is simply WHICH entry of the one database list is currently chosen as
# the lab-shared one. The choice is stored as a REFERENCE -- the entry's id, in
# ``extra["default_database"]`` (DEFAULT_DATABASE_KEY) -- next to lab_presets /
# databases / researchers, so it persists in presets.json exactly like the rest
# of the registry. Storing an id (not a credentials copy) is what makes "switch
# which database is the default" a one-line change and keeps a single source of
# truth for each database's connection details.
#
# Backward compatibility: an old store / the baked lab_info.json carries no
# explicit reference, so default_database_id() falls back to the lab built-in
# (DB_BUILTIN_LAB), whose credentials ARE the lab_info.json database block. So
# DB_MODE_LAB keeps resolving to the same wizard credentials as before with no
# migration, and the baked lab_info.json database is, in effect, the seeded
# default. A custom database created AFTER the wizard can be promoted to default
# by pointing the reference at its id -- the bug this refactor fixes.


def default_database_id(extra):
    """The id of the database chosen as the lab-shared default.

    Falls back to the lab built-in (:data:`DB_BUILTIN_LAB`) when the store has no
    explicit reference, so an old store and the baked lab_info.json keep
    resolving the lab-shared database to the setup-wizard credentials.
    """
    value = str((extra or {}).get(DEFAULT_DATABASE_KEY, "")).strip()
    return value or DB_BUILTIN_LAB


def resolve_default_database(extra):
    """The database entry currently chosen as the lab-shared default.

    Returns the entry the stored reference names (a custom DB, or the lab
    built-in), falling back to the lab built-in when the reference is unset or no
    longer resolves (e.g. it named a custom database that was later removed).
    """
    entry = find_database(extra, default_database_id(extra))
    if entry is None:
        entry = find_database(extra, DB_BUILTIN_LAB)
    return entry


def lab_shared_db_fields(extra):
    """The connection dict the lab-shared default resolves to right now.

    For the lab built-in (or an unset/unresolved reference, or -- defensively --
    an old store that pointed the default at SQLite) this is the immutable
    setup-wizard credentials from lab_info.json; for a custom database promoted
    to default it is that database's stored connection fields. The result is what
    :func:`apply_default_database` publishes as the live :data:`LAB_DB`.
    """
    entry = resolve_default_database(extra)
    if entry is None or entry.get("db_mode") != DB_MODE_CUSTOM:
        return dict(wizard_lab_db())
    return {key: str(entry.get(key, "") or "") for key in DATABASE_CONN_KEYS}


def apply_default_database(extra):
    """Re-point the live lab-shared credentials at the store's chosen default.

    Sets the module :data:`LAB_DB` (and the DEFAULT_CONFIG db_* seeds) to the
    connection the ``default_database`` reference resolves to. Both UIs call this
    right after loading the store and again whenever the default is changed, so
    every DB_MODE_LAB code path -- normalize_config, build_database_url,
    build_env, the built-in lab picker entry -- follows the chosen default with
    no other change. Returns the resolved credentials dict.
    """
    global LAB_DB
    LAB_DB = dict(lab_shared_db_fields(extra))
    DEFAULT_CONFIG.update({
        "db_name": LAB_DB["db_name"], "db_user": LAB_DB["db_user"],
        "db_password": LAB_DB["db_password"], "db_host": LAB_DB["db_host"],
        "db_port": LAB_DB["db_port"],
    })
    return LAB_DB


def default_database_options(extra):
    """The databases selectable as the lab-shared default, in picker order.

    The lab-shared database is a Postgres database, so this offers the lab
    built-in (the setup-wizard DB) plus every custom database in the registry,
    and excludes the SQLite built-in (which is the separate "oTree default
    (SQLite)" / no-database run-config choice, not a shared lab database).
    """
    return [entry for entry in list_databases(extra)
            if entry.get("db_mode") != DB_MODE_NONE]


def set_default_database(extra, db_id):
    """Choose which known database is the lab-shared default, by reference.

    Validates that ``db_id`` names a real Postgres database (built-in lab or a
    custom in the registry), stores its id in ``extra[DEFAULT_DATABASE_KEY]``, and
    re-resolves the live :data:`LAB_DB` via :func:`apply_default_database`.
    Mutates ``extra`` in place and returns the chosen entry. Raises ``ValueError``
    for an unknown id or the SQLite built-in (which cannot be a shared lab DB).
    """
    if extra is None:
        raise ValueError("set_default_database needs a store `extra` dict to write into")
    entry = find_database(extra, db_id)
    if entry is None:
        raise ValueError("No database with id %r to make the default." % (db_id,))
    if entry.get("db_mode") == DB_MODE_NONE:
        raise ValueError(
            "The lab shared database must be a Postgres database, not the oTree "
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

    caution = (c["db_mode"] == DB_MODE_LAB)
    # A room with participant PC links has a lab desktop shortcut (which encodes
    # both the room and which server); the briefing names that shortcut instead
    # of a raw link. A room without one has no shortcut, so the briefing gives
    # the manual per-seat link to open on each computer. The shortcut room is the
    # lab's OWN default_room now, not a hardcoded "study".
    has_participant_links = room_has_participant_links(room, lab_room)
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
        "shortcut_name": study_shortcut_name(lab_label),
        # The human name of THIS lab's participant-PC desktop shortcuts (blank
        # when the lab did not set one). When set, the briefing tells the user to
        # open the "<label>" shortcut on each participant PC.
        "shortcut_label": shortcut_label,
        "caution": caution,
        "caution_text": CAUTION_TEXT if caution else "",
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
            % ("lab" if c["db_mode"] == DB_MODE_LAB else "chosen", host, port,
               _pg_error(error)),
            mask_database_url(url))
        # Carry the db_mode so issue_fix_for can offer "Use lab default instead"
        # only when it is a CUSTOM database that failed (switching to the lab
        # default is meaningless when the lab default is itself what failed).
        result["db_mode"] = c["db_mode"]
        return result
    try:
        conn.close()
    except Exception:
        pass
    return _preflight_result(
        "database", True,
        "Connected to the lab database at %s:%s." % (host, port),
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
        return _preflight_result(
            "port", False,
            "Port %d is already in use: another server is running." % port,
            str(err))

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
        return _preflight_result(
            "project", False,
            "No settings.py found in %s. Is this an oTree project?" % path,
            settings_path_for(path))

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
    if check == "database" and failure.get("db_mode") == DB_MODE_CUSTOM:
        return {"fix": "use_lab_default", "fix_label": "Use lab default instead"}
    return {}


def switch_to_lab_default(cfg):
    """Return a normalized copy of ``cfg`` switched to the lab shared database.

    The switch behind the "Use lab default instead" recovery action both
    launchers show when the chosen custom database cannot be connected to. It
    flips ``db_mode`` to :data:`DB_MODE_LAB`; ``normalize_config`` then forces the
    live :data:`LAB_DB` credentials in, so a re-launch runs against the lab shared
    database. The single source of truth for that switch, shared by both UIs.
    """
    out = dict(cfg or {})
    out["db_mode"] = DB_MODE_LAB
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


def create_database(admin, new_db, new_user="", new_password=""):
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
    """
    result = {"ok": False, "reason": "", "message": "", "created_db": False,
              "created_role": False, "connect_ok": False, "fields": None}

    admin = admin or {}
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
        conn = psycopg2.connect(dbname="postgres", user=a_user, password=a_pw,
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
                        cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                            sql.Identifier(new_user), sql.Literal(new_password)))
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
    try:
        vconn = psycopg2.connect(dbname=new_db, user=c_user, password=c_pw,
                                 host=a_host, port=a_port, connect_timeout=8)
        vconn.close()
        result["connect_ok"] = True
    except psycopg2.Error as error:
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
    result["fields"] = {
        "db_mode": DB_MODE_CUSTOM,
        "db_name": new_db,
        "db_user": c_user,
        "db_password": c_pw,
        "db_host": a_host,
        "db_port": a_port,
    }
    if new_user and role_existed:
        who = "owned by the existing role %r" % new_user
    elif new_user:
        who = "owned by the new role %r" % new_user
    else:
        who = "owned by the admin role %r" % a_user
    result["message"] = "Created database %r, %s. Connection verified." % (new_db, who)
    return result


# NOTE: pure logic, no tkinter. Shared by otree_lab_launcher.py (Tk UI) and
# otree_launcher_web.py (pywebview UI).
